"""
Pool Scoring and Analysis

Scoring factors:
1. APR (35% weight) - Fee generation potential
2. Volume/TVL ratio (20% weight) - Activity level
3. Liquidity depth (20% weight) - Slippage resistance
4. IL risk (10% weight) - Price stability
5. LP burn (15% weight) - Rug pull protection (from V3 burnPercent)
"""
import math
from typing import Dict, List
from bot.config import config


class PoolAnalyzer:
    def __init__(self):
        self.config = config

    def calculate_pool_score(self, pool: Dict) -> float:
        """
        Calculate a composite score for a pool (0-100).
        Higher score = better opportunity.
        """
        score = 0.0

        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        burn_percent = pool.get('burnPercent', 0)

        # 1. APR Score (0-35 points)
        apr_score = min(35, (apr / 50) * 35)
        score += apr_score

        # 2. Volume/TVL Score (0-20 points)
        vol_tvl_ratio = volume / tvl if tvl > 0 else 0
        vol_score = min(20, (vol_tvl_ratio / 2.0) * 20)
        score += vol_score

        # 3. Liquidity Depth Score (0-20 points)
        if tvl >= 1_000_000:
            liq_score = 20
        elif tvl >= 100_000:
            liq_score = 15
        elif tvl >= 50_000:
            liq_score = 10
        elif tvl >= 10_000:
            liq_score = 5
        else:
            liq_score = 0
        score += liq_score

        # 4. IL Risk Score (0-10 points)
        il_score = self._estimate_il_safety(pool)
        score += il_score

        # 5. LP Burn Score (0-15 points)
        if burn_percent >= 95:
            burn_score = 15
        elif burn_percent >= 80:
            burn_score = 12
        elif burn_percent >= 50:
            burn_score = 8
        elif burn_percent >= 20:
            burn_score = 3
        else:
            burn_score = 0
        score += burn_score

        return round(score, 2)

    def _estimate_il_safety(self, pool: Dict) -> float:
        """Estimate IL safety score (0-10 points)."""
        name = pool.get('name', '').upper()

        # Stablecoin pairs have minimal IL
        if any(pair in name for pair in ['USDC/USDT', 'USDT/USDC']):
            return 10.0

        # SOL/stablecoin pairs have moderate IL
        if ('SOL' in name or 'WSOL' in name) and any(s in name for s in ['USDC', 'USDT']):
            return 6.0

        return 3.0

    def rank_pools(self, pools: List[Dict], top_n: int = 10) -> List[Dict]:
        """Score and rank pools, return top N."""
        scored_pools = []

        for pool in pools:
            pool_copy = pool.copy()
            pool_copy['score'] = self.calculate_pool_score(pool)
            scored_pools.append(pool_copy)

        ranked = sorted(scored_pools, key=lambda x: x['score'], reverse=True)
        return ranked[:top_n]

    # Reserve SOL for ATA rent (3 accounts × ~0.00203) + transaction fees
    ATA_RENT_RESERVE_SOL = 0.01

    def calculate_position_size(
        self,
        pool: Dict,
        available_capital: float,
        num_open_positions: int = 0,
        total_wallet_balance: float = 0.0,
        rank: int = 0,
        total_ranked: int = 1,
    ) -> float:
        """
        Calculate optimal position size in SOL.

        Position sizing rules:
        1. A reserve is always kept: max(balance * RESERVE_PERCENT, MIN_RESERVE_SOL)
        2. The best-ranked pool (rank=0) gets the largest allocation.
           With 1 position slot, it gets POSITION_SIZE_PERCENT of deployable capital.
        3. With multiple slots, higher-ranked pools get proportionally more
           (descending weight: rank 0 biggest, rank N-1 smallest).
        4. Never exceed MAX_ABSOLUTE_POSITION_SOL.
        """
        positions_remaining = config.MAX_CONCURRENT_POSITIONS - num_open_positions
        if positions_remaining <= 0:
            return 0.0

        # Use total_wallet_balance to compute reserve (if provided)
        reference_balance = total_wallet_balance if total_wallet_balance > 0 else available_capital

        # Reserve: the larger of percentage-based or absolute minimum
        reserve = max(
            reference_balance * config.RESERVE_PERCENT,
            config.MIN_RESERVE_SOL,
        )
        # Also keep ATA rent aside
        reserve += self.ATA_RENT_RESERVE_SOL

        deployable = available_capital - reserve
        if deployable <= 0:
            return 0.0

        # Position sizing by slot count
        if config.MAX_CONCURRENT_POSITIONS == 1:
            # Single-position mode: use POSITION_SIZE_PERCENT (e.g. 80%)
            alloc_percent = config.POSITION_SIZE_PERCENT
        else:
            # Multiple positions: split evenly across remaining slots
            # Each position gets an equal share of deployable capital
            alloc_percent = 1.0 / positions_remaining

        size = min(
            deployable * alloc_percent,
            config.MAX_ABSOLUTE_POSITION_SOL,
        )

        return size

    @staticmethod
    def calculate_impermanent_loss(
        entry_price_ratio: float,
        current_price_ratio: float,
    ) -> float:
        """
        Calculate impermanent loss as a decimal.
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
        """Estimate fees earned based on pool's 24h APR."""
        day = pool.get('day', {})
        apr_pct = day.get('apr', 0) or pool.get('apr24h', 0)
        apr_decimal = apr_pct / 100
        hourly_rate = apr_decimal / (365 * 24)
        return position_size_sol * hourly_rate * time_held_hours
