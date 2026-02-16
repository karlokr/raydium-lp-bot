"""
Main bot orchestration and monitoring loop

Threading architecture:
  - Main thread: display only (prints status every N seconds)
  - Position check thread: updates positions & detects exits (every 1s)
  - Pool scan thread: scans & ranks pools, queues buy orders
  - Buy worker thread: executes buy orders sequentially from a queue
  - Sell: exits are executed in parallel via ThreadPoolExecutor
"""
import signal
import time
import sys
import threading
import queue
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List

from bot.config import config
from bot.raydium_client import RaydiumAPIClient
from bot.trading.executor import RaydiumExecutor
from bot.analysis.pool_analyzer import PoolAnalyzer
from bot.trading.position_manager import PositionManager
from bot.analysis.price_tracker import PriceTracker
from bot.analysis.pool_quality import PoolQualityAnalyzer
from bot.analysis.snapshot_tracker import SnapshotTracker
from bot import state


class LiquidityBot:
    def __init__(self):
        self.api_client = RaydiumAPIClient()
        self.analyzer = PoolAnalyzer()
        self.snapshot_tracker = SnapshotTracker(max_snapshots=10)  # ~30 min window at 3-min scans
        self.analyzer.set_snapshot_tracker(self.snapshot_tracker)
        self.position_manager = PositionManager()
        self.price_tracker = PriceTracker(self.api_client)
        self.quality_analyzer = PoolQualityAnalyzer()  # Persistent: RugCheck + LP lock caches survive across scans

        # Executor for real transactions
        try:
            self.executor = RaydiumExecutor() if config.TRADING_ENABLED else None
        except Exception as e:
            print(f"⚠️  Could not initialize executor: {e}")
            self.executor = None

        # State (initialize before anything reads it)
        self.running = False
        self._shutting_down = False
        self.last_pool_scan = 0
        self.last_position_check = 0
        self.last_status_print = 0
        self._failed_pools = set()  # Track pools that failed to avoid retrying
        self._exit_cooldowns: dict = {}  # amm_id -> (timestamp, cooldown_duration)
        self._stop_loss_strikes: dict = {}  # amm_id -> consecutive stop-loss count
        self._permanent_blacklist: set = set()  # amm_ids permanently banned (loaded from state)
        self._last_scan_pools: list = []  # Top-ranked pools from last scan
        self._last_balance_refresh = 0.0  # Timestamp of last RPC balance read

        # Threading infrastructure
        self._state_lock = threading.Lock()  # Protects shared mutable state
        self._buy_queue: queue.Queue = queue.Queue()  # Pool dicts to enter
        self._threads: dict = {}  # name -> Thread
        self._selling = threading.Event()  # Set while a sell batch is in progress

        # Restore state from disk FIRST — positions must be known
        # before LP recovery so we don't accidentally exit them
        self._load_saved_state()

        # Full cleanup at startup:
        #   1. Unwrap WSOL → native SOL
        #   2. Auto-close ghost positions (LP=0 on-chain)
        #   3. Recover leftover LP tokens (orphans from previous runs)
        #   4. Sell leftover non-SOL tokens (from failed exit swaps)
        #   5. Close empty token accounts → reclaim rent
        if self.executor and not config.DRY_RUN:
            wsol = self.executor.get_wsol_balance()
            if wsol > 0.001:
                print(f"🔄 Unwrapping {wsol:.4f} WSOL → native SOL...")
                unwrapped = self.executor.unwrap_wsol()
                if unwrapped > 0:
                    print(f"✓ Unwrapped {unwrapped:.4f} WSOL → native SOL")

            # Auto-close ghost positions (LP tokens gone on-chain)
            self._cleanup_ghost_positions()

            # Recover any leftover LP tokens from previous runs
            # (skips LP mints belonging to restored positions)
            self._recover_leftover_lp_tokens()

            # Sell any leftover non-SOL tokens (e.g. from failed exit swaps)
            self._sweep_leftover_tokens()

            # Close empty token accounts to reclaim rent
            keep_mints = [pos.lp_mint for pos in self.position_manager.active_positions.values() if pos.lp_mint]
            result = self.executor.close_empty_accounts(keep_mints=keep_mints)
            if result['closed'] > 0:
                time.sleep(2)
                print(f"🧹 Closed {result['closed']} empty token account(s), reclaimed ~{result['reclaimedSol']:.4f} SOL in rent")

        # Capital is always the current wallet balance
        self.available_capital = 0.0  # in SOL
        self._refresh_balance(force=True)

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

    def _refresh_balance(self, force: bool = False):
        """Refresh available capital from native SOL balance.
        
        Throttled to at most once per 60 seconds unless force=True.
        Entry/exit paths pass force=True to get an immediate read.
        """
        if self.executor and not config.DRY_RUN:
            now = time.time()
            if not force and (now - self._last_balance_refresh) < 60:
                return  # recent enough, skip RPC call
            new_balance = self.executor.get_balance()
            if abs(new_balance - self.available_capital) > 0.0001:
                print(f"💰 Wallet balance: {new_balance:.4f} SOL")
            self.available_capital = new_balance
            self._last_balance_refresh = now
        elif config.DRY_RUN:
            if self.available_capital == 0.0:
                self.available_capital = 1.0
                print(f"💰 Dry run mode: simulated {self.available_capital:.4f} SOL")

    def _load_saved_state(self):
        """Restore bot state from disk if available."""
        saved = state.load_state()
        if not saved:
            return

        age_sec = time.time() - saved.get('saved_timestamp', 0)
        age_str = f"{age_sec / 60:.0f} min" if age_sec < 3600 else f"{age_sec / 3600:.1f}h"
        print(f"\n📂 Loaded saved state from {saved['saved_at']} ({age_str} ago)")

        # Restore active positions
        restored_positions = saved.get('positions', {})
        if restored_positions:
            self.position_manager.active_positions = restored_positions
            names = [p.pool_name for p in restored_positions.values()]
            print(f"  ✓ Restored {len(restored_positions)} active position(s): {', '.join(names)}")

        # Restore exit cooldowns (skip expired ones)
        now = time.time()
        cooldowns = saved.get('exit_cooldowns', {})
        self._exit_cooldowns = {}
        for k, v in cooldowns.items():
            # Support both old format (float timestamp) and new format ([timestamp, duration])
            if isinstance(v, (list, tuple)):
                ts, dur = v
            else:
                ts, dur = v, 86400  # legacy: assume 24h
            if now - ts < dur:
                self._exit_cooldowns[k] = (ts, dur)
        if self._exit_cooldowns:
            print(f"  ✓ Restored {len(self._exit_cooldowns)} exit cooldown(s)")

        # Restore stop-loss strike counts and permanent blacklist
        self._stop_loss_strikes = saved.get('stop_loss_strikes', {})
        self._permanent_blacklist = set(saved.get('permanent_blacklist', []))
        if self._permanent_blacklist:
            print(f"  ✓ Restored {len(self._permanent_blacklist)} permanently blacklisted pool(s)")

        # Restore failed pools
        self._failed_pools = saved.get('failed_pools', set())

        # Restore snapshot tracker history
        snapshot_data = saved.get('snapshots', {})
        if snapshot_data:
            state.snapshots_from_dict(self.snapshot_tracker, snapshot_data)
            print(f"  ✓ Restored snapshot history for {len(snapshot_data)} pool(s)")

        # Restore last scan results
        self._last_scan_pools = saved.get('last_scan_pools', [])
        if self._last_scan_pools:
            print(f"  ✓ Restored {len(self._last_scan_pools)} ranked pools from last scan")

    def _save_state(self):
        """Persist current bot state to disk."""
        state.save_state(
            positions=self.position_manager.active_positions,
            exit_cooldowns=self._exit_cooldowns,
            failed_pools=self._failed_pools,
            snapshot_tracker=self.snapshot_tracker,
            last_scan_pools=self._last_scan_pools,
            stop_loss_strikes=self._stop_loss_strikes,
            permanent_blacklist=self._permanent_blacklist,
        )

    def _cleanup_ghost_positions(self):
        """At startup, check each restored position on-chain.
        If LP tokens are 0 on-chain, auto-close the ghost position
        (swap any remaining tokens back to SOL and remove from state)."""
        if not self.executor:
            return

        ghosts = []
        for amm_id, pos in list(self.position_manager.active_positions.items()):
            if not pos.lp_mint:
                ghosts.append((amm_id, pos))
                continue
            lp_raw = self.executor.get_token_balance(pos.lp_mint)
            if lp_raw == 0:
                ghosts.append((amm_id, pos))

        if not ghosts:
            return

        print(f"\n🧹 Found {len(ghosts)} ghost position(s) (LP=0 on-chain) — cleaning up...")
        for amm_id, pos in ghosts:
            print(f"  🔄 Cleaning ghost: {pos.pool_name}")
            # Try to swap any remaining non-SOL tokens back
            token_name = pos.pool_name.replace('WSOL/', '').replace('/WSOL', '')
            self._retry_swap(amm_id, token_name)
            time.sleep(2)

            sol_price = self.api_client.get_sol_price_usd()
            state.append_trade_history(pos, "Ghost cleanup (startup)", sol_price_usd=sol_price)
            self.position_manager.close_position(amm_id, "Ghost cleanup (startup)")
            print(f"  ✓ Removed ghost: {pos.pool_name}")

        self._save_state()
        print(f"🧹 Ghost cleanup complete — {len(ghosts)} position(s) removed\n")

    def _recover_leftover_lp_tokens(self):
        """Find any leftover LP tokens from previous runs and convert them back to SOL.

        Skips LP mints belonging to active (restored) positions — those are
        intentionally held and will be managed by the normal exit logic.

        Uses the same proven recovery flow as recover.py:
          1. List all token accounts in the wallet
          2. Batch-query Raydium API to identify which are LP mints
          3. For each LP token: removeLiquidity → swap tokens → unwrap WSOL
        """
        if not self.executor:
            return

        # Collect LP mints that belong to restored positions — skip them
        known_lp_mints = set()
        for pos in self.position_manager.active_positions.values():
            if pos.lp_mint:
                known_lp_mints.add(pos.lp_mint)

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
                for pool in (data or []):
                    if not pool or not isinstance(pool, dict):
                        continue
                    pool_id = pool.get('id', '')
                    lp_mint_info = pool.get('lpMint') or {}
                    lp_mint_addr = lp_mint_info.get('address', '')
                    lp_decimals = lp_mint_info.get('decimals', 9)
                    mint_a = pool.get('mintA') or {}
                    mint_b = pool.get('mintB') or {}
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

        # Filter out LP mints belonging to restored positions
        if known_lp_mints:
            before = len(lp_positions)
            lp_positions = [p for p in lp_positions if p['lp_mint'] not in known_lp_mints]
            skipped = before - len(lp_positions)
            if skipped:
                print(f"  ℹ Skipping {skipped} LP token(s) belonging to restored positions")
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

                # Swap remaining tokens back to SOL — retry on transient RPC failures
                token_name = name.replace('WSOL/', '').replace('/WSOL', '')
                if not self._retry_swap(pool_id, token_name):
                    print(f"  ⚠ Could not sell {token_name} after 3 attempts — will retry via token sweep")

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

    def _sweep_leftover_tokens(self):
        """Sell any non-SOL, non-LP tokens left in the wallet.

        This catches tokens stranded by failed exit swaps.  For each token
        we query the Raydium API to find a WSOL pool and sell-all through it.
        """
        if not self.executor:
            return

        # Mints to skip: SOL/WSOL + LP mints of active positions
        wsol_mint = "So11111111111111111111111111111111111111112"
        skip_mints = {wsol_mint}
        for pos in self.position_manager.active_positions.values():
            if pos.lp_mint:
                skip_mints.add(pos.lp_mint)

        all_tokens = self.executor.list_all_tokens()
        if not all_tokens:
            return

        leftover = [t for t in all_tokens
                     if t['mint'] not in skip_mints
                     and int(t.get('balance', 0)) > 0]
        if not leftover:
            return

        # Try to find WSOL pools for these mints via Raydium API
        print(f"🧹 Found {len(leftover)} leftover token(s) — attempting to sell back to SOL...")
        for tok in leftover:
            mint = tok['mint']
            try:
                url = (f"{self.api_client.BASE_URL}/pools/info/mint"
                       f"?mint1={wsol_mint}&mint2={mint}"
                       f"&poolType=standard&poolSortField=liquidity"
                       f"&sortType=desc&pageSize=1&page=1")
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    print(f"  ⚠ Could not look up pool for {mint[:8]}...")
                    continue

                pools_data = resp.json().get('data', {})
                pools = pools_data.get('data', []) if isinstance(pools_data, dict) else []
                if not pools:
                    print(f"  ⚠ No WSOL pool found for {mint[:8]}...")
                    continue

                pool = pools[0]
                pool_id = pool.get('id', '')
                pool_name = f"{(pool.get('mintA') or {}).get('symbol', '?')}/{(pool.get('mintB') or {}).get('symbol', '?')}"
                if not pool_id:
                    continue

                # Only use V4 AMM pools (our bridge only supports this program)
                program = pool.get('programId', '')
                if program and program != '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8':
                    print(f"  ⚠ Pool for {mint[:8]}... is not AMM V4 — skipping")
                    continue

                print(f"  🔄 Selling leftover {pool_name} tokens → SOL...")
                sig = self.executor.swap_tokens(
                    pool_id=pool_id,
                    amount_in=0,  # sell-all
                    direction='sell',
                )
                if sig:
                    print(f"  ✓ Sold {pool_name}")
                    time.sleep(2)
                else:
                    print(f"  ✗ Failed to sell {pool_name}")

            except Exception as e:
                print(f"  ✗ Error selling token {mint[:8]}...: {e}")

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

        # Record snapshots for all qualifying pools (before safety filter)
        # so the velocity tracker builds history even for pools we haven't entered.
        for pool in pools:
            pool_id = pool.get('ammId', pool.get('id', ''))
            day = pool.get('day', {})
            volume = day.get('volume', 0) or pool.get('volume24h', 0)
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            price = pool.get('price', 0)
            if pool_id and tvl > 0:
                self.snapshot_tracker.record(pool_id, volume, tvl, price)

        safe_pools = PoolQualityAnalyzer.get_safe_pools(
            pools,
            check_locks=config.CHECK_TOKEN_SAFETY,
            analyzer=self.quality_analyzer,
        )

        top_pools = self.analyzer.rank_pools(safe_pools, top_n=10)

        print(f"  Found {len(pools)} qualifying pools (burn≥{config.MIN_BURN_PERCENT}%)")
        print(f"  Safe pools after quality check: {len(safe_pools)}")
        print(f"  Snapshot tracker: {self.snapshot_tracker.pool_count()} pools tracked")
        if top_pools:
            best = top_pools[0]
            # Pool age label
            open_time = best.get('openTime', 0)
            age_str = ''
            if open_time:
                try:
                    age_days = (time.time() - int(open_time)) / 86400
                    if age_days < 1:
                        age_str = f", age: {age_days * 24:.0f}h"
                    elif age_days < 30:
                        age_str = f", age: {age_days:.0f}d"
                except (ValueError, TypeError):
                    pass
            print(f"  Top pool: {best['name']} (score: {best['score']:.1f}, "
                  f"burn: {best.get('burnPercent', 0):.0f}%, "
                  f"mom: {best.get('_momentum', 0):.0f}, "
                  f"IL: {best.get('_il_safety', 0):.0f}, "
                  f"fresh: {best.get('_freshness', 0):.0f}, "
                  f"vel: {best.get('_velocity', 0):.0f}{age_str})")

        # Persist scan results to disk
        self._last_scan_pools = top_pools
        self._save_state()

        return top_pools

    def update_positions(self):
        """Update all active positions with current data."""
        if not self.position_manager.active_positions:
            return

        pools_data = {}
        on_chain_data = {}  # amm_id -> {valueSol, priceRatio}
        ghost_positions = []  # positions whose LP tokens are gone on-chain

        # Batch-fetch LP values for ALL positions in a single subprocess
        # (2 RPC calls total instead of 6 per position)
        if self.executor:
            batch_entries = []
            for amm_id, pos in self.position_manager.active_positions.items():
                if pos.lp_mint:
                    batch_entries.append({'pool_id': amm_id, 'lp_mint': pos.lp_mint})
                else:
                    print(f"  ⚠ No lp_mint set for {pos.pool_name}")

            if batch_entries:
                batch_results = self.executor.batch_get_lp_values(batch_entries)

                for amm_id, pos in self.position_manager.active_positions.items():
                    data = batch_results.get(amm_id, {})
                    if not data:
                        continue

                    lp_bal = data.get('lpBalance', -1)
                    val = data.get('valueSol', 0)

                    # Detect ghost position: LP tokens gone on-chain
                    if val == 0 and lp_bal == 0 and pos.time_held_hours > 0.05:
                        ghost_positions.append(amm_id)
                        continue

                    # If we had lp_token_amount=0 (missed at entry) but LP exists, fix it
                    if lp_bal > 0 and pos.lp_token_amount == 0:
                        lp_dec = pos.lp_decimals if pos.lp_decimals > 0 else 9
                        pos.lp_token_amount = lp_bal / (10 ** lp_dec)
                        print(f"  ✓ Recovered LP balance for {pos.pool_name}: {pos.lp_token_amount:.6f}")

                    on_chain_data[amm_id] = data

        # Clean up ghost positions (LP tokens gone on-chain but state not updated)
        for amm_id in ghost_positions:
            pos = self.position_manager.active_positions.get(amm_id)
            if pos:
                print(f"  ⚠ Ghost position detected: {pos.pool_name} — LP tokens are 0 on-chain, cleaning up")
                sol_price = self.api_client.get_sol_price_usd()
                state.append_trade_history(pos, "Ghost cleanup (LP=0)", sol_price_usd=sol_price)
                self.position_manager.close_position(amm_id, "Ghost cleanup")
                self._save_state()

        if ghost_positions:
            self._refresh_balance(force=True)  # Wallet view changed after removing ghosts

        # Update each position — prefer on-chain price, fall back to API
        for amm_id, position in list(self.position_manager.active_positions.items()):
            chain = on_chain_data.get(amm_id, {})
            lp_value_sol = chain.get('valueSol')
            current_price = chain.get('priceRatio', 0)

            # Fall back to API price only if on-chain price unavailable
            if current_price <= 0:
                # Only fetch pool from API when we actually need it (rare fallback)
                if amm_id not in pools_data:
                    pool = self.api_client.get_pool_by_id(amm_id)
                    if pool:
                        pools_data[amm_id] = pool
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
                    # Check if LP tokens are actually already gone on-chain
                    # (e.g. previous exit succeeded but state wasn't saved)
                    onchain_balance = 0
                    if position.lp_mint:
                        onchain_balance = self.executor.get_token_balance(position.lp_mint)
                    if onchain_balance == 0:
                        print(f"  ℹ LP tokens already withdrawn on-chain — cleaning up stale position")
                    else:
                        print(f"✗ Remove liquidity failed - position still open")
                        return False

                else:
                    position.pool_data['exit_signature'] = signature
            else:
                print(f"⚠ No LP tokens found for {position.pool_name}")

            # Step 2: Swap all remaining non-SOL tokens back to SOL
            time.sleep(2)
            token_name = position.pool_name.replace('WSOL/', '').replace('/WSOL', '')
            if not self._retry_swap(amm_id, token_name):
                print(f"⚠ Could not sell {token_name} after 3 attempts — tokens may be stuck in wallet")

        sol_price = self.api_client.get_sol_price_usd()

        # Record trade history before closing (position is deleted on close)
        if position:
            state.append_trade_history(position, reason, sol_price_usd=sol_price)

        success = self.position_manager.close_position(
            amm_id, reason, sol_price_usd=sol_price
        )
        if success:
            self._refresh_balance(force=True)
            # Clean up snapshot history for this pool
            self.snapshot_tracker.clear_pool(amm_id)
            # Escalating cooldown on stop losses; reset on take profit
            with self._state_lock:
                if reason in ("Stop Loss", "High IL"):
                    strikes = self._stop_loss_strikes.get(amm_id, 0) + 1
                    self._stop_loss_strikes[amm_id] = strikes

                    if strikes >= config.PERMANENT_BLACKLIST_STRIKES:
                        self._permanent_blacklist.add(amm_id)
                        self._stop_loss_strikes.pop(amm_id, None)
                        self._exit_cooldowns.pop(amm_id, None)
                        pool_name = position.pool_name if position else amm_id[:8]
                        print(f"  🚫 PERMANENT BLACKLIST: {pool_name} — {strikes} consecutive stop losses")
                    else:
                        idx = min(strikes - 1, len(config.STOP_LOSS_COOLDOWNS) - 1)
                        cooldown_sec = config.STOP_LOSS_COOLDOWNS[idx]
                        self._exit_cooldowns[amm_id] = (time.time(), cooldown_sec)
                        print(f"  🕐 Cooldown: {reason} exit (strike {strikes}/{config.PERMANENT_BLACKLIST_STRIKES}) — won't re-enter for {cooldown_sec // 3600}h")
                elif reason == "Take Profit":
                    # Take profit resets the strike counter
                    if amm_id in self._stop_loss_strikes:
                        print(f"  ✅ Take profit — reset stop-loss strikes for this pool")
                        del self._stop_loss_strikes[amm_id]
                # Persist state after exit
                self._save_state()
        return success

    @staticmethod
    def _usd(sol_amount: float, sol_price: float) -> str:
        """Format a SOL amount with USD equivalent in brackets."""
        if sol_price > 0:
            return f"{sol_amount:.4f} SOL (${sol_amount * sol_price:.2f})"
        return f"{sol_amount:.4f} SOL"

    def _retry_swap(self, pool_id: str, token_name: str, attempts: int = 3) -> bool:
        """Retry selling all of a token back to SOL. Returns True on success."""
        for attempt, delay in enumerate([0, 3, 5][:attempts], 1):
            if delay:
                time.sleep(delay)
            print(f"🔄 Swapping all {token_name} → SOL..." + (f" (attempt {attempt}/{attempts})" if attempt > 1 else ""))
            if self.executor.swap_tokens(pool_id=pool_id, amount_in=0, direction='sell'):
                return True
        return False

    def print_status(self):
        """Print bot status."""
        self._refresh_balance()  # Throttled: at most once per 60s
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
                # Price direction arrow
                price_chg = pos.price_change_percent
                price_arrow = "↑" if price_chg > 0.5 else "↓" if price_chg < -0.5 else "→"

                # P&L indicator
                pnl_pct = pos.pnl_percent
                pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < -0.5 else "⚪"

                # Time info
                hours_held = pos.time_held_hours
                time_left = max(0, config.MAX_HOLD_TIME_HOURS - hours_held)
                time_str = f"{hours_held:.1f}h" if hours_held < 1 else f"{hours_held:.0f}h"
                time_left_str = f"{time_left:.0f}h left" if time_left >= 1 else f"{time_left * 60:.0f}m left"

                # Format values
                entry_str = self._usd(pos.position_size_sol, sol_price)
                value_str = self._usd(pos.current_lp_value_sol, sol_price) if pos.current_lp_value_sol > 0 else "—"
                pnl_usd = f" (${pos.unrealized_pnl_sol * sol_price:.2f})" if sol_price > 0 else ""

                # Exit threshold proximity — show nearest trigger
                exit_warnings = []
                if pnl_pct <= config.STOP_LOSS_PERCENT + 1.0:
                    exit_warnings.append(f"SL {config.STOP_LOSS_PERCENT:.0f}%")
                if pnl_pct >= config.TAKE_PROFIT_PERCENT - 1.5:
                    exit_warnings.append(f"TP {config.TAKE_PROFIT_PERCENT:.0f}%")
                if pos.current_il_percent <= config.MAX_IMPERMANENT_LOSS + 1.0:
                    exit_warnings.append(f"IL lim {config.MAX_IMPERMANENT_LOSS:.0f}%")
                if time_left < 2:
                    exit_warnings.append("time lim")
                exit_str = f" ⚠ Near: {', '.join(exit_warnings)}" if exit_warnings else ""

                print(f"  {pnl_icon} {pos.pool_name}  {price_arrow} {price_chg:+.1f}%  |  {time_str} ({time_left_str})")
                # Use 4 decimal places for IL since it's very small for typical price moves
                # (a 5% price change = only 0.03% IL)
                il_str = f"{pos.current_il_percent:.4f}%" if abs(pos.current_il_percent) < 1.0 else f"{pos.current_il_percent:.2f}%"
                print(f"    Entry: {entry_str}  →  Value: {value_str}  |  P&L: {pos.unrealized_pnl_sol:+.4f} SOL{pnl_usd} ({pnl_pct:+.2f}%)  |  IL: {il_str}{exit_str}")

        print(f"{'─' * 60}\n")

    def _startup_position_check(self):
        """At startup, if there are restored positions, show live details
        and ask the user whether to close them or continue."""
        positions = self.position_manager.active_positions
        if not positions:
            return

        sol_price = self.api_client.get_sol_price_usd()

        print(f"\n{'=' * 60}")
        print(f"  EXISTING POSITIONS ({len(positions)})")
        print(f"{'=' * 60}")
        print(f"  Fetching live on-chain data...")

        # Fetch live data for each position
        total_entry = 0.0
        total_value = 0.0
        total_pnl = 0.0

        for i, (amm_id, pos) in enumerate(positions.items(), 1):
            lp_value = 0.0
            current_price = 0.0
            pool = {}
            lp_balance_raw = 0.0

            # Fresh pool data from API
            pool = self.api_client.get_pool_by_id(amm_id) or {}

            # On-chain LP token balance
            if pos.lp_mint and self.executor:
                lp_balance_raw = self.executor.get_token_balance(pos.lp_mint)

            # On-chain LP value and price from reserves
            if pos.lp_mint and pos.lp_token_amount > 0 and self.executor:
                data = self.executor.get_lp_value_sol(amm_id, pos.lp_mint)
                if data:
                    lp_value = data.get('valueSol', 0)
                    current_price = data.get('priceRatio', 0)

            # Fallback price from API
            if current_price <= 0 and pool:
                current_price = self.price_tracker.get_current_price(amm_id, pool)

            # Update metrics with live data
            if current_price > 0:
                pos.update_metrics(current_price, pool,
                                   lp_value_sol=lp_value if lp_value > 0 else None)

            pnl_sol = pos.unrealized_pnl_sol
            pnl_pct = pos.pnl_percent
            price_chg = pos.price_change_percent

            total_entry += pos.position_size_sol
            if lp_value > 0:
                total_value += lp_value
                total_pnl += pnl_sol

            price_arrow = "↑" if price_chg > 0.5 else "↓" if price_chg < -0.5 else "→"
            pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < -0.5 else "⚪"

            hours_held = pos.time_held_hours
            time_left = max(0, config.MAX_HOLD_TIME_HOURS - hours_held)
            if hours_held < 1:
                time_str = f"{hours_held * 60:.0f}m"
            elif hours_held < 24:
                time_str = f"{hours_held:.1f}h"
            else:
                time_str = f"{hours_held / 24:.1f}d"
            time_left_str = f"{time_left:.0f}h left" if time_left >= 1 else f"{time_left * 60:.0f}m left"

            il_pct = pos.current_il_percent
            il_str = f"{il_pct:.4f}%" if abs(il_pct) < 1.0 else f"{il_pct:.2f}%"

            day = pool.get('day', {})
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            tvl = pool.get('tvl', 0)

            lp_decimals = pos.lp_decimals if pos.lp_decimals > 0 else 9
            lp_human = lp_balance_raw / (10 ** lp_decimals) if lp_balance_raw > 0 else pos.lp_token_amount

            print(f"\n{'─' * 60}")
            print(f"  {pnl_icon} #{i}: {pos.pool_name}")
            print(f"{'─' * 60}")
            print(f"  Entry: {self._usd(pos.position_size_sol, sol_price)}")
            if lp_value > 0:
                print(f"  Value: {self._usd(lp_value, sol_price)}")
            else:
                print(f"  Value: — (could not fetch)")
            pnl_usd = f" (${pnl_sol * sol_price:.2f})" if sol_price > 0 and lp_value > 0 else ""
            print(f"  P&L:   {pnl_sol:+.4f} SOL{pnl_usd} ({pnl_pct:+.2f}%)")
            print(f"  IL:    {il_str}  |  Price: {price_arrow} {price_chg:+.1f}%")
            print(f"  Held:  {time_str} ({time_left_str})  |  LP: {lp_human:.6f}"
                  + ("" if lp_balance_raw > 0 else " ⚠ not found on-chain"))
            if apr > 0:
                print(f"  APR:   {apr:.1f}%" + (f"  |  TVL: ${tvl:,.0f}" if tvl > 0 else ""))

        # Summary
        print(f"\n{'═' * 60}")
        print(f"  {len(positions)} position(s)  |  Entry: {self._usd(total_entry, sol_price)}", end="")
        if total_value > 0:
            ret_pct = (total_pnl / total_entry * 100) if total_entry > 0 else 0
            print(f"  →  Value: {self._usd(total_value, sol_price)}  |  P&L: {total_pnl:+.4f} SOL ({ret_pct:+.2f}%)")
        else:
            print()
        print(f"  Wallet: {self._usd(self.available_capital, sol_price)}")
        print(f"{'═' * 60}")

        # Ask user what to do
        can_close = (self.executor and not config.DRY_RUN and config.TRADING_ENABLED)
        if can_close:
            print(f"\n  [Enter]    Continue with these positions")
            print(f"  [1 2 ...]  Close specific position(s) by number")
            print(f"  [all]      Close ALL positions")
        else:
            print(f"\n  Press Enter to continue...")

        try:
            answer = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n")
            sys.exit(0)

        if not answer or not can_close:
            print(f"  ✓ Continuing with {len(positions)} existing position(s)\n")
            return

        # Route to appropriate handler
        if answer in ('all', 'close'):
            # Double confirm for close all
            try:
                confirm = input(f"  ⚠ Close ALL {len(positions)} position(s)? Type 'confirm': ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Cancelled — continuing with existing positions\n")
                return

            if confirm != 'confirm':
                print(f"  ✓ Continuing with {len(positions)} existing position(s)\n")
                return

            # Close all positions
            print()
            closed = 0
            for amm_id, pos in list(positions.items()):
                print(f"  🔄 Closing {pos.pool_name}...")
                success = self._exit_position(amm_id, "Manual close (startup)")
                if success:
                    closed += 1
                else:
                    print(f"  ✗ Failed to close {pos.pool_name}")

            # Unwrap any remaining WSOL
            if self.executor:
                unwrapped = self.executor.unwrap_wsol()
                if unwrapped > 0:
                    print(f"  ✓ Unwrapped {unwrapped:.4f} WSOL")

            # Wait for on-chain state to settle before reading balance
            time.sleep(3)
            self._refresh_balance(force=True)
            print(f"\n  ✓ Closed {closed} position(s)  |  Balance: {self._usd(self.available_capital, sol_price)}\n")
        else:
            # Parse as specific position numbers
            self._close_specific_positions(positions, sol_price)

    def _close_specific_positions(self, positions: dict, sol_price: float):
        """Interactively close specific positions by number."""
        print("\n  Options:")
        print("    • Type position numbers to close (e.g., '1,3' or '1 3')")
        print("    • Type 'all' to close all positions")
        print("    • Press Enter to skip\n")

        try:
            response = input("  → ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled\n")
            return

        if not response:
            print(f"  ✓ Continuing with {len(positions)} existing position(s)\n")
            return

        # Parse selection
        to_close = []
        pos_list = list(positions.items())

        if response == 'all':
            to_close = pos_list
        else:
            # Parse comma or space-separated numbers
            parts = response.replace(',', ' ').split()
            for part in parts:
                try:
                    idx = int(part) - 1  # user sees 1-indexed
                    if 0 <= idx < len(pos_list):
                        to_close.append(pos_list[idx])
                    else:
                        print(f"  ⚠ Invalid position number: {part}")
                except ValueError:
                    print(f"  ⚠ Invalid input: {part}")

        if not to_close:
            print(f"  ✓ Continuing with {len(positions)} existing position(s)\n")
            return

        # Confirm
        print(f"\n  → Closing {len(to_close)} position(s):")
        for amm_id, pos in to_close:
            print(f"     • {pos.pool_name}")
        print()

        try:
            confirm = input(f"  ⚠ Confirm close? Type 'yes': ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled\n")
            return

        if confirm != 'yes':
            print(f"  ✓ Continuing with {len(positions)} existing position(s)\n")
            return

        # Close selected positions
        print()
        closed = 0
        for amm_id, pos in to_close:
            print(f"  🔄 Closing {pos.pool_name}...")
            success = self._exit_position(amm_id, "Manual close (startup)")
            if success:
                closed += 1
            else:
                print(f"  ✗ Failed to close {pos.pool_name}")

        # Unwrap any remaining WSOL
        if self.executor:
            unwrapped = self.executor.unwrap_wsol()
            if unwrapped > 0:
                print(f"  ✓ Unwrapped {unwrapped:.4f} WSOL")

        # Wait for on-chain state to settle before reading balance
        time.sleep(3)
        self._refresh_balance(force=True)
        remaining = len(positions) - closed
        print(f"\n  ✓ Closed {closed} position(s)  |  {remaining} remaining  |  Balance: {self._usd(self.available_capital, sol_price)}\n")

    def run(self):
        """Main bot loop — spawns worker threads and runs display in the main thread."""
        self.running = True

        # Show existing positions and let user close them before starting
        self._startup_position_check()

        # Register signal handlers for graceful shutdown
        def _signal_handler(sig, frame):
            print(f"\n\n⚠ Signal received ({sig})...")
            self.shutdown()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        print("\n🚀 Starting bot (threaded)...")
        print("  • Position check thread: every %ds" % config.POSITION_CHECK_INTERVAL_SEC)
        print("  • Pool scan thread: every %ds" % config.POOL_SCAN_INTERVAL_SEC)
        print("  • Buy worker thread: sequential queue")
        print("  • Main thread: display every %ds" % config.DISPLAY_INTERVAL_SEC)
        print("Press Ctrl+C to stop\n")

        # Start worker threads (all daemon so they die with main thread)
        self._threads['position_check'] = threading.Thread(
            target=self._position_check_loop, name='position-check', daemon=True)
        self._threads['pool_scan'] = threading.Thread(
            target=self._pool_scan_loop, name='pool-scan', daemon=True)
        self._threads['buy_worker'] = threading.Thread(
            target=self._buy_worker_loop, name='buy-worker', daemon=True)

        for t in self._threads.values():
            t.start()

        # Main thread: display only
        try:
            while self.running:
                current_time = time.time()
                if current_time - self.last_status_print >= config.DISPLAY_INTERVAL_SEC:
                    self.print_status()
                    self.last_status_print = current_time
                time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n\n⚠ Shutdown signal received...")
            self.shutdown()

    # ── Thread workers ──────────────────────────────────────────────

    def _position_check_loop(self):
        """Worker thread: check positions every 1s and trigger exits."""
        while self.running:
            try:
                exits = []
                with self._state_lock:
                    if self.position_manager.active_positions:
                        self.update_positions()
                        exits = self.position_manager.check_exit_conditions()

                if exits:
                    self._execute_exits_parallel(exits)

            except Exception as e:
                print(f"⚠ Position check error: {e}")

            time.sleep(1)

    def _pool_scan_loop(self):
        """Worker thread: scan for pools and queue buy orders."""
        while self.running:
            try:
                current_time = time.time()
                if current_time - self.last_pool_scan >= config.POOL_SCAN_INTERVAL_SEC:
                    top_pools = self.scan_and_rank_pools()
                    self.last_pool_scan = current_time

                    with self._state_lock:
                        self._failed_pools.clear()

                    if top_pools:
                        self._queue_new_entries(top_pools)

            except Exception as e:
                print(f"⚠ Pool scan error: {e}")

            time.sleep(1)

    def _buy_worker_loop(self):
        """Worker thread: execute buy orders sequentially from the queue."""
        while self.running:
            try:
                # Block with timeout so we can check self.running
                pool = self._buy_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._execute_single_entry(pool)
            except Exception as e:
                print(f"⚠ Buy worker error for {pool.get('name', '?')}: {e}")
            finally:
                self._buy_queue.task_done()

    def _execute_exits_parallel(self, exits: list):
        """Sell all triggered positions in parallel."""
        if not exits:
            return

        self._selling.set()
        try:
            if len(exits) == 1:
                # Single exit — no need for thread pool overhead
                amm_id, reason = exits[0]
                self._exit_position(amm_id, reason)
            else:
                print(f"\n⚡ Executing {len(exits)} exits in parallel...")
                with ThreadPoolExecutor(max_workers=len(exits)) as executor:
                    futures = {
                        executor.submit(self._exit_position, amm_id, reason): (amm_id, reason)
                        for amm_id, reason in exits
                    }
                    for future in as_completed(futures):
                        amm_id, reason = futures[future]
                        try:
                            success = future.result()
                            if not success:
                                pos = self.position_manager.active_positions.get(amm_id)
                                name = pos.pool_name if pos else amm_id[:8]
                                print(f"  ✗ Failed to exit {name}")
                        except Exception as e:
                            print(f"  ✗ Exit error for {amm_id[:8]}: {e}")
        finally:
            self._selling.clear()

    def _queue_new_entries(self, top_pools: list):
        """Evaluate pools and queue buy orders (called from scan thread)."""
        self._refresh_balance(force=True)

        with self._state_lock:
            if not self.position_manager.can_open_position(self.available_capital):
                return

            initial_balance = self.available_capital
            committed = 0.0

            active_amm_ids = set(self.position_manager.active_positions.keys())
            failed_amm_ids = self._failed_pools.copy()

            # Expire old cooldowns
            now = time.time()
            expired = [k for k, (ts, dur) in self._exit_cooldowns.items() if now - ts > dur]
            for k in expired:
                del self._exit_cooldowns[k]
            cooldown_ids = set(self._exit_cooldowns.keys())
            blacklist = self._permanent_blacklist.copy()

        # Also exclude anything already queued
        queued_ids = set()
        for item in list(self._buy_queue.queue):
            queued_ids.add(item.get('ammId', ''))

        available_pools = [
            p for p in top_pools
            if p['ammId'] not in active_amm_ids
            and p['ammId'] not in failed_amm_ids
            and p['ammId'] not in cooldown_ids
            and p['ammId'] not in blacklist
            and p['ammId'] not in queued_ids
        ]

        if not available_pools:
            return

        deployable = initial_balance - config.RESERVE_SOL
        if deployable < config.MIN_POSITION_SOL:
            return

        for rank, pool in enumerate(available_pools):
            tracked_capital = initial_balance - committed

            with self._state_lock:
                if not self.position_manager.can_open_position(tracked_capital):
                    break

            if tracked_capital <= config.RESERVE_SOL:
                break

            if pool['score'] < 50:
                continue

            # Pre-compute position sizing so the buy worker has it
            pool['_entry_meta'] = {
                'tracked_capital': tracked_capital,
                'initial_balance': initial_balance,
                'rank': rank,
                'total_ranked': len(available_pools),
            }

            self._buy_queue.put(pool)
            committed += (tracked_capital - config.RESERVE_SOL) / max(1,
                config.MAX_CONCURRENT_POSITIONS - len(active_amm_ids) - rank)

    def _execute_single_entry(self, pool: dict):
        """Execute a single buy (called sequentially by the buy worker thread)."""
        meta = pool.pop('_entry_meta', {})
        tracked_capital = meta.get('tracked_capital', self.available_capital)
        initial_balance = meta.get('initial_balance', self.available_capital)
        rank = meta.get('rank', 0)
        total_ranked = meta.get('total_ranked', 1)

        # Re-check that we can still open
        with self._state_lock:
            self._refresh_balance(force=True)
            if not self.position_manager.can_open_position(self.available_capital):
                return
            tracked_capital = self.available_capital

        amm_id = pool['ammId']

        print(f"\n💡 Entry opportunity: {pool['name']} "
              f"(score: {pool['score']:.1f}, rank #{rank + 1}/{total_ranked})")

        current_price = self.price_tracker.get_current_price(amm_id, pool)
        if current_price <= 0:
            print(f"⚠ Could not get valid price for {pool['name']}, skipping")
            return

        with self._state_lock:
            position = self.position_manager.open_position(
                pool,
                tracked_capital,
                current_price,
                sol_price_usd=self.api_client.get_sol_price_usd(),
                total_wallet_balance=initial_balance,
                rank=rank,
                total_ranked=total_ranked,
            )
            if not position:
                return

        if not config.DRY_RUN and self.executor:
            def _rollback(sell_back=False):
                """Close the position, mark pool as failed, refresh balance."""
                if sell_back:
                    self.executor.swap_tokens(pool_id=amm_id, amount_in=0, direction='sell')
                with self._state_lock:
                    self.position_manager.close_position(amm_id)
                    self._failed_pools.add(amm_id)
                self._refresh_balance(force=True)

            # Step 1: Swap half the SOL into the other token
            token_name = pool.get('name', '').replace('WSOL/', '').replace('/WSOL', '')
            print(f"\n🔄 Swapping {position.sol_amount:.6f} SOL → {token_name}...")
            swap_sig = self.executor.swap_tokens(
                pool_id=amm_id, amount_in=position.sol_amount, direction='buy',
            )
            if not swap_sig:
                print(f"✗ Swap failed - removing position, trying next pool")
                _rollback()
                return

            time.sleep(2)

            # Step 2: Add liquidity with both tokens
            print(f"🔄 Executing add liquidity transaction...")
            add_result = self.executor.add_liquidity(
                pool_id=amm_id,
                token_a_amount=position.token_a_amount,
                token_b_amount=position.token_b_amount,
            )
            if not add_result:
                print(f"✗ Transaction failed - swapping back to SOL")
                _rollback(sell_back=True)
                return

            position.pool_data['entry_signature'] = add_result['signature']

            # Track LP token balance
            lp_mint = add_result.get('lpMint', '')
            if lp_mint:
                position.lp_mint = lp_mint
                lp_decimals = pool.get('lpDecimals', 9)
                position.lp_decimals = lp_decimals

                lp_raw = 0
                for attempt, delay in enumerate([2, 3, 5], 1):
                    time.sleep(delay)
                    lp_raw = self.executor.get_token_balance(lp_mint)
                    if lp_raw > 0:
                        break
                    print(f"  ⏳ LP balance not yet visible (attempt {attempt}/3)...")

                if lp_raw > 0:
                    position.lp_token_amount = lp_raw / (10 ** lp_decimals)
                    print(f"  LP tokens received: {position.lp_token_amount:.6f} (mint: {lp_mint[:8]}...)")
                else:
                    print(f"  ✗ LP tokens not found on-chain after add — rolling back")
                    _rollback(sell_back=True)
                    return

        self._refresh_balance(force=True)
        with self._state_lock:
            self._save_state()

    def shutdown(self):
        """Graceful shutdown — stop all threads, save state.
        Positions stay open on-chain and will be resumed on next run."""
        if self._shutting_down:
            print("\n⚠ Already shutting down... please wait")
            return
        self._shutting_down = True

        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        print("\nShutting down bot...")
        self.running = False

        # Wait for worker threads to finish (daemon threads, but be polite)
        for name, t in self._threads.items():
            t.join(timeout=5)
            if t.is_alive():
                print(f"  ⚠ Thread '{name}' did not stop in time")

        # Drain buy queue
        while not self._buy_queue.empty():
            try:
                self._buy_queue.get_nowait()
            except queue.Empty:
                break

        # Always save state — preserves cooldowns, snapshots, scan history
        with self._state_lock:
            self._save_state()
        n = len(self.position_manager.active_positions)
        if n > 0:
            names = [p.pool_name for p in self.position_manager.active_positions.values()]
            print(f"\n💾 Saved {n} active position(s) to disk: {', '.join(names)}")
            print(f"   Positions remain open on-chain — will resume on next run")
        else:
            print(f"\n💾 State saved (cooldowns, snapshots, scan history)")

        self.print_status()
        print("✓ Bot stopped")
        sys.exit(0)


def main():
    """Entry point."""
    bot = LiquidityBot()
    bot.run()


if __name__ == "__main__":
    main()
