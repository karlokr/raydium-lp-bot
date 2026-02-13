"""
Pool Scoring and Analysis — optimised for LP fee farming

Core LP insight: LPs earn fees from trading volume (direction-irrelevant).
What kills LP returns is impermanent loss (large price moves in either
direction) and fee dilution (too many other LPs in the pool).

Scoring factors (0–100 base, up to +10 velocity bonus):

Fee potential (50 pts total):
  1. Fee APR (30 pts)         – direct measure of fee yield
  2. Volume concentration     – vol/TVL ratio; high ratio = outsized
     (20 pts)                   fee share for your capital

Safety & structure (25 pts total):
  3. LP burn (10 pts)         – rug pull protection
  4. IL safety (15 pts)       – price range tightness (from API priceMin/Max)

Discovery edge (25 pts total):
  5. Volume momentum (15 pts) – day vs week volume acceleration
  6. Freshness (10 pts)       – new pools with proven metrics (smaller bonus)

Real-time bonus (+0–10 on top of 100):
  7. Snapshot velocity         – volume rising + TVL stable + price tight
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
        Higher score = better LP opportunity.
        """
        score = 0.0

        day = pool.get('day', {})
        week = pool.get('week', {})
        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        burn_percent = pool.get('burnPercent', 0)

        # ── Fee potential (50 pts) ─────────────────────────────────

        # 1. Fee APR (0-30 pts)
        # Use feeApr when available — it strips out reward/emission APR
        # and reflects only the real trading-fee yield.
        fee_apr = day.get('feeApr', 0)
        apr = fee_apr if fee_apr > 0 else (day.get('apr', 0) or pool.get('apr24h', 0))
        # 200% fee APR = full 30 pts (very generous for meme pools)
        apr_score = min(30, (apr / 200) * 30)
        score += apr_score

        # 2. Volume concentration — vol/TVL ratio (0-20 pts)
        # This is THE key metric for LPs: how much trading volume flows
        # through each dollar of liquidity. High ratio = outsized fees
        # per unit of capital deployed.
        # Also implicitly penalises over-crowded pools (high TVL + low vol).
        vol_tvl_ratio = volume / tvl if tvl > 0 else 0
        # 3x daily turnover = full 20 pts
        vol_conc_score = min(20, (vol_tvl_ratio / 3.0) * 20)
        score += vol_conc_score

        # ── Safety & structure (25 pts) ────────────────────────────

        # 3. LP Burn Score (0-10 points) — rug protection
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

        # 4. IL Safety (0-15 pts) — price range tightness
        il_score = self._estimate_il_safety(pool)
        score += il_score

        # ── Discovery edge (25 pts) ───────────────────────────────

        # 5. Volume momentum (0-15 pts) — day vs week
        momentum_score = self._calculate_momentum(day, week, volume, tvl)
        score += momentum_score

        # 6. Freshness (0-10 pts) — pool age bonus
        freshness_score = self._calculate_freshness(pool, score)
        score += freshness_score

        # ── Real-time bonus (+0–10) ───────────────────────────────

        # 7. Snapshot velocity — from in-memory tracker
        velocity_bonus = 0.0
        pool_id = pool.get('ammId', pool.get('id', ''))
        if self._snapshot_tracker and pool_id:
            velocity_bonus = self._snapshot_tracker.get_velocity_bonus(pool_id)
        score += velocity_bonus

        return round(score, 2)

    # ------------------------------------------------------------------
    # Momentum: day vs week volume
    # ------------------------------------------------------------------

    def _calculate_momentum(self, day: Dict, week: Dict, volume: float, tvl: float) -> float:
        """Score volume momentum by comparing today to weekly average (0-15 pts).

        Compares day.volume to week.volume/7. A pool doing 2× its weekly
        average today is experiencing a volume surge → more fees right now.

        We DON'T compare APR momentum separately because APR is derived
        from volume/TVL — that would double-count.
        """
        score = 0.0

        day_vol = day.get('volume', 0) or volume
        week_vol = week.get('volume', 0)
        week_avg_vol = week_vol / 7 if week_vol > 0 else 0

        if week_avg_vol > 0 and day_vol > 0:
            vol_ratio = day_vol / week_avg_vol  # 1.0 = same as usual
            # Also reward pools with sustainedly high volume (ratio near 1.0
            # means consistently active, which is great for LPs).
            if vol_ratio >= 2.0:
                # Surging: 2x+ weekly avg = full 15 pts
                score = 15.0
            elif vol_ratio >= 1.0:
                # Above average: linear 8-15 pts
                score = 8.0 + (vol_ratio - 1.0) * 7.0
            elif vol_ratio >= 0.5:
                # Decent but below average: 3-8 pts
                score = 3.0 + (vol_ratio - 0.5) / 0.5 * 5.0
            else:
                # Declining significantly: 0-3 pts
                score = vol_ratio / 0.5 * 3.0
        elif day_vol > 0 and week_avg_vol == 0:
            # Brand new pool with volume but no week data
            score = 7.0

        return round(min(15.0, score), 2)

    # ------------------------------------------------------------------
    # Freshness: pool age weighting (reduced from 15 → 10)
    # ------------------------------------------------------------------

    def _calculate_freshness(self, pool: Dict, base_score: float) -> float:
        """Score pool freshness based on openTime (0-10 pts).

        New pools CAN be great LP opportunities (less competition, high
        initial volume from discovery trading), but they're also riskier.
        So this is a moderate bonus, not a dominant factor.

        ~43% of Raydium V4 pools have openTime=0 (data unavailable),
        including major pools like WSOL/USDC. These get a neutral
        baseline score so they aren't penalised vs pools with timestamps.

        Tiers:
          openTime = 0  → 4 pts (neutral baseline — data unavailable)
          < 1 day old   → 10 pts (if base_score >= 30, else 3)
          1-3 days      → 8 pts
          3-7 days      → 5 pts
          7-14 days     → 2 pts
          > 14 days     → 0 pts
        """
        open_time = pool.get('openTime', 0)

        try:
            open_time = int(open_time)
        except (ValueError, TypeError):
            return 4.0  # no data — neutral baseline

        if open_time == 0:
            # ~43% of V4 pools have openTime=0.  Give neutral baseline
            # so they aren't disadvantaged vs pools with timestamps.
            return 4.0

        age_seconds = time.time() - open_time
        age_days = age_seconds / 86400

        if age_days < 0:
            return 0.0  # bad data

        if age_days < 1:
            # Very new — only reward if base metrics are decent
            return 10.0 if base_score >= 30 else 3.0
        elif age_days < 3:
            return 8.0
        elif age_days < 7:
            return 5.0
        elif age_days < 14:
            return 2.0
        else:
            return 0.0

    # ------------------------------------------------------------------
    # IL safety: use actual price range data from the API
    # ------------------------------------------------------------------

    def _estimate_il_safety(self, pool: Dict) -> float:
        """Estimate IL safety score (0-15 pts).

        Uses day.priceMin / day.priceMax from the V3 API to measure the
        actual 24h price range. Tighter range = less IL = better for LPs.

        Falls back to name-based heuristic if price range data is missing.
        """
        day = pool.get('day', {})
        price_min = day.get('priceMin', 0)
        price_max = day.get('priceMax', 0)

        # Try to use real price range data
        if price_min and price_max and price_min > 0 and price_max > 0:
            # Price range ratio: priceMax / priceMin
            # For a constant-product AMM, IL depends on the magnitude of
            # the price move. A tight range = low IL.
            range_ratio = price_max / price_min
            # range_ratio = 1.0 means no price change (impossible, but ideal)
            # range_ratio = 1.05 means 5% range → very tight, minimal IL
            # range_ratio = 1.20 means 20% range → moderate IL
            # range_ratio = 2.0 means 100% range → severe IL

            if range_ratio <= 1.05:
                return 15.0   # < 5% range, negligible IL
            elif range_ratio <= 1.10:
                return 12.0   # 5-10% range
            elif range_ratio <= 1.20:
                return 9.0    # 10-20% range
            elif range_ratio <= 1.50:
                return 5.0    # 20-50% range
            elif range_ratio <= 2.0:
                return 2.0    # 50-100% range, rough
            else:
                return 0.0    # > 100% range, extreme IL

        # Fallback: name-based heuristic (no price data available)
        name = pool.get('name', '').upper()
        if any(pair in name for pair in ['USDC/USDT', 'USDT/USDC']):
            return 15.0
        if ('SOL' in name or 'WSOL' in name) and any(s in name for s in ['USDC', 'USDT']):
            return 9.0
        # Unknown meme pairs — assume moderate IL risk
        return 5.0

    def rank_pools(self, pools: List[Dict], top_n: int = 10) -> List[Dict]:
        """Score and rank pools, return top N.
        
        Injects component scores into each pool dict for downstream logging.
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
            pool_copy['_il_safety'] = self._estimate_il_safety(pool)

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
