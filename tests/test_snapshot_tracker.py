"""Tests for bot/analysis/snapshot_tracker.py — rolling snapshots and velocity."""
import time
import pytest

from bot.analysis.snapshot_tracker import Snapshot, SnapshotTracker


class TestSnapshot:

    def test_fields(self):
        s = Snapshot(timestamp=1000, volume_24h=5000, tvl=100_000, price=1.5)
        assert s.timestamp == 1000
        assert s.volume_24h == 5000
        assert s.tvl == 100_000
        assert s.price == 1.5


class TestSnapshotTrackerRecord:

    def test_record_single(self):
        t = SnapshotTracker()
        t.record("pool1", 1000, 50_000, 1.0)
        assert t.pool_count() == 1

    def test_record_multiple_pools(self):
        t = SnapshotTracker()
        t.record("a", 100, 1000, 1.0)
        t.record("b", 200, 2000, 2.0)
        assert t.pool_count() == 2

    def test_max_snapshots_respected(self):
        t = SnapshotTracker(max_snapshots=3)
        for i in range(5):
            t.record("pool1", i * 100, 1000, 1.0)
        assert len(t._history["pool1"]) == 3

    def test_oldest_evicted(self):
        t = SnapshotTracker(max_snapshots=3)
        for i in range(5):
            t.record("pool1", (i + 1) * 100, 1000, 1.0)
        # oldest (100) and second (200) should be gone
        snaps = list(t._history["pool1"])
        assert snaps[0].volume_24h == 300


class TestGetVelocityBonus:

    def test_insufficient_data(self):
        t = SnapshotTracker()
        t.record("pool1", 1000, 50_000, 1.0)
        t.record("pool1", 1100, 50_000, 1.0)
        assert t.get_velocity_bonus("pool1") == 0.0

    def test_unknown_pool(self):
        t = SnapshotTracker()
        assert t.get_velocity_bonus("nonexistent") == 0.0

    def test_stable_pool_high_bonus(self):
        """Stable TVL, stable price, rising volume → high bonus."""
        t = SnapshotTracker(max_snapshots=6)
        base = time.time()
        # Old half: lower volume
        for i in range(3):
            t.record("p", 10_000, 100_000, 1.00)
        # New half: higher volume (+25% growth)
        for i in range(3):
            t.record("p", 12_500, 100_000, 1.00)

        bonus = t.get_velocity_bonus("p")
        # Volume accel: +25% → (0.25/0.20)*4 = 5 → capped at 4
        # TVL stable: 3 pts
        # Price stable: 3 pts
        assert bonus == pytest.approx(10.0)

    def test_volume_accelerating(self):
        t = SnapshotTracker(max_snapshots=6)
        for i in range(3):
            t.record("p", 1000, 50_000, 1.0)
        for i in range(3):
            t.record("p", 1400, 50_000, 1.0)  # +40% volume growth
        bonus = t.get_velocity_bonus("p")
        assert bonus >= 4.0  # at least volume component

    def test_draining_tvl_penalized(self):
        t = SnapshotTracker(max_snapshots=4)
        t.record("p", 1000, 100_000, 1.0)
        t.record("p", 1000, 100_000, 1.0)
        t.record("p", 1000, 80_000, 1.0)   # -20% TVL drain
        t.record("p", 1000, 80_000, 1.0)
        bonus = t.get_velocity_bonus("p")
        # TVL stability component should be 0 (>15% drain)
        # Volume: 0 growth, price: stable
        assert bonus <= 6.0  # max: 0 vol + 0 tvl + 3 price

    def test_wild_price_swings_penalized(self):
        t = SnapshotTracker(max_snapshots=4)
        t.record("p", 1000, 50_000, 1.0)
        t.record("p", 1000, 50_000, 1.3)  # +30% move
        t.record("p", 1000, 50_000, 0.7)  # -46% from peak
        t.record("p", 1000, 50_000, 1.0)
        bonus = t.get_velocity_bonus("p")
        # Price stability should be 0 (>10% deviation)
        assert bonus <= 7.0

    def test_bonus_capped_at_10(self):
        t = SnapshotTracker(max_snapshots=6)
        for i in range(3):
            t.record("p", 1000, 100_000, 1.0)
        for i in range(3):
            t.record("p", 2000, 100_000, 1.0)  # huge volume growth
        bonus = t.get_velocity_bonus("p")
        assert bonus <= 10.0


class TestGetSummary:

    def test_not_enough_data(self):
        t = SnapshotTracker()
        t.record("p", 1000, 50_000, 1.0)
        assert t.get_summary("p") is None

    def test_summary_fields(self):
        t = SnapshotTracker()
        t.record("p", 1000, 50_000, 1.0)
        t.record("p", 1200, 52_000, 1.02)
        s = t.get_summary("p")
        assert "snapshots" in s
        assert "window_minutes" in s
        assert "volume_change_pct" in s
        assert "tvl_change_pct" in s
        assert "price_change_pct" in s
        assert "velocity_bonus" in s

    def test_volume_change(self):
        t = SnapshotTracker()
        t.record("p", 1000, 50_000, 1.0)
        t.record("p", 1500, 50_000, 1.0)
        s = t.get_summary("p")
        assert s["volume_change_pct"] == pytest.approx(50.0)

    def test_unknown_pool(self):
        t = SnapshotTracker()
        assert t.get_summary("nope") is None


class TestPoolCount:

    def test_empty(self):
        assert SnapshotTracker().pool_count() == 0

    def test_counts_unique_pools(self):
        t = SnapshotTracker()
        t.record("a", 1, 1, 1)
        t.record("b", 1, 1, 1)
        t.record("a", 2, 2, 2)
        assert t.pool_count() == 2


class TestClearPool:

    def test_removes_pool(self):
        t = SnapshotTracker()
        t.record("a", 1, 1, 1)
        t.record("b", 1, 1, 1)
        t.clear_pool("a")
        assert t.pool_count() == 1

    def test_clearing_nonexistent_is_noop(self):
        t = SnapshotTracker()
        t.clear_pool("nope")  # should not raise
        assert t.pool_count() == 0
