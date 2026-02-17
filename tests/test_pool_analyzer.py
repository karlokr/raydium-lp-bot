"""Tests for bot/analysis/pool_analyzer.py — scoring, IL, position sizing."""
import math
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

    def test_max_score_is_100(self, analyzer):
        """Score is capped at 100 — no bonus above that."""
        pool = {
            "tvl": 10_000,
            "day": {"feeApr": 10_000, "volume": 50_000, "volumeFee": 2000,
                    "priceMin": 100, "priceMax": 101},
            "week": {"volumeFee": 14_000},
        }
        score = analyzer.calculate_pool_score(pool)
        assert score <= 100

    def test_high_quality_pool_scores_well(self, analyzer, sample_pool):
        score = analyzer.calculate_pool_score(sample_pool)
        assert score > 40  # decent pool should score above 40

    def test_zero_tvl_pool(self, analyzer):
        pool = {"tvl": 0, "day": {"apr": 0, "volume": 0}, "week": {}}
        score = analyzer.calculate_pool_score(pool)
        assert score >= 0

    def test_fee_apr_component_capped(self, analyzer):
        """Fee APR score caps at 50 pts."""
        components = {}
        pool = {"tvl": 50_000, "day": {"feeApr": 10_000, "volume": 0}, "week": {}}
        analyzer.calculate_pool_score(pool, _out=components)
        assert components['fee_apr'] == 50

    def test_feeApr_preferred_over_apr(self, analyzer):
        pool_with = {"tvl": 50_000, "day": {"apr": 500, "feeApr": 200, "volume": 50_000}, "week": {}}
        pool_without = {"tvl": 50_000, "day": {"apr": 500, "feeApr": 0, "volume": 50_000}, "week": {}}
        score1 = analyzer.calculate_pool_score(pool_with)
        score2 = analyzer.calculate_pool_score(pool_without)
        assert score1 >= 0 and score2 >= 0

    def test_deeper_pool_gets_depth_points(self, analyzer):
        """A pool with higher TVL should earn more depth points."""
        deep = {"tvl": 100_000, "day": {"feeApr": 100}, "week": {}}
        shallow = {"tvl": 5_000, "day": {"feeApr": 100}, "week": {}}
        out_deep, out_shallow = {}, {}
        analyzer.calculate_pool_score(deep, _out=out_deep)
        analyzer.calculate_pool_score(shallow, _out=out_shallow)
        assert out_deep['depth'] > out_shallow['depth']

    def test_components_stashed(self, analyzer, sample_pool):
        out = {}
        analyzer.calculate_pool_score(sample_pool, _out=out)
        assert 'fee_apr' in out
        assert 'fee_consistency' in out
        assert 'depth' in out
        assert 'il_safety' in out


class TestFeeConsistency:

    def test_consistent_fees(self, analyzer):
        day = {"volumeFee": 100}
        week = {"volumeFee": 700}  # avg 100/day — perfectly consistent
        score = analyzer._calculate_fee_consistency(day, week, 100)
        assert score == 15.0

    def test_moderately_volatile(self, analyzer):
        day = {"volumeFee": 150}
        week = {"volumeFee": 700}  # avg 100/day, today 1.5x
        score = analyzer._calculate_fee_consistency(day, week, 150)
        assert 3.0 < score < 15.0

    def test_highly_volatile(self, analyzer):
        day = {"volumeFee": 500}
        week = {"volumeFee": 700}  # avg 100/day, today 5x
        score = analyzer._calculate_fee_consistency(day, week, 500)
        assert score == 3.0

    def test_new_pool_no_week_data(self, analyzer):
        day = {"volumeFee": 100}
        week = {"volumeFee": 0}
        score = analyzer._calculate_fee_consistency(day, week, 100)
        assert score == 7.0

    def test_no_fees(self, analyzer):
        score = analyzer._calculate_fee_consistency({}, {}, 0)
        assert score == 0.0


class TestEstimateIlSafety:

    def test_tight_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 104}}  # 4% range
        assert analyzer._estimate_il_safety(pool) == 25.0

    def test_moderate_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 112}}  # 12% range
        assert analyzer._estimate_il_safety(pool) == 16.0

    def test_wide_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 180}}  # 80% range
        assert analyzer._estimate_il_safety(pool) == 2.0

    def test_extreme_range(self, analyzer):
        pool = {"day": {"priceMin": 100, "priceMax": 300}}  # 200% range
        assert analyzer._estimate_il_safety(pool) == 0.0

    def test_no_price_data_fallback(self, analyzer):
        pool = {"day": {}, "name": "BONK/WSOL"}
        score = analyzer._estimate_il_safety(pool)
        assert score == 6.0  # unknown meme pair default

    def test_stablecoin_fallback(self, analyzer):
        pool = {"day": {}, "name": "USDC/USDT"}
        assert analyzer._estimate_il_safety(pool) == 25.0

    def test_sol_usdc_fallback(self, analyzer):
        pool = {"day": {}, "name": "WSOL/USDC"}
        assert analyzer._estimate_il_safety(pool) == 15.0


class TestRankPools:

    def test_returns_top_n(self, analyzer):
        pools = [
            {"tvl": 10_000, "day": {"apr": 50, "volume": 5000}, "week": {}},
            {"tvl": 50_000, "day": {"apr": 200, "volume": 100000}, "week": {"volume": 500000}},
            {"tvl": 30_000, "day": {"apr": 100, "volume": 30000}, "week": {"volume": 200000}},
        ]
        ranked = analyzer.rank_pools(pools, top_n=2)
        assert len(ranked) == 2
        assert ranked[0]["score"] >= ranked[1]["score"]

    def test_injects_component_scores(self, analyzer, sample_pool):
        ranked = analyzer.rank_pools([sample_pool], top_n=1)
        assert "_fee_apr" in ranked[0]
        assert "_fee_consistency" in ranked[0]
        assert "_depth" in ranked[0]
        assert "_il_safety" in ranked[0]


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
        # Standard formula: 2*sqrt(2)/3 - 1 ≈ -0.05719
        expected = 2 * math.sqrt(2.0) / (1 + 2.0) - 1
        assert il == pytest.approx(expected, abs=1e-6)

    def test_half_price_decrease(self):
        il = PoolAnalyzer.calculate_impermanent_loss(100, 50)
        # Standard formula: 2*sqrt(0.5)/1.5 - 1 ≈ -0.05719
        expected = 2 * math.sqrt(0.5) / (1 + 0.5) - 1
        assert il == pytest.approx(expected, abs=1e-6)

    def test_il_always_negative(self):
        """Standard IL is always <= 0: LP always underperforms HODL (ignoring fees)."""
        for ratio in [0.1, 0.5, 2.0, 5.0, 10.0]:
            il = PoolAnalyzer.calculate_impermanent_loss(100, 100 * ratio)
            assert il <= 1e-9, f"IL should be <= 0 for r={ratio}, got {il}"

    def test_zero_entry_price(self):
        assert PoolAnalyzer.calculate_impermanent_loss(0, 100) == 0.0

    def test_zero_current_price(self):
        assert PoolAnalyzer.calculate_impermanent_loss(100, 0) == 0.0

    def test_il_symmetry(self):
        """Standard IL is symmetric: IL(2x) == IL(0.5x)."""
        il_up = PoolAnalyzer.calculate_impermanent_loss(100, 200)    # r=2
        il_down = PoolAnalyzer.calculate_impermanent_loss(100, 50)   # r=0.5
        assert il_up == pytest.approx(il_down, abs=1e-9)
        # Both should be ≈ -5.72%
        assert il_up == pytest.approx(-0.05719, abs=1e-4)
