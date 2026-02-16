"""Tests for bot/analysis/price_tracker.py — price extraction."""
import pytest
from unittest.mock import MagicMock

from bot.analysis.price_tracker import PriceTracker


@pytest.fixture
def tracker(mock_api_client):
    return PriceTracker(mock_api_client)


class TestPriceFromPool:

    def test_reserves_ratio(self, tracker):
        pool = {"mintAmountA": 1_000, "mintAmountB": 50_000}
        assert tracker._price_from_pool(pool) == pytest.approx(50.0)

    def test_price_field_fallback(self, tracker):
        pool = {"mintAmountA": 0, "mintAmountB": 0, "price": 42.5}
        assert tracker._price_from_pool(pool) == 42.5

    def test_no_data_returns_zero(self, tracker):
        assert tracker._price_from_pool({}) == 0

    def test_invalid_reserves(self, tracker):
        pool = {"mintAmountA": "bad", "mintAmountB": "data", "price": 0}
        assert tracker._price_from_pool(pool) == 0

    def test_negative_amounts_return_zero(self, tracker):
        """Negative reserves are rejected by the > 0 guards."""
        pool = {"mintAmountA": -100, "mintAmountB": -200}
        assert tracker._price_from_pool(pool) == 0


class TestGetCurrentPrice:

    def test_from_pool_data(self, tracker):
        pool = {"mintAmountA": 500, "mintAmountB": 10_000}
        price = tracker.get_current_price("pool1", pool_data=pool)
        assert price == pytest.approx(20.0)

    def test_fallback_to_api(self, tracker, mock_api_client):
        mock_api_client.get_pool_by_id.return_value = {
            "mintAmountA": 100,
            "mintAmountB": 4_000,
        }
        price = tracker.get_current_price("pool1")
        assert price == pytest.approx(40.0)
        mock_api_client.get_pool_by_id.assert_called_with("pool1")

    def test_api_returns_none(self, tracker, mock_api_client):
        mock_api_client.get_pool_by_id.return_value = None
        price = tracker.get_current_price("missing")
        assert price == 0


class TestGetCurrentPricesBatch:

    def test_batch_prices(self, tracker):
        from bot.trading.position_manager import Position
        from datetime import datetime

        pos = Position(
            amm_id="p1",
            pool_name="A/B",
            entry_time=datetime.now(),
            entry_price_ratio=1.0,
            position_size_sol=1.0,
            token_a_amount=100,
            token_b_amount=100,
            pool_data={"mintAmountA": 200, "mintAmountB": 600},
        )
        prices = tracker.get_current_prices_batch({"p1": pos})
        assert "p1" in prices
        assert prices["p1"] == pytest.approx(3.0)

    def test_batch_skips_zero_price(self, tracker):
        from bot.trading.position_manager import Position
        from datetime import datetime

        pos = Position(
            amm_id="p2",
            pool_name="X/Y",
            entry_time=datetime.now(),
            entry_price_ratio=1.0,
            position_size_sol=1.0,
            token_a_amount=100,
            token_b_amount=100,
            pool_data={},  # no price data
        )
        tracker.api_client.get_pool_by_id.return_value = None
        prices = tracker.get_current_prices_batch({"p2": pos})
        assert "p2" not in prices
