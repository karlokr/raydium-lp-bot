"""
Pool Scoring and Analysis

Scoring factors (0–100 base, up to +20 bonus):
1. APR (25% weight) - Fee generation potential
2. Volume/TVL ratio (15% weight) - Activity level
3. Liquidity depth (15% weight) - Slippage resistance
4. IL risk (5% weight) - Price stability
5. LP burn (10% weight) - Rug pull protection (from V3 burnPercent)
6. Momentum (15% weight) - Day vs week acceleration
7. Freshness (15% weight) - Pool age bonus (new + strong metrics)

Bonus (on top of 100):
8. Snapshot velocity (+0–10) - Real-time volume/tvl/price trend from in-memory tracker
"""
import math
import time
from typing import Dict, List
from bot.config import config


class PoolAnalyzer:
    def __init__(self):
        self.config = config
        # Optional reference to a SnapshotTracker, set by main.py
        self._snapshot_tracker = None

    def set_snapshot_tracker(self, tracker):
        """Attach a SnapshotTracker so scoring can include velocity bonus."""
        self._snapshot_tracker = tracker

    def calculate_pool_score(self, pool: Dict) -> float:
        """
        Calculate a composite score for a pool (0-100 base + up to 10 bonus).
        Higher score = better opportunity.
        """
        score = 0.0

        day = pool.get('day', {})
        week = pool.get('week', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        burn_percent = pool.get('burnPercent', 0)

        # 1. APR Score (0-25 points)
        apr_score = min(25, (apr / 50) * 25)
        score += apr_score

        # 2. Volume/TVL Score (0-15 points)
        vol_tvl_ratio = volume / tvl if tvl > 0 else 0
        vol_score = min(15, (vol_tvl_ratio / 2.0) * 15)
        score += vol_score

        # 3. Liquidity Depth Score (0-15 points)
        # Lower thresholds — small pools are fine if they pass safety
        if tvl >= 500_000:
            liq_score = 15
        elif tvl >= 100_000:
            liq_score = 13
        elif tvl >= 50_000:
            liq_score = 11
        elif tvl >= 20_000:
            liq_score = 8
        elif tvl >= 5_000:
            liq_score = 5
        else:
            liq_score = 0
        score += liq_score

        # 4. IL Risk Score (0-5 points)
        il_score = self._estimate_il_safety(pool, max_points=5)
        score += il_score

        # 5. LP Burn Score (0-10 points)
        if burn_percent >= 95:
            burn_score = 10
        elif burn_percent >= 80:
            burn_score = 8
        elif burn_percent >= 50:
            burn_score = 5
        elif burn_percent >= 20:
            burn_score = 2
        else:
            burn_score = 0
        score += burn_score

        # 6. Momentum Score (0-15 points) — day vs week acceleration
        momentum_score = self._calculate_momentum(day, week, volume, tvl)
        score += momentum_score

        # 7. Freshness Score (0-15 points) — pool age bonus
        freshness_score = self._calculate_freshness(pool, score)
        score += freshness_score

        # 8. Snapshot velocity bonus (+0–10) — real-time trend from tracker
        velocity_bonus = 0.0
        pool_id = pool.get('ammId', pool.get('id', ''))
        if self._snapshot_tracker and pool_id:
            velocity_bonus = self._snapshot_tracker.get_velocity_bonus(pool_id)
        score += velocity_bonus

        return round(score, 2)

    # ------------------------------------------------------------------
    # Momentum: day vs week
    # ------------------------------------------------------------------

    def _calculate_momentum(self, day: Dict, week: Dict, volume: float, tvl: float) -> float:
        """Score momentum by comparing today's metrics to weekly average (0-15 pts).

        - Volume momentum (0-8 pts): day.volume vs week.volume/7
        - APR momentum (0-7 pts): day.apr vs week.apr (proxy for fee velocity)

        Pools where today massively outperforms the weekly average are
        experiencing a breakout — they deserve a higher ranking.
        """
        score = 0.0

        # --- Volume momentum (0-8 pts) ---
        day_vol = day.get('volume', 0) or volume
        week_vol = week.get('volume', 0)
        week_avg_vol = week_vol / 7 if week_vol > 0 else 0

        if week_avg_vol > 0 and day_vol > 0:
            vol_ratio = day_vol / week_avg_vol  # 1.0 = same as usual
            # 2x weekly avg = full 8 pts, linear scale, floor at 0
            score += min(8.0, max(0.0, (vol_ratio - 1.0) * 8.0))
        elif day_vol > 0 and week_avg_vol == 0:
            # Brand new pool with volume but no week data — give moderate score
            score += 4.0

        # --- APR momentum (0-7 pts) ---
        day_apr = day.get('apr', 0)
        week_apr = week.get('apr', 0)

        if week_apr > 0 and day_apr > 0:
            apr_ratio = day_apr / week_apr
            # 1.5x weekly APR = full 7 pts
            score += min(7.0, max(0.0, (apr_ratio - 1.0) / 0.5 * 7.0))
        elif day_apr > 0 and week_apr == 0:
            score += 3.5

        return round(score, 2)

    # ------------------------------------------------------------------
    # Freshness: pool age weighting
    # ------------------------------------------------------------------

    def _calculate_freshness(self, pool: Dict, base_score: float) -> float:
        """Score pool freshness based on openTime (0-15 pts).

        New pools with strong base metrics = breakout candidates.
        Old pools are fine but don't get the freshness bonus.

        Tiers:
          < 1 day old   → 15 pts (if base_score >= 30, else 5)
          1-3 days      → 12 pts
          3-7 days      → 8 pts
          7-14 days     → 4 pts
          14-30 days    → 2 pts
          > 30 days     → 0 pts
        """
        open_time = pool.get('openTime', 0)
        if not open_time:
            return 0.0

        try:
            open_time = int(open_time)
        except (ValueError, TypeError):
            return 0.0

        age_seconds = time.time() - open_time
        age_days = age_seconds / 86400

        if age_days < 0:
            return 0.0  # bad data

        if age_days < 1:
            # Very new pool — only reward if base metrics are decent
            # to avoid rewarding brand-new rug pools with nothing else going for them
            return 15.0 if base_score >= 30 else 5.0
        elif age_days < 3:
            return 12.0
        elif age_days < 7:
            return 8.0
        elif age_days < 14:
            return 4.0
        elif age_days < 30:
            return 2.0
        else:
            return 0.0

    def _estimate_il_safety(self, pool: Dict, max_points: float = 5) -> float:
        """Estimate IL safety score (0-max_points)."""
        name = pool.get('name', '').upper()

        # Stablecoin pairs have minimal IL
        if any(pair in name for pair in ['USDC/USDT', 'USDT/USDC']):
            return max_points

        # SOL/stablecoin pairs have moderate IL
        if ('SOL' in name or 'WSOL' in name) and any(s in name for s in ['USDC', 'USDT']):
            return max_points * 0.6

        return max_points * 0.3

    def rank_pools(self, pools: List[Dict], top_n: int = 10) -> List[Dict]:
        """Score and rank pools, return top N.
        
        Injects momentum, freshness, and velocity data into each pool dict
        for downstream logging.
        """
        scored_pools = []

        for pool in pools:
            pool_copy = pool.copy()
            pool_copy['score'] = self.calculate_pool_score(pool)

            # Attach component scores for transparency
            day = pool.get('day', {})
            week = pool.get('week', {})
            volume = day.get('volume', 0) or pool.get('volume24h', 0)
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            pool_copy['_momentum'] = self._calculate_momentum(day, week, volume, tvl)
            pool_copy['_freshness'] = self._calculate_freshness(pool, pool_copy['score'])

            pool_id = pool.get('ammId', pool.get('id', ''))
            if self._snapshot_tracker and pool_id:
                pool_copy['_velocity'] = self._snapshot_tracker.get_velocity_bonus(pool_id)
            else:
                pool_copy['_velocity'] = 0.0

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
        Calculate optimal position size in SOL.

        Split available capital evenly across remaining slots after
        keeping a small fixed reserve for transaction fees.

        Rules:
        1. Reserve = RESERVE_SOL (fixed, e.g. 0.05 SOL)
        2. Size = deployable / positions_remaining
        3. Never exceed MAX_ABSOLUTE_POSITION_SOL
        """
        positions_remaining = config.MAX_CONCURRENT_POSITIONS - num_open_positions
        if positions_remaining <= 0:
            return 0.0

        deployable = available_capital - config.RESERVE_SOL
        if deployable <= 0:
            return 0.0

        # Equal split across remaining slots
        size = deployable / positions_remaining

        size = min(size, config.MAX_ABSOLUTE_POSITION_SOL)

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
