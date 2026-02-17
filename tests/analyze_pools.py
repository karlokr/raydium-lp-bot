#!/usr/bin/env python3
"""
Analyze pools through the full safety pipeline and show results.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import config
from bot.raydium_client import RaydiumAPIClient
from bot.analysis.pool_quality import PoolQualityAnalyzer
from bot.analysis.pool_analyzer import PoolAnalyzer
from bot.analysis.snapshot_tracker import SnapshotTracker
from bot.state import load_state, snapshots_from_dict

def main():
    print("=" * 80)
    print("Pool Safety Analysis Pipeline")
    print("=" * 80)
    print(f"\nConfig:")
    print(f"  MIN_LIQUIDITY_USD:             ${config.MIN_LIQUIDITY_USD:,.0f}")
    print(f"  MIN_VOLUME_TVL_RATIO:          {config.MIN_VOLUME_TVL_RATIO}")
    print(f"  MIN_APR_24H:                   {config.MIN_APR_24H}%")
    print(f"  MIN_PREDICTED_NET_APR:         {config.MIN_PREDICTED_NET_APR}%")
    print(f"  MIN_BURN_PERCENT:              {config.MIN_BURN_PERCENT}%")
    print(f"  MIN_LP_LOCK_PERCENT:           {config.MIN_LP_LOCK_PERCENT}%")
    print(f"  MIN_SAFE_LP_PERCENT:           {config.MIN_SAFE_LP_PERCENT}%")
    print(f"  MAX_RUGCHECK_SCORE:            {config.MAX_RUGCHECK_SCORE}")
    print(f"  MAX_TOP10_HOLDER_PERCENT:      {config.MAX_TOP10_HOLDER_PERCENT}%")
    print(f"  MAX_SINGLE_HOLDER_PERCENT:     {config.MAX_SINGLE_HOLDER_PERCENT}%")
    print(f"  CHECK_TOKEN_SAFETY:            {config.CHECK_TOKEN_SAFETY}")
    print(f"  CHECK_LP_LOCK:                 {config.CHECK_LP_LOCK}")
    
    print("\n" + "=" * 80)
    print("Step 1: Fetch pools from Raydium API")
    print("=" * 80)
    
    api_client = RaydiumAPIClient()
    pools = api_client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
    )
    
    print(f"✓ Fetched {len(pools)} pools meeting basic criteria")
    
    print("\n" + "=" * 80)
    print("Step 2: Filter by LP burn percent")
    print("=" * 80)
    
    pre_burn = len(pools)
    pools = [p for p in pools if p.get('burnPercent', 0) >= config.MIN_BURN_PERCENT]
    print(f"✓ {len(pools)} pools with burn ≥ {config.MIN_BURN_PERCENT}% (rejected {pre_burn - len(pools)})")
    
    print("\n" + "=" * 80)
    print("Step 3: Safety analysis (RugCheck + LP Lock)")
    print("=" * 80)
    
    # Load snapshot tracker from saved state
    snapshot_tracker = SnapshotTracker()
    state = load_state()
    if state and state.get('snapshots'):
        snapshots_from_dict(snapshot_tracker, state['snapshots'])
        print(f"✓ Loaded snapshot data for {len(state['snapshots'])} pools from bot_state.json")
    
    analyzer = PoolQualityAnalyzer()
    scorer = PoolAnalyzer()
    scorer.set_snapshot_tracker(snapshot_tracker)
    
    safe_pools = []
    rejected_pools = []
    
    # Analyze first 40 pools to show detailed results
    analyze_count = min(40, len(pools))
    print(f"\nAnalyzing first {analyze_count} pools in detail...\n")
    
    for i, pool in enumerate(pools[:analyze_count], 1):
        pool_name = pool.get('name', 'Unknown')
        amm_id = pool.get('ammId', pool.get('id', ''))
        tvl = pool.get('tvl', 0)
        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        burn = pool.get('burnPercent', 0)
        volume = day.get('volume', 0)
        
        print(f"[{i:2d}] {pool_name:25s} | TVL: ${tvl:>10,.0f} | Vol: ${volume:>10,.0f} | APR: {apr:>6.1f}% | Burn: {burn:>5.1f}%")
        
        # Run full safety analysis
        analysis = analyzer.analyze_pool(pool, check_safety=config.CHECK_TOKEN_SAFETY)
        
        if analysis['is_safe']:
            safe_pools.append((pool, analysis))
            print(f"     ✅ PASS | Risk: {analysis['risk_level']}")
            if analysis.get('warnings'):
                for w in analysis['warnings'][:2]:  # show first 2 warnings
                    print(f"        ⚠ {w}")
        else:
            rejected_pools.append((pool, analysis))
            print(f"     ❌ FAIL | Risk: {analysis['risk_level']}")
            for r in analysis['risks'][:3]:  # show first 3 risks
                print(f"        ✗ {r}")
        
        # Show RugCheck details if available
        rc = analysis.get('rugcheck')
        if rc and rc.get('available'):
            score = rc.get('risk_score', 0)
            holders = rc.get('total_holders', 0)
            top10 = rc.get('top10_holder_pct', 0)
            print(f"        RC: score={score}/100, holders={holders}, top10={top10:.1f}%")
        elif rc and not rc.get('available'):
            print(f"        RC: unavailable")
        
        # Show LP lock details if available
        lp = analysis.get('lp_lock')
        if lp and lp.get('available'):
            safe = lp.get('safe_pct', 0)
            unlocked = lp.get('unlocked_pct', 0)
            max_single = lp.get('max_single_unlocked_pct', 0)
            print(f"        LP: safe={safe:.1f}%, unlocked={unlocked:.1f}%, max_whale={max_single:.1f}%")
        elif lp and not lp.get('available'):
            print(f"        LP: unavailable")
        
        print()
    
    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    print(f"Total analyzed:  {analyze_count}")
    print(f"✅ Safe pools:   {len(safe_pools)}")
    print(f"❌ Rejected:     {len(rejected_pools)}")
    
    if safe_pools:
        # Compute assumed position size ($300 USD worth of SOL)
        sol_price_usd = api_client.get_sol_price_usd()
        assumed_position_usd = 300.0
        position_sol = assumed_position_usd / sol_price_usd if sol_price_usd > 0 else 3.5
        print(f"\nAssumed position: ${assumed_position_usd:.0f} = {position_sol:.2f} SOL "
              f"(@ ${sol_price_usd:.2f}/SOL)")

        # Enrich safe pools with daily OHLCV candles for multi-period Parkinson σ
        safe_pool_list = [p for p, _ in safe_pools]
        print(f"\nFetching daily candles for {len(safe_pool_list)} safe pools...")
        api_client.enrich_pools_with_candles(safe_pool_list, days=7)

        # Score all safe pools and apply predicted net APR gate
        scored_safe = []
        gated_out = []
        min_net_apr = config.MIN_PREDICTED_NET_APR
        for pool, analysis in safe_pools:
            components = {}
            score = scorer.calculate_pool_score(
                pool, _out=components,
                position_sol=position_sol, sol_price_usd=sol_price_usd)
            pred = components.get('prediction', {})
            net_pct = pred.get('net_return_pct', 0)
            hold_days = pred.get('hold_days', 7)
            net_apr = round(net_pct / hold_days * 365, 1) if hold_days > 0 else 0
            net_sol = pred.get('net_return_sol', 0)
            entry = (pool, analysis, components, score, net_pct, net_apr, net_sol)
            # Apply both gates: APR quality + trade profitability
            if net_apr >= min_net_apr and net_sol > 0:
                scored_safe.append(entry)
            else:
                gated_out.append(entry)
        # Sort by predicted net return
        scored_safe.sort(key=lambda x: x[4], reverse=True)
        gated_out.sort(key=lambda x: x[4], reverse=True)

        print("\n" + "=" * 80)
        print(f"Step 4: Predicted Net APR gate (≥ {min_net_apr:.0f}%) + slippage profitability")
        print("=" * 80)
        print(f"✅ {len(scored_safe)} pools above {min_net_apr:.0f}% net APR and slip-profitable")
        print(f"❌ {len(gated_out)} pools gated out")

        def _print_pool_detail(idx, pool, analysis, components, score, net_pct, net_apr, net_sol, label=""):
            tvl = pool.get('tvl', 0)
            day = pool.get('day', {})
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            burn = pool.get('burnPercent', 0)
            volume = day.get('volume', 0)
            pred = components.get('prediction', {})
            hold_days = pred.get('hold_days', 7)
            fee_apr_val = pred.get('fee_apr', 0)
            reward_apr_val = pred.get('reward_apr', 0)
            total_apr_val = pred.get('total_apr', 0)
            daily_f = pred.get('daily_fees_pct', 0)
            daily_r = pred.get('daily_rewards_pct', 0)
            daily_t = pred.get('daily_total_pct', 0)
            total_y = pred.get('total_yield_pct', 0)
            sigma = pred.get('sigma_daily', 0)
            lvr_d = pred.get('lvr_daily_pct', 0)
            lvr_t = pred.get('lvr_total_pct', 0)
            lvr_apr_val = pred.get('lvr_apr', 0)
            park_n = pred.get('parkinson_n', 0)
            slip_pct = pred.get('roundtrip_slip_pct', 0)
            slip_sol = pred.get('roundtrip_slip_sol', 0)
            gross_sol = pred.get('gross_return_sol', 0)
            tvl_sol = tvl / sol_price_usd if sol_price_usd > 0 else 0

            print(f"\n{idx}. {pool['name']:30s}{label}")
            apr_src = "7d" if pred.get('has_week_data') else "24h"
            print(f"   TVL: ${tvl:>10,.0f}  |  APR({apr_src}): {fee_apr_val:>6.1f}%  |  Burn: {burn:>5.1f}%  |  Vol: ${volume:>10,.0f}")
            print(f"   ★ {hold_days:.0f}d P&L: {net_sol:+.4f} SOL (${net_sol * sol_price_usd:+.2f})  for {position_sol:.2f} SOL position")
            if reward_apr_val > 0:
                print(f"   └─ Yield: feeApr({apr_src}) {fee_apr_val:.0f}% + rewards {reward_apr_val:.0f}% = {total_apr_val:.0f}% → {daily_t:.4f}%/day × {hold_days:.0f}d = {total_y:.4f}%")
            else:
                print(f"   └─ Yield: feeApr({apr_src}) {fee_apr_val:.0f}% → {daily_f:.4f}%/day × {hold_days:.0f}d = {total_y:.4f}%")
            if pred.get('has_price_data'):
                src = pred.get('parkinson_src', 'window')
                if src == 'candles':
                    src_label = f"{park_n}×1d"
                elif park_n == 7:
                    src_label = "7d window"
                else:
                    src_label = "24h"
                print(f"   └─ Vol:   σ = {sigma:.1%}/day (Parkinson {src_label}) → LVR = {lvr_d:.4f}%/day × {hold_days:.0f}d = {lvr_t:.4f}%  ({lvr_apr_val:.0f}% APR)")
            else:
                print(f"   └─ Vol:   σ = {sigma:.1%}/day (default, no price data) → LVR = {lvr_t:.4f}%/{hold_days:.0f}d  ({lvr_apr_val:.0f}% APR)")
            print(f"   └─ Gross: {total_y:.4f}% − {lvr_t:.4f}% = {net_pct:.4f}%/{hold_days:.0f}d  (×{365/hold_days:.0f} = {net_apr:.1f}% APR)")
            print(f"   └─ Gross: {gross_sol:+.4f} SOL  ({net_pct:.4f}% × {position_sol:.2f})")
            if tvl_sol > 0:
                print(f"   └─ Slip:  −{slip_sol:.4f} SOL  (RT {slip_pct:.2f}% = swap fee + {position_sol:.2f}/{tvl_sol:.0f} impact)")
            else:
                print(f"   └─ Slip:  −{slip_sol:.4f} SOL  (RT {slip_pct:.2f}%)")
            print(f"   └─ Net:   {net_sol:+.4f} SOL  (${net_sol * sol_price_usd:+.2f})")
            print(f"   Score: {score:.1f}/100  |  Depth: {components.get('depth', 0):.0f}/10  |  DQ: {components.get('data_quality', 0):.0f}/15")
            print(f"   Quality tier: {analysis['liquidity_tier']}")
            rc = analysis.get('rugcheck')
            if rc and rc.get('available'):
                holders = rc.get('total_holders', 0)
                top10 = rc.get('top10_holder_pct', 0)
                risk_score = rc.get('risk_score', 0)
                print(f"   RugCheck: {holders:,} holders  |  Top10: {top10:.1f}%  |  Risk: {risk_score}/100")
            if analysis.get('warnings'):
                print(f"   Warnings: {len(analysis['warnings'])}×")
                for w in analysis['warnings'][:3]:
                    print(f"      ⚠ {w}")

        if gated_out:
            print(f"\n--- Rejected pools ---")
            for i, (pool, analysis, components, score, net_pct, net_apr, net_sol) in enumerate(gated_out, 1):
                reason = ""
                if net_apr < min_net_apr:
                    reason = f"  ❌ net APR {net_apr:.1f}% < {min_net_apr:.0f}%"
                elif net_sol <= 0:
                    reason = f"  ❌ slip eats profit"
                _print_pool_detail(i, pool, analysis, components, score, net_pct, net_apr, net_sol, reason)

        if scored_safe:
            print("\n" + "=" * 80)
            print(f"Top {len(scored_safe)} Investable Pools (sorted by predicted net return)")
            print("=" * 80)
            for i, (pool, analysis, components, score, net_pct, net_apr, net_sol) in enumerate(scored_safe, 1):
                _print_pool_detail(i, pool, analysis, components, score, net_pct, net_apr, net_sol)
    
    if rejected_pools:
        print("\n" + "=" * 80)
        print("Rejection Reasons Summary")
        print("=" * 80)
        
        # Count rejection reasons
        reason_counts = {}
        for pool, analysis in rejected_pools:
            for risk in analysis['risks']:
                # Simplify risk to category
                if 'RugCheck' in risk and 'unavailable' in risk:
                    key = 'RugCheck unavailable'
                elif 'LP lock' in risk and 'unavailable' in risk:
                    key = 'LP lock unavailable'
                elif 'Low LP burn' in risk:
                    key = 'Low LP burn'
                elif 'RugCheck DANGER' in risk:
                    key = 'RugCheck danger'
                elif 'freeze authority' in risk:
                    key = 'Freeze authority'
                elif 'mint authority' in risk:
                    key = 'Mint authority'
                elif 'Top 10 holders' in risk:
                    key = 'Top 10 concentration'
                elif 'Single holder' in risk:
                    key = 'Whale holder'
                elif 'few holders' in risk:
                    key = 'Low holder count'
                elif 'mutable metadata' in risk:
                    key = 'Mutable metadata'
                elif 'LP providers' in risk:
                    key = 'Low LP providers'
                elif 'LP whale' in risk:
                    key = 'LP whale risk'
                elif 'total LP is safe' in risk:
                    key = 'Low total LP safety'
                elif 'Extreme APR' in risk:
                    key = 'Extreme APR'
                elif 'rug pull' in risk.lower():
                    key = 'Rug pull pattern'
                elif 'RugCheck risk score' in risk:
                    key = 'High risk score'
                else:
                    key = risk[:50]
                
                reason_counts[key] = reason_counts.get(key, 0) + 1
        
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {count:2d}× {reason}")

if __name__ == '__main__':
    main()
