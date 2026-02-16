"""Tests for bot/safety/rugcheck.py — RugCheck API integration."""
import time
import requests
from unittest.mock import patch, MagicMock
import pytest

from bot.safety.rugcheck import RugCheckAPI


@pytest.fixture
def api():
    return RugCheckAPI()


def _full_report(**overrides):
    """Build a realistic RugCheck report dict."""
    report = {
        "score_normalised": 15,
        "rugged": False,
        "risks": [],
        "topHolders": [
            {"pct": 8.0}, {"pct": 5.0}, {"pct": 4.0},
            {"pct": 3.0}, {"pct": 2.0}, {"pct": 1.5},
            {"pct": 1.0}, {"pct": 0.8}, {"pct": 0.5}, {"pct": 0.3},
        ],
        "totalHolders": 1200,
    }
    report.update(overrides)
    return report


class TestGetTokenReport:

    @patch("bot.safety.rugcheck.requests.get")
    def test_success(self, mock_get, api):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: _full_report(),
        )
        report = api.get_token_report("mintABC")
        assert report is not None
        assert report["score_normalised"] == 15

    @patch("bot.safety.rugcheck.requests.get")
    def test_caching(self, mock_get, api):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: _full_report())
        api.get_token_report("mintX")
        api.get_token_report("mintX")
        mock_get.assert_called_once()

    @patch("bot.safety.rugcheck.requests.get")
    def test_404(self, mock_get, api):
        mock_get.return_value = MagicMock(status_code=404)
        assert api.get_token_report("bad") is None

    @patch("bot.safety.rugcheck.requests.get")
    def test_server_error(self, mock_get, api):
        mock_get.return_value = MagicMock(status_code=500)
        assert api.get_token_report("err") is None

    @patch("bot.safety.rugcheck.requests.get", side_effect=requests.RequestException("timeout"))
    def test_exception(self, _, api):
        assert api.get_token_report("fail") is None


class TestAnalyzeTokenSafety:

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_unavailable(self, mock_report, api):
        mock_report.return_value = None
        result = api.analyze_token_safety("unknown")
        assert result["available"] is False
        assert result["risk_score"] == 100

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_safe_token(self, mock_report, api):
        mock_report.return_value = _full_report()
        result = api.analyze_token_safety("safe_token")
        assert result["available"] is True
        assert result["risk_level"] == "medium"  # score 15 → medium
        assert result["is_rugged"] is False
        assert len(result["dangers"]) == 0

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_low_risk(self, mock_report, api):
        mock_report.return_value = _full_report(score_normalised=5)
        result = api.analyze_token_safety("t")
        assert result["risk_level"] == "low"

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_high_risk(self, mock_report, api):
        mock_report.return_value = _full_report(score_normalised=60)
        result = api.analyze_token_safety("t")
        assert result["risk_level"] == "high"

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_rugged_flag(self, mock_report, api):
        mock_report.return_value = _full_report(rugged=True)
        result = api.analyze_token_safety("t")
        assert result["is_rugged"] is True

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_danger_risks_parsed(self, mock_report, api):
        mock_report.return_value = _full_report(risks=[
            {"level": "danger", "name": "Mint Authority", "description": "Unlimited minting"},
            {"level": "warn", "name": "Low Volume", "description": "Low 24h volume"},
        ])
        result = api.analyze_token_safety("t")
        assert len(result["dangers"]) == 1
        assert "Mint Authority" in result["dangers"][0]
        assert len(result["warnings"]) == 1

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_freeze_authority_detected(self, mock_report, api):
        mock_report.return_value = _full_report(risks=[
            {"level": "danger", "name": "Freeze Authority Enabled", "description": "Owner can freeze tokens"},
        ])
        result = api.analyze_token_safety("t")
        assert result["has_freeze_authority"] is True

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_mint_authority_detected(self, mock_report, api):
        mock_report.return_value = _full_report(risks=[
            {"level": "warn", "name": "Mint Authority Still Active", "description": ""},
        ])
        result = api.analyze_token_safety("t")
        assert result["has_mint_authority"] is True

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_mutable_metadata(self, mock_report, api):
        mock_report.return_value = _full_report(risks=[
            {"level": "warn", "name": "Mutable Metadata", "description": "Metadata can change"},
        ])
        result = api.analyze_token_safety("t")
        assert result["has_mutable_metadata"] is True

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_low_lp_providers(self, mock_report, api):
        mock_report.return_value = _full_report(risks=[
            {"level": "warn", "name": "Low LP Providers", "description": "Only 2 providers"},
        ])
        result = api.analyze_token_safety("t")
        assert result["low_lp_providers"] is True

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_holder_concentration(self, mock_report, api):
        holders = [{"pct": 20.0}, {"pct": 15.0}, {"pct": 10.0}, {"pct": 5.0}, {"pct": 4.0}]
        mock_report.return_value = _full_report(topHolders=holders)
        result = api.analyze_token_safety("t")
        assert result["top5_holder_pct"] == pytest.approx(54.0)
        assert result["top10_holder_pct"] == pytest.approx(54.0)  # only 5 holders
        assert result["max_single_holder_pct"] == pytest.approx(20.0)

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_empty_holders(self, mock_report, api):
        mock_report.return_value = _full_report(topHolders=[])
        result = api.analyze_token_safety("t")
        assert result["top5_holder_pct"] == 0
        assert result["max_single_holder_pct"] == 0

    @patch.object(RugCheckAPI, 'get_token_report')
    def test_total_holders(self, mock_report, api):
        mock_report.return_value = _full_report(totalHolders=5000)
        result = api.analyze_token_safety("t")
        assert result["total_holders"] == 5000
