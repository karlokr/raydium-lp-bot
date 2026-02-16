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
    
    # Analyze first 20 pools to show detailed results
    analyze_count = min(20, len(pools))
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
        print("\n" + "=" * 80)
        print(f"Top {len(safe_pools)} Safe Pools with Scoring Details")
        print("=" * 80)
        for i, (pool, analysis) in enumerate(safe_pools, 1):
            tvl = pool.get('tvl', 0)
            day = pool.get('day', {})
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            burn = pool.get('burnPercent', 0)
            volume = day.get('volume', 0)
            
            # Calculate pool score with component breakdown
            components = {}
            score = scorer.calculate_pool_score(pool, _out=components)
            
            print(f"\n{i}. {pool['name']:30s}")
            print(f"   TVL: ${tvl:>10,.0f}  |  APR: {apr:>6.1f}%  |  Burn: {burn:>5.1f}%  |  Vol: ${volume:>10,.0f}")
            print(f"   Score: {score:.1f}/110")
            print(f"   └─ Fee potential:  APR + Vol/TVL concentration")
            print(f"   └─ Safety:         Burn + IL safety")
            print(f"   └─ Discovery:      Momentum: {components.get('momentum', 0):.1f}/15  |  Freshness: {components.get('freshness', 0):.1f}/10")
            print(f"   └─ Velocity bonus: {components.get('velocity', 0):.1f}/10")
            print(f"   Quality tier: {analysis['liquidity_tier']}")
            
            # Show key safety metrics
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
