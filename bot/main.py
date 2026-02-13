"""
Main bot orchestration and monitoring loop
"""
import time
import sys
from datetime import datetime
from typing import Dict

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

        # Capital is always the current wallet balance
        self.available_capital = 0.0  # in SOL
        self._refresh_balance()

        # State
        self.running = False
        self.last_pool_scan = 0
        self.last_position_check = 0

        print("=" * 60)
        print("Raydium LP Bot Initialized")
        print("=" * 60)
        print(f"Mode: {'DRY RUN (Paper Trading)' if config.DRY_RUN else 'LIVE TRADING'}")
        if not config.DRY_RUN and self.executor:
            balances = self.executor.get_wallet_balances()
            print(f"Wallet: {self.executor.wallet.pubkey()}")
            print(f"SOL Balance:  {balances['sol']:.4f} SOL")
            print(f"WSOL Balance: {balances['wsol']:.4f} WSOL")
            print(f"Total:        {balances['total_sol']:.4f} SOL")
        print(f"Available Capital: {self.available_capital:.4f} SOL")
        print(f"Max Positions: {config.MAX_CONCURRENT_POSITIONS}")
        print(f"Stop Loss: {config.STOP_LOSS_PERCENT}%")
        print(f"Take Profit: {config.TAKE_PROFIT_PERCENT}%")
        print(f"Max Hold Time: {config.MAX_HOLD_TIME_HOURS}h")
        print(f"Min LP Burn: {config.MIN_BURN_PERCENT}%")
        print("=" * 60)

    def _refresh_balance(self):
        """Refresh available capital from actual wallet balance."""
        if self.executor and not config.DRY_RUN:
            wsol = self.executor.get_wsol_balance()
            sol = self.executor.get_balance()
            new_balance = wsol + sol
            if abs(new_balance - self.available_capital) > 0.0001:
                print(f"💰 Wallet balance refreshed: {new_balance:.4f} SOL")
            self.available_capital = new_balance
        elif config.DRY_RUN:
            if self.available_capital == 0.0:
                self.available_capital = 1.0
                print(f"💰 Dry run mode: simulated {self.available_capital:.4f} SOL")

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

        current_prices = self.price_tracker.get_current_prices_batch(
            self.position_manager.active_positions
        )

        pools_data = {}
        for amm_id in self.position_manager.active_positions.keys():
            pool = self.api_client.get_pool_by_id(amm_id)
            if pool:
                pools_data[amm_id] = pool

        self.position_manager.update_all_positions(current_prices, pools_data)

    def check_and_execute_exits(self):
        """Check exit conditions and close positions."""
        exits = self.position_manager.check_exit_conditions()

        for amm_id, reason in exits:
            position = self.position_manager.active_positions.get(amm_id)
            if not position:
                continue

            if not config.DRY_RUN and self.executor:
                print(f"\n🔄 Executing remove liquidity transaction...")
                lp_amount = position.pool_data.get('lp_tokens', 1.0)

                signature = self.executor.remove_liquidity(
                    pool_id=amm_id,
                    lp_token_amount=lp_amount,
                    slippage=0.01
                )

                if not signature:
                    print(f"✗ Exit transaction failed - position still open")
                    continue

                position.pool_data['exit_signature'] = signature

            success = self.position_manager.close_position(amm_id, reason)
            if success:
                self._refresh_balance()

    def look_for_new_entries(self, top_pools: list):
        """Look for new entry opportunities."""
        self._refresh_balance()

        if not self.position_manager.can_open_position(self.available_capital):
            return

        active_amm_ids = set(self.position_manager.active_positions.keys())
        available_pools = [p for p in top_pools if p['ammId'] not in active_amm_ids]

        if not available_pools:
            print("  ⓘ No new pools available (all top pools already have positions)")
            return

        best_pool = available_pools[0]

        if best_pool['score'] >= 50:
            print(f"\n💡 Entry opportunity: {best_pool['name']} (score: {best_pool['score']:.1f})")

            current_price = self.price_tracker.get_current_price(
                best_pool['ammId'],
                best_pool
            )

            if current_price <= 0:
                print(f"⚠ Could not get valid price for {best_pool['name']}, skipping")
                return

            position = self.position_manager.open_position(
                best_pool,
                self.available_capital,
                current_price
            )

            if position:
                if not config.DRY_RUN and self.executor:
                    print(f"\n🔄 Executing add liquidity transaction...")
                    signature = self.executor.add_liquidity(
                        pool_id=best_pool['ammId'],
                        token_a_amount=position.token_a_amount,
                        token_b_amount=position.token_b_amount,
                        slippage=0.01
                    )

                    if not signature:
                        print(f"✗ Transaction failed - removing position")
                        self.position_manager.close_position(best_pool['ammId'])
                        return

                    position.pool_data['entry_signature'] = signature

                self._refresh_balance()

    def print_status(self):
        """Print bot status."""
        summary = self.position_manager.get_summary()

        print(f"\n{'─' * 60}")
        print(f"Bot Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'─' * 60}")
        print(f"Active Positions: {summary['active_positions']}/{config.MAX_CONCURRENT_POSITIONS}")
        print(f"Deployed Capital: {summary['total_deployed_sol']:.4f} SOL")
        print(f"Available Capital: {self.available_capital:.4f} SOL")
        print(f"Total P&L: {summary['total_pnl_sol']:.4f} SOL")
        print(f"  ├─ Fees Earned: {summary['total_fees_sol']:.4f} SOL")
        print(f"  └─ Avg IL: {summary['avg_il_percent']:.2f}%")

        if self.position_manager.active_positions:
            print(f"\nActive Positions:")
            for amm_id, pos in self.position_manager.active_positions.items():
                print(f"  • {pos.pool_name}")
                print(f"    Time: {pos.time_held_hours:.1f}h | IL: {pos.current_il_percent:.2f}% | "
                      f"P&L: {pos.unrealized_pnl_sol:.4f} SOL ({pos.pnl_percent:.2f}%)")

        print(f"{'─' * 60}\n")

    def run(self):
        """Main bot loop."""
        self.running = True

        print("\n🚀 Starting bot main loop...")
        print("Press Ctrl+C to stop\n")

        try:
            iteration = 0
            while self.running:
                iteration += 1
                current_time = time.time()

                if current_time - self.last_pool_scan >= config.POOL_SCAN_INTERVAL_SEC:
                    top_pools = self.scan_and_rank_pools()
                    self.last_pool_scan = current_time
                    self.look_for_new_entries(top_pools)
                else:
                    top_pools = []

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
        """Graceful shutdown."""
        print("Shutting down bot...")
        self.running = False

        if config.ENABLE_EMERGENCY_EXIT:
            print("Closing all positions...")
            for amm_id in list(self.position_manager.active_positions.keys()):
                self.position_manager.close_position(amm_id, "Emergency Exit")

        self.print_status()
        print("✓ Bot stopped")
        sys.exit(0)


def main():
    """Entry point."""
    bot = LiquidityBot()
    bot.run()


if __name__ == "__main__":
    main()
