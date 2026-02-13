"""
Pool Quality Analyzer
Provides detailed analysis of pool quality and risks
"""
from typing import Dict, List
from bot.raydium_client import RaydiumAPIClient
from bot.safety.liquidity_lock import LiquidityLockChecker, check_pool_lock_safety
from bot.config import config


class PoolQualityAnalyzer:
    """Analyzes pool quality and identifies risks"""
    
    def __init__(self):
        self.lock_checker = LiquidityLockChecker()
    
    def analyze_pool(self, pool: Dict, check_locks: bool = True) -> Dict:
        """
        Analyze a single pool for quality and risks.
        """
        risks = []
        warnings = []
        quality_issues = []
        
        liquidity = pool.get('liquidity', 0)
        volume24h = pool.get('volume24h', 0)
        apr24h = pool.get('apr24h', 0)
        base_amount = pool.get('tokenAmountCoin', 0)
        name = pool.get('name', '')
        
        if base_amount > 1e12:
            risks.append(f"Meme token detected (supply: {base_amount:.2e})")
        
        if apr24h > 200:
            risks.append(f"Extremely high APR ({apr24h:.1f}%) - likely unsustainable")
        elif apr24h > 100:
            warnings.append(f"Very high APR ({apr24h:.1f}%) - high volatility expected")
        
        if liquidity < 50_000:
            warnings.append(f"Low liquidity (${liquidity:,.0f}) - high slippage risk")
        elif liquidity < 100_000:
            quality_issues.append(f"Moderate liquidity (${liquidity:,.0f})")
        
        vol_tvl = volume24h / liquidity if liquidity > 0 else 0
        if vol_tvl > 5:
            warnings.append(f"Very high volume/TVL ({vol_tvl:.1f}x) - volatile pool")
        
        if liquidity < 20_000 and apr24h > 300:
            risks.append("⚠️ CRITICAL: Low liquidity + extreme APR = likely rug pull")
        
        lock_status = None
        if check_locks:
            lock_status = self.lock_checker.get_lp_lock_status(pool)
            
            if lock_status.get('rugcheck_available'):
                if lock_status.get('is_rugged'):
                    risks.append("🚨 RUG PULL DETECTED by RugCheck")
                elif lock_status.get('rugcheck_score', 0) < 100:
                    risks.append(f"Very low RugCheck score ({lock_status.get('rugcheck_score')}/1000) - HIGH RISK")
                elif lock_status.get('rugcheck_score', 0) < 500:
                    warnings.append(f"Low RugCheck score ({lock_status.get('rugcheck_score')}/1000)")
                
                if lock_status.get('has_freeze_authority'):
                    warnings.append("Token has freeze authority")
                if lock_status.get('has_mint_authority'):
                    warnings.append("Token has mint authority")
            elif lock_status['risk_level'] == 'unknown':
                warnings.append("Unable to verify token safety (RugCheck unavailable)")
        
        if risks:
            risk_level = "HIGH"
        elif warnings:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"
        
        return {
            'risk_level': risk_level,
            'risks': risks,
            'warnings': warnings,
            'quality_issues': quality_issues,
            'is_safe': len(risks) == 0,
            'is_meme_token': base_amount > 1e12,
            'liquidity_tier': 'high' if liquidity > 100_000 else 'medium' if liquidity > 50_000 else 'low',
            'lock_status': lock_status,
        }
    
    @staticmethod
    def print_pool_analysis(pool: Dict, check_locks: bool = True):
        """Print detailed analysis for a pool"""
        analyzer = PoolQualityAnalyzer()
        analysis = analyzer.analyze_pool(pool, check_locks=check_locks)
        
        print(f"\n{'='*80}")
        print(f"Pool: {pool['name']}")
        print(f"AMM ID: {pool['ammId']}")
        print(f"{'='*80}")
        
        print(f"\nMetrics:")
        print(f"  TVL: ${pool['liquidity']:,.2f}")
        print(f"  24h Volume: ${pool['volume24h']:,.2f}")
        print(f"  Volume/TVL: {pool['volume24h']/pool['liquidity']:.2f}x")
        print(f"  APR (24h): {pool['apr24h']:.2f}%")
        print(f"  Price: {pool['price']:.10f}")
        print(f"  Base tokens: {pool['tokenAmountCoin']:,.2f}")
        print(f"  Quote tokens: {pool['tokenAmountPc']:,.2f}")
        
        print(f"\nRisk Assessment:")
        print(f"  Overall Risk: {analysis['risk_level']}")
        print(f"  Liquidity Tier: {analysis['liquidity_tier'].upper()}")
        print(f"  Meme Token: {'YES' if analysis['is_meme_token'] else 'NO'}")
        
        if analysis.get('lock_status'):
            lock = analysis['lock_status']
            
            if lock.get('rugcheck_available'):
                score = lock.get('rugcheck_score', 0)
                print(f"  RugCheck Token Safety: {score}/1000")
                
                if lock.get('is_rugged'):
                    print(f"    🚨 RUG PULL DETECTED!")
                
                if lock.get('has_freeze_authority'):
                    print(f"    ⚠️  Has freeze authority")
                if lock.get('has_mint_authority'):
                    print(f"    ⚠️  Has mint authority")
                
                holders = lock.get('total_holders', 0)
                top_holder_pct = lock.get('top_holder_concentration')
                
                if holders:
                    print(f"    Holders: {holders:,}")
                if top_holder_pct:
                    concentration_warning = " ⚠️  High!" if top_holder_pct > 50 else ""
                    print(f"    Top 5 holders: {top_holder_pct:.1f}%{concentration_warning}")
                
                print(f"    ⚠️  Note: LP lock status NOT verified - check manually!")
            
            else:
                print(f"  Token Safety: UNKNOWN (RugCheck unavailable)")
                print(f"    ⚠️  Cannot verify token or LP lock safety!")
        
        if analysis['risks']:
            print(f"\n  🚨 CRITICAL RISKS:")
            for risk in analysis['risks']:
                print(f"    - {risk}")
        
        if analysis['warnings']:
            print(f"\n  ⚠️  WARNINGS:")
            for warning in analysis['warnings']:
                print(f"    - {warning}")
        
        if analysis['quality_issues']:
            print(f"\n  ℹ️  QUALITY ISSUES:")
            for issue in analysis['quality_issues']:
                print(f"    - {issue}")
        
        if analysis['is_safe']:
            print(f"\n  ✅ Pool appears relatively safe for LP")
        else:
            print(f"\n  ❌ Pool has significant risks - NOT RECOMMENDED")
        
        print(f"{'='*80}")
    
    @staticmethod
    def get_safe_pools(pools: List[Dict], check_locks: bool = False) -> List[Dict]:
        """
        Filter pools to only safe ones.
        """
        analyzer = PoolQualityAnalyzer()
        safe_pools = []
        
        for pool in pools:
            analysis = analyzer.analyze_pool(pool, check_locks=check_locks)
            if analysis['is_safe'] and analysis['liquidity_tier'] != 'low':
                safe_pools.append(pool)
        
        return safe_pools


def main():
    """Analyze current pool quality"""
    client = RaydiumAPIClient()
    
    print("Fetching pools from Raydium API...")
    pools = client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
        quote_tokens=config.ALLOWED_QUOTE_TOKENS,
    )
    
    print(f"\n{'='*80}")
    print(f"POOL QUALITY REPORT")
    print(f"{'='*80}")
    print(f"Total pools matching basic criteria: {len(pools)}")
    
    for pool in sorted(pools, key=lambda x: x.get('apr24h', 0), reverse=True):
        PoolQualityAnalyzer.print_pool_analysis(pool)
    
    safe_pools = PoolQualityAnalyzer.get_safe_pools(pools)
    
    print(f"\n{'='*80}")
    print(f"SUMMARY")
    print(f"{'='*80}")
    print(f"Total pools analyzed: {len(pools)}")
    print(f"Safe pools: {len(safe_pools)}")
    print(f"Risky pools: {len(pools) - len(safe_pools)}")
    
    if safe_pools:
        print(f"\n✅ Safe pools to consider:")
        for pool in safe_pools:
            print(f"  - {pool['name']} (APR: {pool['apr24h']:.2f}%, TVL: ${pool['liquidity']:,.0f})")
    else:
        print(f"\n⚠️  No safe pools found matching criteria!")
        print(f"\nRecommendations:")
        print(f"  1. Lower MIN_APR_24H to find more stable pools")
        print(f"  2. Consider targeting stablecoin pairs specifically")
        print(f"  3. Wait for better market conditions")
    

if __name__ == "__main__":
    main()
