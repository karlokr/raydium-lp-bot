#!/usr/bin/env python3
"""
Position Manager — View & Close Active Positions

Shows live details of all active LP positions (fresh on-chain data, not cached)
and optionally closes them all.

Usage:
    cd /path/to/raydium-lp-bot
    set -a && source .env && set +a
    .venv/bin/python tests/manage_positions.py
"""
import sys
import os
import time

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bot.config import config
from bot.raydium_client import RaydiumAPIClient
from bot.trading.executor import RaydiumExecutor
from bot.trading.position_manager import Position
from bot.analysis.price_tracker import PriceTracker
from bot import state


def sol_usd(sol_amount: float, sol_price: float) -> str:
    """Format a SOL amount with USD equivalent."""
    if sol_price > 0:
        return f"{sol_amount:.4f} SOL (${sol_amount * sol_price:.2f})"
    return f"{sol_amount:.4f} SOL"


def fetch_live_position_data(positions: dict, executor: RaydiumExecutor,
                              api_client: RaydiumAPIClient,
                              price_tracker: PriceTracker) -> dict:
    """Fetch fresh on-chain data for all positions.

    Returns dict of amm_id -> {
        'lp_value_sol': float,  # on-chain LP value
        'price_ratio': float,   # on-chain price
        'pool_data': dict,      # fresh API pool data
        'lp_balance_raw': float,# actual LP token balance on-chain
    }
    """
    live = {}
    for amm_id, pos in positions.items():
        entry = {
            'lp_value_sol': 0.0,
            'price_ratio': 0.0,
            'pool_data': {},
            'lp_balance_raw': 0.0,
        }

        # Fresh pool data from API
        pool = api_client.get_pool_by_id(amm_id)
        if pool:
            entry['pool_data'] = pool

        # On-chain LP token balance
        if pos.lp_mint:
            raw_balance = executor.get_token_balance(pos.lp_mint)
            entry['lp_balance_raw'] = raw_balance

        # On-chain LP value and price from reserves
        if pos.lp_mint and pos.lp_token_amount > 0:
            data = executor.get_lp_value_sol(amm_id, pos.lp_mint)
            if data:
                entry['lp_value_sol'] = data.get('valueSol', 0)
                entry['price_ratio'] = data.get('priceRatio', 0)

        # Fallback price from API
        if entry['price_ratio'] <= 0 and pool:
            entry['price_ratio'] = price_tracker.get_current_price(amm_id, pool)

        live[amm_id] = entry

    return live


def display_positions(positions: dict, live_data: dict, sol_price: float):
    """Display detailed position info with live data."""
    total_entry = 0.0
    total_value = 0.0
    total_pnl = 0.0

    for i, (amm_id, pos) in enumerate(positions.items(), 1):
        data = live_data.get(amm_id, {})
        lp_value = data['lp_value_sol']
        current_price = data['price_ratio']
        pool = data.get('pool_data', {})
        lp_balance_raw = data['lp_balance_raw']

        # Update metrics with live data
        if current_price > 0:
            pos.update_metrics(current_price, pool, lp_value_sol=lp_value if lp_value > 0 else None)

        # Compute PnL
        pnl_sol = pos.unrealized_pnl_sol
        pnl_pct = pos.pnl_percent
        price_chg = pos.price_change_percent

        total_entry += pos.position_size_sol
        if lp_value > 0:
            total_value += lp_value
            total_pnl += pnl_sol

        # Price direction
        price_arrow = "↑" if price_chg > 0.5 else "↓" if price_chg < -0.5 else "→"

        # PnL icon
        pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < -0.5 else "⚪"

        # Time info
        hours_held = pos.time_held_hours
        time_left = max(0, config.MAX_HOLD_TIME_HOURS - hours_held)
        if hours_held < 1:
            time_str = f"{hours_held * 60:.0f}m"
        elif hours_held < 24:
            time_str = f"{hours_held:.1f}h"
        else:
            time_str = f"{hours_held / 24:.1f}d"

        if time_left >= 1:
            time_left_str = f"{time_left:.0f}h left"
        else:
            time_left_str = f"{time_left * 60:.0f}m left"

        # IL
        il_pct = pos.current_il_percent
        il_str = f"{il_pct:.4f}%" if abs(il_pct) < 1.0 else f"{il_pct:.2f}%"

        # Pool APR from API
        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        tvl = pool.get('tvl', 0)

        # LP token info
        lp_decimals = pos.lp_decimals if pos.lp_decimals > 0 else 9
        lp_human = lp_balance_raw / (10 ** lp_decimals) if lp_balance_raw > 0 else pos.lp_token_amount

        print(f"\n{'─' * 60}")
        print(f"  {pnl_icon} Position #{i}: {pos.pool_name}")
        print(f"{'─' * 60}")
        print(f"  Pool ID:     {amm_id}")
        print(f"  LP Mint:     {pos.lp_mint or '(not set)'}")
        print(f"  LP Tokens:   {lp_human:.6f}" +
              (f"  (on-chain: {lp_balance_raw:.0f} raw)" if lp_balance_raw > 0 else "  ⚠ no LP tokens found on-chain"))
        print()
        print(f"  Entry:       {sol_usd(pos.position_size_sol, sol_price)}")
        if lp_value > 0:
            print(f"  Value Now:   {sol_usd(lp_value, sol_price)}")
        else:
            print(f"  Value Now:   — (could not fetch)")
        pnl_usd = f" (${pnl_sol * sol_price:.2f})" if sol_price > 0 and lp_value > 0 else ""
        print(f"  P&L:         {pnl_sol:+.4f} SOL{pnl_usd} ({pnl_pct:+.2f}%)")
        print(f"  IL:          {il_str}")
        print(f"  Fees Est:    {sol_usd(pos.fees_earned_sol, sol_price)}")
        print()
        print(f"  Price:       {price_arrow} {price_chg:+.1f}% since entry")
        print(f"  Entry Price: {pos.entry_price_ratio:.10f}")
        print(f"  Now:         {current_price:.10f}" if current_price > 0 else "  Now:         — (unavailable)")
        print()
        print(f"  Held:        {time_str} ({time_left_str})")
        print(f"  Entered:     {pos.entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if tvl > 0:
            print(f"  Pool TVL:    ${tvl:,.0f}")
        if volume > 0:
            print(f"  24h Volume:  ${volume:,.0f}")
        if apr > 0:
            print(f"  24h APR:     {apr:.1f}%")

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  TOTAL")
    print(f"{'═' * 60}")
    print(f"  Positions:    {len(positions)}")
    print(f"  Entry Cost:   {sol_usd(total_entry, sol_price)}")
    if total_value > 0:
        print(f"  Current Val:  {sol_usd(total_value, sol_price)}")
        print(f"  Total P&L:    {total_pnl:+.4f} SOL" +
              (f" (${total_pnl * sol_price:.2f})" if sol_price > 0 else ""))
        if total_entry > 0:
            print(f"  Return:       {(total_pnl / total_entry) * 100:+.2f}%")
    print()


def close_all_positions(positions: dict, executor: RaydiumExecutor,
                         api_client: RaydiumAPIClient):
    """Close all positions: removeLiquidity → swap → unwrap."""
    sol_price = api_client.get_sol_price_usd()
    closed = 0
    failed = 0

    for amm_id, pos in list(positions.items()):
        print(f"\n{'─' * 60}")
        print(f"  Closing: {pos.pool_name}")
        print(f"{'─' * 60}")

        # Step 1: Remove liquidity
        lp_amount = pos.lp_token_amount
        if lp_amount <= 0 and pos.lp_mint:
            lp_amount_raw = executor.get_token_balance(pos.lp_mint)
            if lp_amount_raw > 0:
                lp_decimals = pos.lp_decimals if pos.lp_decimals > 0 else 9
                lp_amount = lp_amount_raw / (10 ** lp_decimals)

        if lp_amount > 0:
            print(f"  🔄 Removing liquidity ({lp_amount:.6f} LP tokens)...")
            sig = executor.remove_liquidity(pool_id=amm_id, lp_token_amount=lp_amount)
            if not sig:
                print(f"  ✗ Remove liquidity FAILED — skipping this position")
                failed += 1
                continue
            print(f"  ✓ Liquidity removed: {sig}")
        else:
            print(f"  ⚠ No LP tokens to remove")

        # Step 2: Swap remaining tokens back to SOL
        time.sleep(2)
        token_name = pos.pool_name.replace('WSOL/', '').replace('/WSOL', '')
        print(f"  🔄 Swapping {token_name} → SOL...")
        executor.swap_tokens(pool_id=amm_id, amount_in=0, direction='sell')

        # Record trade history
        state.append_trade_history(pos, "Manual close", sol_price_usd=sol_price)
        closed += 1

        time.sleep(1)

    # Step 3: Unwrap any WSOL
    print(f"\n🔄 Unwrapping WSOL → native SOL...")
    unwrapped = executor.unwrap_wsol()
    if unwrapped > 0:
        print(f"✓ Unwrapped {unwrapped:.4f} WSOL → native SOL")

    # Clear positions from saved state
    if closed > 0:
        saved = state.load_state()
        if saved:
            state.save_state(
                positions={},
                exit_cooldowns=saved.get('exit_cooldowns', {}),
                failed_pools=set(saved.get('failed_pools', [])),
            )
        else:
            pass  # State file kept for cooldowns/snapshots

    # Final balance
    final_balance = executor.get_balance()
    sol_price = api_client.get_sol_price_usd()

    print(f"\n{'═' * 60}")
    print(f"  DONE")
    print(f"{'═' * 60}")
    print(f"  Closed:   {closed} position(s)")
    if failed > 0:
        print(f"  Failed:   {failed} position(s)")
    print(f"  Balance:  {sol_usd(final_balance, sol_price)}")
    print()


def main():
    print("=" * 60)
    print("  POSITION MANAGER")
    print("=" * 60)

    # Load saved state
    saved = state.load_state()
    if not saved or not saved.get('positions'):
        print("\n  No active positions found.\n")
        return

    positions = saved['positions']
    age_sec = time.time() - saved.get('saved_timestamp', 0)
    if age_sec < 60:
        age_str = f"{age_sec:.0f}s ago"
    elif age_sec < 3600:
        age_str = f"{age_sec / 60:.0f}m ago"
    else:
        age_str = f"{age_sec / 3600:.1f}h ago"

    print(f"\n  Found {len(positions)} active position(s)")
    print(f"  State saved: {saved.get('saved_at', '?')} ({age_str})")

    # Initialize components for live data
    print(f"\n  Fetching live on-chain data...\n")
    api_client = RaydiumAPIClient()
    sol_price = api_client.get_sol_price_usd()

    try:
        executor = RaydiumExecutor()
    except Exception as e:
        print(f"  ⚠ Could not initialize executor: {e}")
        print(f"  Showing cached data only.\n")
        # Show cached data without live refresh
        for i, (amm_id, pos) in enumerate(positions.items(), 1):
            print(f"  {i}. {pos.pool_name}")
            print(f"     Entry: {sol_usd(pos.position_size_sol, sol_price)}")
            print(f"     Held:  {pos.time_held_hours:.1f}h")
            print(f"     AMM:   {amm_id}")
            print()
        return

    price_tracker = PriceTracker(api_client)

    # Fetch live data
    live_data = fetch_live_position_data(positions, executor, api_client, price_tracker)

    # Display positions with live data
    display_positions(positions, live_data, sol_price)

    # Show wallet balance
    balance = executor.get_balance()
    print(f"  Wallet:       {sol_usd(balance, sol_price)}")
    print()

    # Ask user if they want to close
    if config.DRY_RUN:
        print("  ⚠ DRY RUN mode — cannot close positions.")
        return

    if not config.TRADING_ENABLED:
        print("  ⚠ Trading disabled (TRADING_ENABLED=False) — cannot close positions.")
        return

    try:
        answer = input("  Close ALL positions? (yes/no): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return

    if answer not in ('yes', 'y'):
        print("\n  No changes made.\n")
        return

    # Double confirm
    try:
        confirm = input(f"  ⚠ This will close {len(positions)} position(s) on-chain. Type 'confirm' to proceed: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.\n")
        return

    if confirm != 'confirm':
        print("\n  Aborted.\n")
        return

    print()
    close_all_positions(positions, executor, api_client)


if __name__ == "__main__":
    main()
