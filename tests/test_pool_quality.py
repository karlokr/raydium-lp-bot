"""Tests for bot/analysis/pool_quality.py — safety analysis and filtering."""
from unittest.mock import MagicMock, patch
import pytest

from bot.analysis.pool_quality import PoolQualityAnalyzer


@pytest.fixture
def analyzer():
    qa = PoolQualityAnalyzer()
    qa.rugcheck = MagicMock()
    qa.lp_lock = MagicMock()
    return qa


def _safe_rugcheck():
    return {
        "available": True,
        "risk_score": 10,
        "risk_level": "low",
        "is_rugged": False,
        "dangers": [],
        "warnings": [],
        "has_freeze_authority": False,
        "has_mint_authority": False,
        "has_mutable_metadata": False,
        "low_lp_providers": False,
        "top5_holder_pct": 15,
        "top10_holder_pct": 25,
        "max_single_holder_pct": 8,
        "total_holders": 500,
    }


def _safe_lp_lock():
    return {
        "available": True,
        "safe_pct": 90,
        "max_single_unlocked_pct": 5,
        "unlocked_pct": 10,
    }


class TestAnalyzePoolBasicChecks:

    def test_safe_pool_passes(self, analyzer, sample_pool):
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is True
        assert result["risk_level"] == "LOW"

    def test_low_burn_percent_rejected(self, analyzer, sample_pool):
        sample_pool["burnPercent"] = 30
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False
        assert any("LP burn" in r for r in result["risks"])

    def test_extreme_apr_rejected(self, analyzer, sample_pool):
        sample_pool["day"]["apr"] = 2100  # Above 2000 threshold
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert any("Extreme APR" in r for r in result["risks"])

    def test_rug_pull_pattern(self, analyzer):
        pool = {
            "tvl": 3_000, "liquidity": 3_000,
            "burnPercent": 90,
            "day": {"apr": 600, "volume": 2000},
            "baseMint": "token", "quoteMint": "So11111111111111111111111111111111111111112",
        }
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        result = analyzer.analyze_pool(pool)
        assert result["is_safe"] is False
        assert any("rug pull" in r.lower() for r in result["risks"])

    def test_low_tvl_warning(self, analyzer, sample_pool):
        sample_pool["tvl"] = 40_000
        sample_pool["liquidity"] = 40_000
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert any("Low liquidity" in w for w in result["warnings"])


class TestAnalyzePoolRugCheck:

    def test_rugged_token(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["is_rugged"] = True
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_high_risk_score(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["risk_score"] = 60
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_freeze_authority(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["has_freeze_authority"] = True
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False
        assert any("freeze" in r.lower() for r in result["risks"])

    def test_mint_authority(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["has_mint_authority"] = True
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_mutable_metadata(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["has_mutable_metadata"] = True
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_high_top10_concentration(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["top10_holder_pct"] = 50
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_whale_holder(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["max_single_holder_pct"] = 30  # Above 25% threshold
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_low_holders(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["total_holders"] = 50
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_danger_items_rejected(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["dangers"] = ["Massive supply increase detected"]
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_rugcheck_unavailable_rejects_pool(self, analyzer, sample_pool):
        rc = _safe_rugcheck()
        rc["available"] = False
        analyzer.rugcheck.analyze_token_safety.return_value = rc
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False
        assert any("unavailable" in r.lower() for r in result["risks"])


class TestShortCircuit:

    def test_lp_lock_skipped_when_risks_present(self, analyzer, sample_pool):
        """If rugcheck already rejects, LP lock RPC should NOT be called."""
        sample_pool["burnPercent"] = 20  # triggers risk
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False
        assert result["lp_lock"] is None
        analyzer.lp_lock.analyze_lp_lock.assert_not_called()


class TestCheckSafetyFalse:

    def test_skips_rugcheck_and_lp_lock(self, analyzer, sample_pool):
        result = analyzer.analyze_pool(sample_pool, check_safety=False)
        analyzer.rugcheck.analyze_token_safety.assert_not_called()
        analyzer.lp_lock.analyze_lp_lock.assert_not_called()
        assert result["is_safe"] is True  # high burn, no safety checks = safe


class TestLpLockAnalysis:

    def test_unsafe_lp_lock_rejected(self, analyzer, sample_pool):
        # With burnPercent=95, even 80% of remaining is only 4% of total.
        # Lower burn so the whale actually exceeds the threshold.
        sample_pool["burnPercent"] = 20  # only 20% burned → 80% circulating
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        lp = _safe_lp_lock()
        lp["max_single_unlocked_pct"] = 80  # whale holds 80% of 80% = 64% total
        lp["safe_pct"] = 10
        analyzer.lp_lock.analyze_lp_lock.return_value = lp
        result = analyzer.analyze_pool(sample_pool)
        assert result["is_safe"] is False

    def test_lp_lock_unavailable_rejects_pool(self, analyzer, sample_pool):
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        analyzer.lp_lock.analyze_lp_lock.return_value = {"available": False}
        result = analyzer.analyze_pool(sample_pool)
        # LP lock unavailable = cannot verify safety = reject
        assert result["is_safe"] is False
        assert any("unavailable" in r.lower() for r in result["risks"])


class TestGetSafePools:

    def test_filters_risky_pools(self):
        safe_pool = {
            "tvl": 100_000, "burnPercent": 99,
            "day": {"apr": 100, "volume": 50_000},
            "baseMint": "tok", "quoteMint": "So11111111111111111111111111111111111111112",
            "lpMint": {"address": "lp1"},
        }
        risky_pool = {
            "tvl": 2_000, "burnPercent": 10,
            "day": {"apr": 3000, "volume": 500},
            "baseMint": "tok", "quoteMint": "So11111111111111111111111111111111111111112",
            "lpMint": {"address": "lp2"},
        }
        mock_analyzer = MagicMock()
        mock_analyzer.analyze_pool.side_effect = [
            {"is_safe": True},
            {"is_safe": False},
        ]
        result = PoolQualityAnalyzer.get_safe_pools(
            [safe_pool, risky_pool], check_locks=True, analyzer=mock_analyzer
        )
        assert len(result) == 1

    def test_creates_default_analyzer_when_none(self):
        # Just verify it doesn't crash (will create a real instance)
        with patch.object(PoolQualityAnalyzer, 'analyze_pool', return_value={"is_safe": True}):
            result = PoolQualityAnalyzer.get_safe_pools([{"test": True}])
            assert len(result) == 1


class TestReturnStructure:

    def test_result_keys(self, analyzer, sample_pool):
        analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
        analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
        result = analyzer.analyze_pool(sample_pool)
        assert "risk_level" in result
        assert "risks" in result
        assert "warnings" in result
        assert "is_safe" in result
        assert "burn_percent" in result
        assert "liquidity_tier" in result

    def test_liquidity_tiers(self, analyzer):
        for tvl, expected in [(200_000, "high"), (75_000, "medium"), (10_000, "low")]:
            pool = {
                "tvl": tvl, "burnPercent": 99,
                "day": {"apr": 100, "volume": 50_000},
                "baseMint": "t", "quoteMint": "So11111111111111111111111111111111111111112",
                "lpMint": {"address": "lp"},
            }
            analyzer.rugcheck.analyze_token_safety.return_value = _safe_rugcheck()
            analyzer.lp_lock.analyze_lp_lock.return_value = _safe_lp_lock()
            result = analyzer.analyze_pool(pool)
            assert result["liquidity_tier"] == expected
