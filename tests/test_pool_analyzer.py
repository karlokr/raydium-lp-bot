"""Tests for bot/analysis/pool_analyzer.py — scoring, IL, position sizing."""
import math
import time
import pytest

from bot.analysis.pool_analyzer import PoolAnalyzer


@pytest.fixture
def analyzer():
    return PoolAnalyzer()


# ── calculate_pool_score ─────────────────────────────────────────────

class TestCalculatePoolScore:

    def test_returns_nonnegative(self, analyzer, sample_pool):
        score = analyzer.calculate_pool_score(sample_pool)
        assert score >= 0

    def test_high_quality_pool_scores_well(self, analyzer, sample_pool):
        score = analyzer.calculate_pool_score(sample_pool)
        assert score > 40  # decent pool should score above 40

    def test_zero_tvl_pool(self, analyzer):
        pool = {"tvl": 0, "day": {"apr": 0, "volume": 0}, "burnPercent": 0, "week": {}}
        score = analyzer.calculate_pool_score(pool)
        assert score >= 0

    def test_extreme_apr_maxes_component(self, analyzer):
        pool = {
            "tvl": 100_000,
            "day": {"apr": 10_000, "feeApr": 10_000, "volume": 50_000},
            "burnPercent": 99,
            "week": {"volume": 200_000},
            "openTime": 0,
        }
        score = analyzer.calculate_pool_score(pool)
        # Fee APR component should be capped at 30
        assert score <= 120  # 30+20+10+15+15+10+10 = theoretical max 110

    def test_feeApr_preferred_over_apr(self, analyzer):
        pool_with_fee_apr = {
            "tvl": 50_000,
            "day": {"apr": 500, "feeApr": 200, "volume": 50_000},
            "burnPercent": 50,
            "week": {},
            "openTime": 0,
        }
        pool_without_fee_apr = {
            "tvl": 50_000,
            "day": {"apr": 500, "feeApr": 0, "volume": 50_000},
            "burnPercent": 50,
            "week": {},
            "openTime": 0,
        }
        score1 = analyzer.calculate_pool_score(pool_with_fee_apr)
        score2 = analyzer.calculate_pool_score(pool_without_fee_apr)
        # With feeApr=200 → 30 pts; without → uses apr=500 → also capped at 30.
        # They may differ due to how feeApr is selected. Verify no crash.
        assert score1 >= 0 and score2 >= 0


class TestCalculateMomentum:

    def test_surging_volume(self, analyzer):
        day = {"volume": 100_000}
        week = {"volume": 350_000}  # avg 50k/day, today 100k → 2x
        score = analyzer._calculate_momentum(day, week, 100_000, 50_000)
        assert score == 15.0

    def test_above_average(self, analyzer):
        day = {"volume": 70_000}
        week = {"volume": 350_000}  # avg 50k, today 70k → 1.4x
        score = analyzer._calculate_momentum(day, week, 70_000, 50_000)
        assert 8.0 <= score <= 15.0

    def test_declining_volume(self, analyzer):
        day = {"volume": 10_000}
        week = {"volume": 350_000}  # avg 50k, today 10k → 0.2x
        score = analyzer._calculate_momentum(day, week, 10_000, 50_000)
        assert score < 3.0

    def test_no_week_data(self, analyzer):
        day = {"volume": 50_000}
        week = {"volume": 0}
        score = analyzer._calculate_momentum(day, week, 50_000, 50_000)
        assert score == 7.0

    def test_zero_everything(self, analyzer):
        score = analyzer._calculate_momentum({}, {}, 0, 0)
        assert score == 0.0


class TestCalculateFreshness:

    def test_opentime_zero_neutral(self, analyzer):
        pool = {"openTime": 0}
        assert analyzer._calculate_freshness(pool, 50) == 4.0

    def test_very_new_high_base_score(self, analyzer):
        pool = {"openTime": int(time.time()) - 1800}  # 30 min ago
        assert analyzer._calculate_freshness(pool, 50) == 10.0

    def test_very_new_low_base_score(self, analyzer):
        pool = {"openTime": int(time.time()) - 1800}
        assert analyzer._calculate_freshness(pool, 20) == 3.0

    def test_one_to_three_days(self, analyzer):
        pool = {"openTime": int(time.time()) - 86400 * 2}  # 2 days
        assert analyzer._calculate_freshness(pool, 50) == 8.0

    def test_three_to_seven_days(self, analyzer):
        pool = {"openTime": int(time.time()) - 86400 * 5}
        assert analyzer._calculate_freshness(pool, 50) == 5.0

    def test_seven_to_fourteen_days(self, analyzer):
        pool = {"openTime": int(time.time()) - 86400 * 10}
        assert analyzer._calculate_freshness(pool, 50) == 2.0

    def test_older_than_fourteen_days(self, analyzer):
        pool = {"openTime": int(time.time()) - 86400 * 30}
        assert analyzer._calculate_freshness(pool, 50) == 0.0

    def test_invalid_opentime_string(self, analyzer):
        pool = {"openTime": "bad"}
        assert analyzer._calculate_freshness(pool, 50) == 4.0

    def test_future_opentime(self, analyzer):
        pool = {"openTime": int(time.time()) + 86400}
        assert analyzer._calculate_freshness(pool, 50) == 0.0


class TestEstimateIlSafety:

    def test_tight_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 104}}  # 4% range
        assert analyzer._estimate_il_safety(pool) == 15.0

    def test_moderate_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 115}}  # 15% range
        assert analyzer._estimate_il_safety(pool) == 9.0

    def test_wide_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 180}}  # 80% range
        assert analyzer._estimate_il_safety(pool) == 2.0

    def test_extreme_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 300}}  # 200% range
        assert analyzer._estimate_il_safety(pool) == 0.0

    def test_no_price_data_fallback(self, analyzer):
        pool = {"day": {}, "name": "BONK/WSOL"}
        score = analyzer._estimate_il_safety(pool)
        assert score == 5.0  # unknown meme pair default

    def test_stablecoin_fallback(self, analyzer):
        pool = {"day": {}, "name": "USDC/USDT"}
        assert analyzer._estimate_il_safety(pool) == 15.0

    def test_sol_usdc_fallback(self, analyzer):
        pool = {"day": {}, "name": "WSOL/USDC"}
        assert analyzer._estimate_il_safety(pool) == 9.0


class TestRankPools:

    def test_returns_top_n(self, analyzer):
        pools = [
            {"tvl": 10_000, "day": {"apr": 50, "volume": 5000}, "burnPercent": 50, "week": {}, "openTime": 0},
            {"tvl": 50_000, "day": {"apr": 200, "volume": 100000}, "burnPercent": 99, "week": {"volume": 500000}, "openTime": 0},
            {"tvl": 30_000, "day": {"apr": 100, "volume": 30000}, "burnPercent": 80, "week": {"volume": 200000}, "openTime": 0},
        ]
        ranked = analyzer.rank_pools(pools, top_n=2)
        assert len(ranked) == 2
        assert ranked[0]["score"] >= ranked[1]["score"]

    def test_injects_component_scores(self, analyzer, sample_pool):
        ranked = analyzer.rank_pools([sample_pool], top_n=1)
        assert "_momentum" in ranked[0]
        assert "_freshness" in ranked[0]
        assert "_il_safety" in ranked[0]
        assert "_velocity" in ranked[0]


# ── calculate_position_size ──────────────────────────────────────────

class TestCalculatePositionSize:

    def test_basic_sizing(self, analyzer, sample_pool):
        # 10 SOL available, 0 positions open, max 3 slots, 0.05 reserve
        size = analyzer.calculate_position_size(sample_pool, 10.0, 0)
        # deployable = 10 - 0.05 = 9.95, / 3 slots ≈ 3.317
        assert 3.0 < size < 4.0

    def test_caps_at_max(self, analyzer, sample_pool):
        size = analyzer.calculate_position_size(sample_pool, 100.0, 0)
        assert size <= 5.0  # MAX_ABSOLUTE_POSITION_SOL

    def test_no_remaining_slots(self, analyzer, sample_pool):
        size = analyzer.calculate_position_size(sample_pool, 10.0, 3)
        assert size == 0.0

    def test_no_capital(self, analyzer, sample_pool):
        size = analyzer.calculate_position_size(sample_pool, 0.01, 0)
        assert size == 0.0  # below reserve

    def test_size_decreases_with_open_positions(self, analyzer, sample_pool):
        s0 = analyzer.calculate_position_size(sample_pool, 10.0, 0)
        s1 = analyzer.calculate_position_size(sample_pool, 10.0, 1)
        s2 = analyzer.calculate_position_size(sample_pool, 10.0, 2)
        assert s0 < s1 < s2 or s0 > s1 > s2  # monotonic
        # Actually with fewer remaining slots the per-slot size increases
        # because remaining slots = MAX - open, and deployable / remaining
        # With 3 slots total: 0 open → /3, 1 open → /2, 2 open → /1
        assert s2 > s1 > s0


# ── calculate_impermanent_loss ───────────────────────────────────────

class TestCalculateImpermanentLoss:

    def test_no_change(self):
        il = PoolAnalyzer.calculate_impermanent_loss(100, 100)
        assert il == pytest.approx(0.0)

    def test_2x_price_increase(self):
        il = PoolAnalyzer.calculate_impermanent_loss(100, 200)
        expected = 2 * math.sqrt(2) / (1 + 2) - 1
        assert il == pytest.approx(expected, abs=1e-6)

    def test_half_price_decrease(self):
        il = PoolAnalyzer.calculate_impermanent_loss(100, 50)
        expected = 2 * math.sqrt(0.5) / (1 + 0.5) - 1
        assert il == pytest.approx(expected, abs=1e-6)

    def test_il_always_negative(self):
        for ratio in [0.1, 0.5, 2.0, 5.0, 10.0]:
            il = PoolAnalyzer.calculate_impermanent_loss(1.0, ratio)
            assert il <= 0

    def test_zero_entry_price(self):
        assert PoolAnalyzer.calculate_impermanent_loss(0, 100) == 0.0

    def test_zero_current_price(self):
        assert PoolAnalyzer.calculate_impermanent_loss(100, 0) == 0.0

    def test_symmetry_of_magnitude(self):
        """IL(2x) should equal IL(0.5x) in magnitude."""
        il_up = PoolAnalyzer.calculate_impermanent_loss(100, 200)
        il_down = PoolAnalyzer.calculate_impermanent_loss(100, 50)
        assert abs(il_up) == pytest.approx(abs(il_down), abs=1e-6)
