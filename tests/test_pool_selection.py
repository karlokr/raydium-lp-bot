#!/usr/bin/env python3
"""
End-to-end pool selection test

Runs the full bot pipeline — fetch pools, apply all filters (burn, RugCheck,
LP lock), score, rank — and shows exactly what pools would be selected.

Usage:
    cd /path/to/raydium-lp-bot
    set -a && source .env && set +a
    .venv/bin/python tests/test_pool_selection.py
    .venv/bin/python tests/test_pool_selection.py --verbose    # show LP lock details
    .venv/bin/python tests/test_pool_selection.py --no-lp-lock # skip LP lock check
"""
import sys
import os
import time
import argparse

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bot.config import config
from bot.raydium_client import RaydiumAPIClient
from bot.analysis.pool_analyzer import PoolAnalyzer
from bot.analysis.pool_quality import PoolQualityAnalyzer


def main():
    parser = argparse.ArgumentParser(description="Test pool selection pipeline")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show LP lock breakdown for every pool")
    parser.add_argument("--no-lp-lock", action="store_true",
                        help="Skip LP lock check (faster)")
    parser.add_argument("--no-rugcheck", action="store_true",
                        help="Skip RugCheck + LP lock (fastest, just API filters)")
    args = parser.parse_args()

    check_safety = not args.no_rugcheck
    check_lp_lock_orig = config.CHECK_LP_LOCK

    if args.no_lp_lock:
        config.CHECK_LP_LOCK = False

    print("=" * 70)
    print("END-TO-END POOL SELECTION TEST")
    print("=" * 70)
    print(f"Config:")
    print(f"  MIN_LIQUIDITY_USD:       ${config.MIN_LIQUIDITY_USD:,.0f}")
    print(f"  MIN_VOLUME_TVL_RATIO:    {config.MIN_VOLUME_TVL_RATIO}")
    print(f"  MIN_APR_24H:             {config.MIN_APR_24H}%")
    print(f"  MIN_BURN_PERCENT:        {config.MIN_BURN_PERCENT}%")
    print(f"  CHECK_TOKEN_SAFETY:      {check_safety}")
    print(f"  CHECK_LP_LOCK:           {config.CHECK_LP_LOCK}")
    if config.CHECK_LP_LOCK:
        print(f"  MIN_SAFE_LP_PERCENT:     {config.MIN_SAFE_LP_PERCENT}%")
        print(f"  MAX_SINGLE_LP_HOLDER:    {config.MAX_SINGLE_LP_HOLDER_PERCENT}%")
    print(f"  MAX_RUGCHECK_SCORE:      {config.MAX_RUGCHECK_SCORE}")
    print(f"  MAX_TOP10_HOLDER_PERCENT:{config.MAX_TOP10_HOLDER_PERCENT}%")
    print(f"  MAX_SINGLE_HOLDER_PERCENT:{config.MAX_SINGLE_HOLDER_PERCENT}%")
    print(f"  MIN_TOKEN_HOLDERS:       {config.MIN_TOKEN_HOLDERS}")
    print()

    t0 = time.time()

    # Step 1: Fetch pools
    print("[1/4] Fetching pools from Raydium API...")
    client = RaydiumAPIClient()
    pools = client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
    )
    print(f"  → {len(pools)} pools pass basic filters (TVL/vol/APR)")

    # Step 2: Burn filter
    print("[2/4] Applying burn filter...")
    pools = [p for p in pools if p.get('burnPercent', 0) >= config.MIN_BURN_PERCENT]
    print(f"  → {len(pools)} pools pass burn >= {config.MIN_BURN_PERCENT}%")

    if not pools:
        print("\n⚠ No pools passed basic filters + burn. Nothing to check.")
        return

    # Step 3: Safety check
    if check_safety:
        checks = "RugCheck"
        if config.CHECK_LP_LOCK:
            checks += " + LP Lock"
        print(f"[3/4] Running safety analysis ({checks})...")
        print(f"  Checking {len(pools)} pools — may take a moment...")
        sys.stdout.flush()
    else:
        print("[3/4] Safety checks SKIPPED (--no-rugcheck)")

    analyzer = PoolQualityAnalyzer()
    safe_pools = []
    rejected = []

    for i, pool in enumerate(pools):
        name = pool.get('name', '?')
        burn = pool.get('burnPercent', 0)
        pool_id = pool.get('ammId', pool.get('id', ''))

        analysis = analyzer.analyze_pool(pool, check_safety=check_safety)

        if analysis['is_safe']:
            safe_pools.append(pool)
            print(f"  ✓ [{i+1}/{len(pools)}] {name} — SAFE (burn={burn:.0f}%)")
        else:
            first_risk = analysis['risks'][0] if analysis['risks'] else '?'
            rejected.append((name, burn, analysis))
            print(f"  ✗ [{i+1}/{len(pools)}] {name} — REJECTED: {first_risk[:80]}")

        # Verbose: show LP lock details
        if args.verbose:
            lp_lock = analysis.get('lp_lock')
            if lp_lock and lp_lock.get('available'):
                remaining_frac = (100 - burn) / 100
                eff_safe = burn + lp_lock['safe_pct'] * remaining_frac
                max_pull = lp_lock['max_single_unlocked_pct'] * remaining_frac
                print(f"      LP Lock: burned_chain={lp_lock['burned_pct']:.1f}% "
                      f"protocol={lp_lock['protocol_locked_pct']:.1f}% "
                      f"contract={lp_lock['contract_locked_pct']:.1f}% "
                      f"unlocked={lp_lock['unlocked_pct']:.1f}%")
                print(f"      effective_safe={eff_safe:.1f}%  max_pullable={max_pull:.2f}%")
            elif lp_lock:
                print(f"      LP Lock: unavailable")

        sys.stdout.flush()

    print(f"  → {len(safe_pools)} safe, {len(rejected)} rejected")

    # Step 4: Rank
    print("[4/4] Scoring and ranking safe pools...")
    ranker = PoolAnalyzer()
    top_pools = ranker.rank_pools(safe_pools, top_n=10)

    elapsed = time.time() - t0

    print()
    print("=" * 70)
    if top_pools:
        print(f"RESULTS: {len(top_pools)} pools would be selected for entry")
    else:
        print("RESULTS: ⚠ NO POOLS PASSED ALL FILTERS")
    print("=" * 70)

    if top_pools:
        print(f"{'#':>3} {'Pool':<22} {'Score':>6} {'TVL':>12} {'Vol 24h':>12} "
              f"{'APR':>7} {'Burn':>5}")
        print("-" * 70)
        for i, p in enumerate(top_pools):
            day = p.get('day', {})
            tvl = p.get('tvl', 0)
            vol = day.get('volume', 0) or p.get('volume24h', 0)
            apr = day.get('apr', 0) or p.get('apr24h', 0)
            burn = p.get('burnPercent', 0)
            score = p.get('score', 0)
            print(f"{i+1:>3}. {p['name']:<22} {score:>5.1f} "
                  f"${tvl:>10,.0f} ${vol:>10,.0f} "
                  f"{apr:>6.0f}% {burn:>4.0f}%")
    else:
        print()
        # Group rejections by reason category
        from collections import Counter
        reasons = Counter()
        for name, burn, analysis in rejected:
            for risk in analysis['risks']:
                if 'RugCheck DANGER' in risk:
                    reasons['RugCheck DANGER'] += 1
                elif 'RugCheck' in risk and 'score' in risk:
                    reasons['RugCheck score too high'] += 1
                elif 'LP whale' in risk or 'LP is safe' in risk:
                    reasons['LP Lock risk'] += 1
                elif 'holder' in risk.lower() and 'top 10' in risk.lower():
                    reasons['Top 10 holder concentration'] += 1
                elif 'holder' in risk.lower() and 'single' in risk.lower():
                    reasons['Single holder whale'] += 1
                elif 'mutable metadata' in risk.lower():
                    reasons['Mutable metadata'] += 1
                elif 'LP providers' in risk.lower():
                    reasons['Few LP providers'] += 1
                elif 'freeze' in risk.lower():
                    reasons['Freeze authority'] += 1
                elif 'mint authority' in risk.lower():
                    reasons['Mint authority'] += 1
                elif 'burn' in risk.lower():
                    reasons['Low burn'] += 1
                elif 'few holders' in risk.lower():
                    reasons['Few token holders'] += 1
                else:
                    reasons[risk[:50]] += 1

        print("Rejection breakdown (one pool can trigger multiple):")
        for reason, count in reasons.most_common():
            print(f"  {count:>3}× {reason}")

    print(f"\nCompleted in {elapsed:.1f}s")

    # Restore config
    config.CHECK_LP_LOCK = check_lp_lock_orig


if __name__ == "__main__":
    main()
