"""
Main bot orchestration and monitoring loop
"""
import signal
import time
import sys
import requests
from datetime import datetime
from typing import Dict, List

from bot.config import config
from bot.raydium_client import RaydiumAPIClient
from bot.trading.executor import RaydiumExecutor
from bot.analysis.pool_analyzer import PoolAnalyzer
from bot.trading.position_manager import PositionManager
from bot.analysis.price_tracker import PriceTracker
from bot.analysis.pool_quality import PoolQualityAnalyzer


class LiquidityBot:
    def __init__(self):
        self.api_client = RaydiumAPIClient()
        self.analyzer = PoolAnalyzer()
        self.position_manager = PositionManager()
        self.price_tracker = PriceTracker(self.api_client)

        # Executor for real transactions
        try:
            self.executor = RaydiumExecutor() if config.TRADING_ENABLED else None
        except Exception as e:
            print(f"⚠️  Could not initialize executor: {e}")
            self.executor = None

        # Unwrap any WSOL to native SOL at startup
        # (the SDK uses native SOL for all operations)
        if self.executor and not config.DRY_RUN:
            wsol = self.executor.get_wsol_balance()
            if wsol > 0.001:
                print(f"🔄 Unwrapping {wsol:.4f} WSOL → native SOL...")
                unwrapped = self.executor.unwrap_wsol()
                if unwrapped > 0:
                    print(f"✓ Unwrapped {unwrapped:.4f} WSOL → native SOL")

            # Recover any leftover LP tokens from previous runs
            self._recover_leftover_lp_tokens()

        # Capital is always the current wallet balance
        self.available_capital = 0.0  # in SOL
        self._refresh_balance()

        # State
        self.running = False
        self._shutting_down = False
        self.last_pool_scan = 0
        self.last_position_check = 0
        self._failed_pools = set()  # Track pools that failed to avoid retrying

        sol_price = self.api_client.get_sol_price_usd()

        print("=" * 60)
        print("Raydium LP Bot Initialized")
        print("=" * 60)
        print(f"Mode: {'DRY RUN (Paper Trading)' if config.DRY_RUN else 'LIVE TRADING'}")
        if sol_price > 0:
            print(f"SOL Price: ${sol_price:.2f}")
        if not config.DRY_RUN and self.executor:
            print(f"Wallet: {self.executor.wallet.pubkey()}")
            print(f"SOL Balance: {self._usd(self.available_capital, sol_price)}")
        print(f"Available Capital: {self._usd(self.available_capital, sol_price)}")
        print(f"Max Positions: {config.MAX_CONCURRENT_POSITIONS}")
        print(f"Stop Loss: {config.STOP_LOSS_PERCENT}%")
        print(f"Take Profit: {config.TAKE_PROFIT_PERCENT}%")
        print(f"Max Hold Time: {config.MAX_HOLD_TIME_HOURS}h")
        print(f"Min LP Burn: {config.MIN_BURN_PERCENT}%")
        print("=" * 60)

    def _refresh_balance(self):
        """Refresh available capital from native SOL balance.
        
        WSOL is unwrapped at startup, and the SDK uses native SOL
        directly for all operations (creates temp WSOL accounts internally).
        """
        if self.executor and not config.DRY_RUN:
            new_balance = self.executor.get_balance()
            if abs(new_balance - self.available_capital) > 0.0001:
                print(f"💰 Wallet balance: {new_balance:.4f} SOL")
            self.available_capital = new_balance
        elif config.DRY_RUN:
            if self.available_capital == 0.0:
                self.available_capital = 1.0
                print(f"💰 Dry run mode: simulated {self.available_capital:.4f} SOL")

    def _recover_leftover_lp_tokens(self):
        """Find any leftover LP tokens from previous runs and convert them back to SOL.

        Uses the same proven recovery flow as recover.py:
          1. List all token accounts in the wallet
          2. Batch-query Raydium API to identify which are LP mints
          3. For each LP token: removeLiquidity → swap tokens → unwrap WSOL
        """
        if not self.executor:
            return

        # Step 1: Get all non-zero token accounts
        all_tokens = self.executor.list_all_tokens()
        if not all_tokens:
            return

        # Filter out WSOL (already handled by unwrap above)
        wsol_mint = "So11111111111111111111111111111111111111112"
        token_mints = [t['mint'] for t in all_tokens if t['mint'] != wsol_mint and int(t.get('balance', 0)) > 0]
        if not token_mints:
            return

        # Step 2: Batch-query Raydium API to find which are LP mints
        # API endpoint: /pools/info/lps?lps=mint1,mint2,...
        lp_positions = []
        try:
            batch_size = 20  # API might limit query size
            for i in range(0, len(token_mints), batch_size):
                batch = token_mints[i:i + batch_size]
                url = f"{self.api_client.BASE_URL}/pools/info/lps?lps={','.join(batch)}"
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    continue
                data = resp.json().get('data', [])
                for pool in data:
                    pool_id = pool.get('id', '')
                    lp_mint_info = pool.get('lpMint', {})
                    lp_mint_addr = lp_mint_info.get('address', '')
                    lp_decimals = lp_mint_info.get('decimals', 9)
                    mint_a = pool.get('mintA', {})
                    mint_b = pool.get('mintB', {})
                    pool_name = f"{mint_a.get('symbol', '?')}/{mint_b.get('symbol', '?')}"
                    if pool_id and lp_mint_addr:
                        lp_positions.append({
                            'pool_id': pool_id,
                            'lp_mint': lp_mint_addr,
                            'lp_decimals': lp_decimals,
                            'name': pool_name,
                        })
        except Exception as e:
            print(f"⚠ Error checking LP mints via API: {e}")
            return

        if not lp_positions:
            return

        # Step 3: Recover each LP position (same flow as recover.py)
        print(f"\n{'=' * 60}")
        print(f"🔄 Found {len(lp_positions)} leftover LP position(s) — recovering...")
        print(f"{'=' * 60}")

        for pos in lp_positions:
            pool_id = pos['pool_id']
            name = pos['name']

            try:
                # Remove liquidity (bridge reads exact on-chain balance)
                print(f"\n🔄 Removing liquidity: {name}")
                sig = self.executor.remove_liquidity(pool_id=pool_id, lp_token_amount=0)
                if not sig:
                    print(f"  ✗ Remove liquidity failed for {name}, skipping")
                    continue

                time.sleep(3)

                # Swap remaining tokens back to SOL
                token_name = name.replace('WSOL/', '').replace('/WSOL', '')
                print(f"🔄 Swapping all {token_name} → SOL...")
                self.executor.swap_tokens(pool_id=pool_id, amount_in=0, direction='sell')

                time.sleep(2)

                # Unwrap any WSOL created by the swap
                wsol = self.executor.get_wsol_balance()
                if wsol > 0.001:
                    self.executor.unwrap_wsol()

                print(f"✓ Recovered {name}")

            except Exception as e:
                print(f"  ✗ Error recovering {name}: {e}")

        print(f"{'=' * 60}")
        print(f"✓ LP recovery complete")
        print(f"{'=' * 60}\n")

    def scan_and_rank_pools(self) -> list:
        """Scan for pools and rank them."""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning pools...")

        pools = self.api_client.get_filtered_pools(
            min_liquidity=config.MIN_LIQUIDITY_USD,
            min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
            min_apr=config.MIN_APR_24H,
        )

        # Filter by burn percent
        pools = [p for p in pools if p.get('burnPercent', 0) >= config.MIN_BURN_PERCENT]

        safe_pools = PoolQualityAnalyzer.get_safe_pools(
            pools,
            check_locks=config.CHECK_TOKEN_SAFETY
        )

        top_pools = self.analyzer.rank_pools(safe_pools, top_n=10)

        print(f"  Found {len(pools)} qualifying pools (burn≥{config.MIN_BURN_PERCENT}%)")
        print(f"  Safe pools after quality check: {len(safe_pools)}")
        if top_pools:
            best = top_pools[0]
            print(f"  Top pool: {best['name']} (score: {best['score']:.1f}, burn: {best.get('burnPercent', 0):.0f}%)")

        return top_pools

    def update_positions(self):
        """Update all active positions with current data."""
        if not self.position_manager.active_positions:
            return

        pools_data = {}
        on_chain_data = {}  # amm_id -> {valueSol, priceRatio}

        for amm_id, pos in self.position_manager.active_positions.items():
            pool = self.api_client.get_pool_by_id(amm_id)
            if pool:
                pools_data[amm_id] = pool

            # Get on-chain LP value + real-time price from reserves
            if pos.lp_mint and pos.lp_token_amount > 0 and self.executor:
                data = self.executor.get_lp_value_sol(amm_id, pos.lp_mint)
                if data:
                    on_chain_data[amm_id] = data
            elif not pos.lp_mint:
                print(f"  ⚠ No lp_mint set for {pos.pool_name}")
            elif pos.lp_token_amount <= 0:
                print(f"  ⚠ lp_token_amount is 0 for {pos.pool_name}")

        # Update each position — prefer on-chain price, fall back to API
        for amm_id, position in list(self.position_manager.active_positions.items()):
            chain = on_chain_data.get(amm_id, {})
            lp_value_sol = chain.get('valueSol')
            current_price = chain.get('priceRatio', 0)

            # Fall back to API price only if on-chain price unavailable
            if current_price <= 0:
                current_price = self.price_tracker.get_current_price(
                    amm_id, pools_data.get(amm_id)
                )

            pool_data = pools_data.get(amm_id, position.pool_data)
            if current_price > 0:
                position.update_metrics(
                    current_price,
                    pool_data,
                    lp_value_sol=lp_value_sol,
                )

    def _exit_position(self, amm_id: str, reason: str) -> bool:
        """Execute the full on-chain exit for a single position.
        Returns True if the position was successfully closed."""
        position = self.position_manager.active_positions.get(amm_id)
        if not position:
            return False

        if not config.DRY_RUN and self.executor:
            # Step 1: Remove liquidity using tracked LP token amount
            lp_amount = position.lp_token_amount
            if lp_amount <= 0:
                # Fallback: query LP balance on-chain
                if position.lp_mint:
                    lp_amount_raw = self.executor.get_token_balance(position.lp_mint)
                    if lp_amount_raw > 0 and position.lp_decimals > 0:
                        lp_amount = lp_amount_raw / (10 ** position.lp_decimals)
                    elif lp_amount_raw > 0:
                        lp_amount = lp_amount_raw / 1e9  # default to 9 decimals

            if lp_amount > 0:
                print(f"\n🔄 Removing liquidity ({lp_amount:.6f} LP tokens)...")
                signature = self.executor.remove_liquidity(
                    pool_id=amm_id,
                    lp_token_amount=lp_amount,
                )

                if not signature:
                    print(f"✗ Remove liquidity failed - position still open")
                    return False

                position.pool_data['exit_signature'] = signature
            else:
                print(f"⚠ No LP tokens found for {position.pool_name}")

            # Step 2: Swap all remaining non-SOL tokens back to SOL
            import time as _time
            _time.sleep(2)
            token_name = position.pool_name.replace('WSOL/', '').replace('/WSOL', '')
            print(f"🔄 Swapping all {token_name} → SOL...")
            self.executor.swap_tokens(
                pool_id=amm_id,
                amount_in=0,  # sell-all mode
                direction='sell',
            )

        success = self.position_manager.close_position(
            amm_id, reason, sol_price_usd=self.api_client.get_sol_price_usd()
        )
        if success:
            self._refresh_balance()
        return success

    def check_and_execute_exits(self):
        """Check exit conditions and close positions."""
        exits = self.position_manager.check_exit_conditions()

        for amm_id, reason in exits:
            self._exit_position(amm_id, reason)

    def look_for_new_entries(self, top_pools: list):
        """Look for new entry opportunities. Pools are already sorted by score (best first).
        Higher-ranked pools get larger position sizes."""
        self._refresh_balance()

        if not self.position_manager.can_open_position(self.available_capital):
            return

        # Snapshot the total wallet balance for reserve calculations
        # (this stays constant across all entries in this cycle)
        total_wallet_balance = self.available_capital

        active_amm_ids = set(self.position_manager.active_positions.keys())
        failed_amm_ids = getattr(self, '_failed_pools', set())
        available_pools = [p for p in top_pools if p['ammId'] not in active_amm_ids and p['ammId'] not in failed_amm_ids]

        if not available_pools:
            return

        for rank, pool in enumerate(available_pools):
            if not self.position_manager.can_open_position(self.available_capital):
                break

            # Enforce absolute minimum reserve before even trying
            reserve_floor = max(
                total_wallet_balance * config.RESERVE_PERCENT,
                config.MIN_RESERVE_SOL,
            )
            if self.available_capital <= reserve_floor:
                print(f"⚠ Available capital ({self.available_capital:.4f} SOL) at reserve floor "
                      f"({reserve_floor:.4f} SOL) — not entering new positions")
                break

            if pool['score'] < 50:
                continue

            print(f"\n💡 Entry opportunity: {pool['name']} "
                  f"(score: {pool['score']:.1f}, rank #{rank + 1}/{len(available_pools)})")

            current_price = self.price_tracker.get_current_price(
                pool['ammId'],
                pool
            )

            if current_price <= 0:
                print(f"⚠ Could not get valid price for {pool['name']}, skipping")
                continue

            position = self.position_manager.open_position(
                pool,
                self.available_capital,
                current_price,
                sol_price_usd=self.api_client.get_sol_price_usd(),
                total_wallet_balance=total_wallet_balance,
                rank=rank,
                total_ranked=len(available_pools),
            )

            if not position:
                continue

            amm_id = pool['ammId']

            if not config.DRY_RUN and self.executor:
                # Step 1: Swap half the SOL into the other token
                token_name = pool.get('name', '').replace('WSOL/', '').replace('/WSOL', '')
                print(f"\n🔄 Swapping {position.sol_amount:.6f} SOL → {token_name}...")
                swap_sig = self.executor.swap_tokens(
                    pool_id=amm_id,
                    amount_in=position.sol_amount,
                    direction='buy',
                )

                if not swap_sig:
                    print(f"✗ Swap failed - removing position, trying next pool")
                    self.position_manager.close_position(amm_id)
                    self._failed_pools.add(amm_id)
                    self._refresh_balance()
                    continue

                # Brief delay for on-chain state to settle
                import time as _time
                _time.sleep(2)

                # Step 2: Add liquidity with both tokens
                print(f"🔄 Executing add liquidity transaction...")
                add_result = self.executor.add_liquidity(
                    pool_id=amm_id,
                    token_a_amount=position.token_a_amount,
                    token_b_amount=position.token_b_amount,
                )

                if not add_result:
                    print(f"✗ Transaction failed - swapping back to SOL")
                    # Use 0 amount to signal 'sell all' — the bridge will query the actual balance
                    self.executor.swap_tokens(
                        pool_id=amm_id,
                        amount_in=0,  # sell all available
                        direction='sell',
                    )
                    self.position_manager.close_position(amm_id)
                    self._failed_pools.add(amm_id)
                    self._refresh_balance()
                    continue

                position.pool_data['entry_signature'] = add_result['signature']

                # Track LP token balance
                lp_mint = add_result.get('lpMint', '')
                if lp_mint:
                    position.lp_mint = lp_mint
                    import time as _time
                    _time.sleep(1)
                    lp_raw = self.executor.get_token_balance(lp_mint)
                    # LP decimals typically match base decimals
                    lp_decimals = pool.get('lpDecimals', 9)
                    position.lp_decimals = lp_decimals
                    position.lp_token_amount = lp_raw / (10 ** lp_decimals)
                    print(f"  LP tokens received: {position.lp_token_amount:.6f} (mint: {lp_mint[:8]}...)")

            self._refresh_balance()

    @staticmethod
    def _usd(sol_amount: float, sol_price: float) -> str:
        """Format a SOL amount with USD equivalent in brackets."""
        if sol_price > 0:
            return f"{sol_amount:.4f} SOL (${sol_amount * sol_price:.2f})"
        return f"{sol_amount:.4f} SOL"

    def print_status(self):
        """Print bot status."""
        summary = self.position_manager.get_summary()
        sol_price = self.api_client.get_sol_price_usd()

        print(f"\n{'─' * 60}")
        print(f"Bot Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if sol_price > 0:
            print(f"SOL Price: ${sol_price:.2f}")
        print(f"{'─' * 60}")
        print(f"Active Positions: {summary['active_positions']}/{config.MAX_CONCURRENT_POSITIONS}")
        print(f"Deployed Capital: {self._usd(summary['total_deployed_sol'], sol_price)}")
        print(f"Available Capital: {self._usd(self.available_capital, sol_price)}")
        print(f"Total P&L: {self._usd(summary['total_pnl_sol'], sol_price)}")
        print(f"  ├─ Fees Earned: {self._usd(summary['total_fees_sol'], sol_price)}")
        print(f"  └─ Avg IL: {summary['avg_il_percent']:.2f}%")

        if self.position_manager.active_positions:
            print(f"\nActive Positions:")
            for amm_id, pos in self.position_manager.active_positions.items():
                pnl_usd = f" (${pos.unrealized_pnl_sol * sol_price:.2f})" if sol_price > 0 else ""
                size_str = self._usd(pos.position_size_sol, sol_price)
                lp_val_str = ""
                if pos.current_lp_value_sol > 0:
                    lp_val_str = f" | Value: {self._usd(pos.current_lp_value_sol, sol_price)}"
                print(f"  • {pos.pool_name}")
                print(f"    Size: {size_str} | Time: {pos.time_held_hours:.1f}h | IL: {pos.current_il_percent:.2f}% | "
                      f"P&L: {pos.unrealized_pnl_sol:.4f} SOL{pnl_usd} ({pos.pnl_percent:.2f}%){lp_val_str}")

        print(f"{'─' * 60}\n")

    def run(self):
        """Main bot loop."""
        self.running = True

        # Register signal handlers for graceful shutdown
        def _signal_handler(sig, frame):
            print(f"\n\n⚠ Signal received ({sig})...")
            self.shutdown()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        print("\n🚀 Starting bot main loop...")
        print("Press Ctrl+C to stop (will exit all positions)\n")

        try:
            iteration = 0
            top_pools = []
            while self.running:
                iteration += 1
                current_time = time.time()

                if current_time - self.last_pool_scan >= config.POOL_SCAN_INTERVAL_SEC:
                    top_pools = self.scan_and_rank_pools()
                    self.last_pool_scan = current_time
                    self._failed_pools.clear()  # Reset failed pools on new scan

                if top_pools:
                    self.look_for_new_entries(top_pools)

                if current_time - self.last_position_check >= config.POSITION_CHECK_INTERVAL_SEC:
                    self.update_positions()
                    self.check_and_execute_exits()
                    self.last_position_check = current_time

                if iteration % 10 == 0:
                    self.print_status()

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n⚠ Shutdown signal received...")
            self.shutdown()

    def shutdown(self):
        """Graceful shutdown — exit ALL positions on-chain."""
        if self._shutting_down:
            print("\n⚠ Already shutting down... please wait")
            return
        self._shutting_down = True

        # Prevent further Ctrl+C from interrupting exits
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        print("\nShutting down bot...")
        self.running = False

        positions = list(self.position_manager.active_positions.keys())
        if positions:
            print(f"\n⚠ Exiting {len(positions)} active position(s)...")
            for amm_id in positions:
                pos = self.position_manager.active_positions.get(amm_id)
                name = pos.pool_name if pos else amm_id[:8]
                print(f"\n{'─' * 40}")
                print(f"Exiting: {name}")
                try:
                    self._exit_position(amm_id, "Shutdown")
                except Exception as e:
                    print(f"✗ Error exiting {name}: {e}")
                    # Still remove from tracking even if on-chain exit fails
                    self.position_manager.close_position(
                        amm_id, f"Shutdown (error: {e})",
                        sol_price_usd=self.api_client.get_sol_price_usd()
                    )

        self.print_status()
        print("✓ Bot stopped")
        sys.exit(0)


def main():
    """Entry point."""
    bot = LiquidityBot()
    bot.run()


if __name__ == "__main__":
    main()
