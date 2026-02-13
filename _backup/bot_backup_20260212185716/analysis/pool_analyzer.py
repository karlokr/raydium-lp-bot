"""
Pool Scoring and Analysis
"""
import math
from typing import Dict, List
from bot.config import config


class PoolAnalyzer:
    def __init__(self):
        self.config = config
    
    def calculate_pool_score(self, pool: Dict) -> float:
        """
        Calculate a composite score for a pool based on multiple factors.
        Higher score = better opportunity.
        
        Scoring factors:
        1. APR (40% weight) - Fee generation potential
        2. Volume/TVL ratio (25% weight) - Activity level
        3. Liquidity depth (20% weight) - Slippage resistance
        4. IL risk (15% weight) - Price stability (lower volatility = better)
        
        Returns:
            Score between 0-100
        """
        score = 0.0
        
        # 1. APR Score (0-40 points)
        apr = pool.get('apr24h', 0)
        apr_score = min(40, (apr / 50) * 40)
        score += apr_score
        
        # 2. Volume/TVL Score (0-25 points)
        liquidity = pool.get('liquidity', 1)
        volume = pool.get('volume24h', 0)
        vol_tvl_ratio = volume / liquidity if liquidity > 0 else 0
        vol_score = min(25, (vol_tvl_ratio / 2.0) * 25)
        score += vol_score
        
        # 3. Liquidity Depth Score (0-20 points)
        liq_score = 0
        if liquidity >= 1_000_000:
            liq_score = 20
        elif liquidity >= 100_000:
            liq_score = 15
        elif liquidity >= 50_000:
            liq_score = 10
        elif liquidity >= 10_000:
            liq_score = 5
        score += liq_score
        
        # 4. IL Risk Score (0-15 points)
        il_score = self._estimate_il_safety(pool)
        score += il_score
        
        return round(score, 2)
    
    def _estimate_il_safety(self, pool: Dict) -> float:
        """
        Estimate IL safety score (0-15 points).
        """
        name = pool.get('name', '').upper()
        
        if any(pair in name for pair in ['USDC/USDT', 'USDT/USDC', 'USDH/USDC']):
            return 15.0
        
        if ('SOL' in name or 'STSOL' in name) and any(stable in name for stable in ['USDC', 'USDT']):
            return 8.0
        
        return 5.0
    
    def rank_pools(self, pools: List[Dict], top_n: int = 10) -> List[Dict]:
        """
        Score and rank pools, return top N.
        """
        scored_pools = []
        
        for pool in pools:
            pool_copy = pool.copy()
            pool_copy['score'] = self.calculate_pool_score(pool)
            scored_pools.append(pool_copy)
        
        ranked = sorted(scored_pools, key=lambda x: x['score'], reverse=True)
        return ranked[:top_n]
    
    def calculate_position_size(
        self,
        pool: Dict,
        available_capital: float,
        num_open_positions: int = 0,
    ) -> float:
        """
        Calculate optimal position size based on constraints.
        Uses dynamic sizing to ensure equal position sizes.
        
        Args:
            pool: Pool dictionary with liquidity data
            available_capital: Available SOL to deploy
            num_open_positions: Number of currently open positions
            
        Returns:
            Position size in SOL
        """
        positions_remaining = config.MAX_CONCURRENT_POSITIONS - num_open_positions
        if positions_remaining <= 0:
            return 0.0
        
        max_deploy_percent = 1.0 - config.RESERVE_PERCENT
        dynamic_percent = min(1.0 / positions_remaining, max_deploy_percent)
        
        size = min(
            available_capital * dynamic_percent,
            config.MAX_ABSOLUTE_POSITION_SOL,
        )
        
        return size
    
    @staticmethod
    def calculate_impermanent_loss(
        entry_price_ratio: float,
        current_price_ratio: float,
    ) -> float:
        """
        Calculate impermanent loss percentage.
        
        Formula: IL = 2 * sqrt(price_ratio) / (1 + price_ratio) - 1
        """
        if entry_price_ratio <= 0 or current_price_ratio <= 0:
            return 0.0
        
        price_change = current_price_ratio / entry_price_ratio
        il = 2 * math.sqrt(price_change) / (1 + price_change) - 1
        return il
    
    @staticmethod
    def estimate_fees_earned(
        pool: Dict,
        position_size_sol: float,
        time_held_hours: float,
    ) -> float:
        """
        Estimate fees earned based on pool's 24h APR.
        """
        apr_24h = pool.get('apr24h', 0) / 100
        hourly_rate = apr_24h / (365 * 24)
        fees = position_size_sol * hourly_rate * time_held_hours
        return fees


if __name__ == "__main__":
    from bot.raydium_client import RaydiumAPIClient
    
    client = RaydiumAPIClient()
    analyzer = PoolAnalyzer()
    
    pools = client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
        quote_tokens=config.ALLOWED_QUOTE_TOKENS,
    )
    
    top_pools = analyzer.rank_pools(pools, top_n=10)
    
    print(f"Top 10 Pools by Score:\n")
    print(f"{'Rank':<5} {'Pool':<20} {'Score':<8} {'APR':<8} {'Vol/TVL':<10} {'TVL':<12}")
    print("=" * 75)
    
    for i, pool in enumerate(top_pools, 1):
        vol_tvl = pool['volume24h'] / pool['liquidity']
        print(f"{i:<5} {pool['name']:<20} {pool['score']:<8.1f} "
              f"{pool['apr24h']:<8.1f}% {vol_tvl:<10.2f}x ${pool['liquidity']:<11,.0f}")
    
    print("\n\nImpermanent Loss Examples:")
    print(f"Price +10%: {analyzer.calculate_impermanent_loss(1.0, 1.1):.2%}")
    print(f"Price +50%: {analyzer.calculate_impermanent_loss(1.0, 1.5):.2%}")
    print(f"Price +100%: {analyzer.calculate_impermanent_loss(1.0, 2.0):.2%}")
    print(f"Price -50%: {analyzer.calculate_impermanent_loss(1.0, 0.5):.2%}")
