"""
Pool Snapshot Tracker — rolling in-memory window for momentum detection

Stores {timestamp, volume, tvl, price} per pool on every scan cycle.
Computes short-term velocity metrics that the API doesn't provide:
  - Volume velocity: is volume accelerating or decelerating?
  - TVL delta: is liquidity flowing in or draining out?
  - Price stability: tight range = low IL risk (LPs earn fees in both directions)

Typical usage:
  tracker = SnapshotTracker()
  # each scan cycle:
  tracker.record(pool_id, volume, tvl, price)
  bonus = tracker.get_velocity_bonus(pool_id)
"""
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class Snapshot:
    """Single point-in-time observation for a pool."""
    timestamp: float
    volume_24h: float      # cumulative 24h volume from API
    tvl: float             # pool TVL in USD
    price: float           # token price (base/quote ratio)


class SnapshotTracker:
    """Tracks rolling snapshots per pool and computes velocity metrics.

    Parameters:
        max_snapshots: Maximum readings to keep per pool (default 10).
                       At 3-min scan interval → ~30 min observation window.
    """

    def __init__(self, max_snapshots: int = 10):
        self.max_snapshots = max_snapshots
        # pool_id -> deque of Snapshot
        self._history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_snapshots)
        )

    def record(self, pool_id: str, volume_24h: float, tvl: float, price: float):
        """Record a new snapshot for a pool."""
        snap = Snapshot(
            timestamp=time.time(),
            volume_24h=volume_24h,
            tvl=tvl,
            price=price,
        )
        self._history[pool_id].append(snap)

    def get_velocity_bonus(self, pool_id: str) -> float:
        """Compute a velocity bonus score (0–10) for the pool.

        Components:
          - Volume acceleration (0–4 pts): is 24h volume rising between scans?
          - TVL stability (0–3 pts): stable TVL = healthy pool, draining = dying
          - Price stability (0–3 pts): tight range = low IL risk

        Returns 0 if not enough data (need >= 3 snapshots).
        """
        history = self._history.get(pool_id)
        if not history or len(history) < 3:
            return 0.0

        snapshots = list(history)
        bonus = 0.0

        # --- Volume acceleration (0–4 pts) ---
        # Compare volume in recent half vs older half.
        # Volume is a 24h rolling number from the API, so rising = more activity.
        mid = len(snapshots) // 2
        old_vols = [s.volume_24h for s in snapshots[:mid]]
        new_vols = [s.volume_24h for s in snapshots[mid:]]
        avg_old = sum(old_vols) / len(old_vols) if old_vols else 0
        avg_new = sum(new_vols) / len(new_vols) if new_vols else 0

        if avg_old > 0:
            vol_growth = (avg_new - avg_old) / avg_old
            # +20% growth = full 4 pts, linear scale, capped
            bonus += min(4.0, max(0.0, (vol_growth / 0.20) * 4.0))

        # --- TVL stability (0–3 pts) ---
        # For LPs, stable TVL = healthy pool. Draining TVL = pool dying.
        # Growing TVL is neutral-to-slightly-bad (fee dilution) but signals
        # confidence, so we reward stability, not growth.
        first_tvl = snapshots[0].tvl
        last_tvl = snapshots[-1].tvl
        if first_tvl > 0:
            tvl_change = (last_tvl - first_tvl) / first_tvl
            if abs(tvl_change) < 0.05:
                # Stable (< 5% change either way) = full 3 pts
                bonus += 3.0
            elif tvl_change >= 0.05:
                # Growing — mild positive (confidence signal, slight dilution)
                bonus += 1.5
            elif tvl_change > -0.15:
                # Mild drain (5-15%) — partial points
                bonus += max(0.0, 1.5 * (1.0 - abs(tvl_change) / 0.15))
            # > 15% drain = 0 pts (pool may be dying)

        # --- Price stability (0–3 pts) ---
        # LPs earn fees from volume in BOTH directions. What hurts is large
        # price moves (impermanent loss). Reward tight price range.
        prices = [s.price for s in snapshots if s.price > 0]
        if len(prices) >= 2:
            avg_price = sum(prices) / len(prices)
            if avg_price > 0:
                max_deviation = max(abs(p - avg_price) / avg_price for p in prices)
                # < 2% max deviation = full 3 pts (very tight range)
                # 2-10% = partial, > 10% = 0
                if max_deviation <= 0.02:
                    bonus += 3.0
                elif max_deviation < 0.10:
                    bonus += max(0.0, 3.0 * (1.0 - (max_deviation - 0.02) / 0.08))
                # > 10% deviation = 0 pts (wild swings = IL risk)

        return round(min(10.0, bonus), 2)

    def get_summary(self, pool_id: str) -> Optional[Dict]:
        """Return a human-readable summary dict for a pool's recent history."""
        history = self._history.get(pool_id)
        if not history or len(history) < 2:
            return None

        snapshots = list(history)
        first, last = snapshots[0], snapshots[-1]
        window_min = (last.timestamp - first.timestamp) / 60

        vol_delta = 0.0
        tvl_delta = 0.0
        price_delta = 0.0
        if first.volume_24h > 0:
            vol_delta = (last.volume_24h - first.volume_24h) / first.volume_24h * 100
        if first.tvl > 0:
            tvl_delta = (last.tvl - first.tvl) / first.tvl * 100
        if first.price > 0:
            price_delta = (last.price - first.price) / first.price * 100

        return {
            'snapshots': len(snapshots),
            'window_minutes': round(window_min, 1),
            'volume_change_pct': round(vol_delta, 2),
            'tvl_change_pct': round(tvl_delta, 2),
            'price_change_pct': round(price_delta, 2),
            'velocity_bonus': self.get_velocity_bonus(pool_id),
        }

    def pool_count(self) -> int:
        """Number of pools being tracked."""
        return len(self._history)

    def clear_pool(self, pool_id: str):
        """Remove all snapshots for a pool (e.g. after exit)."""
        self._history.pop(pool_id, None)
