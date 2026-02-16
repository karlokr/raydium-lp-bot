"""Tests for bot/raydium_client.py — RaydiumAPIClient."""
import time
import requests
from unittest.mock import patch, MagicMock
import pytest

from bot.raydium_client import RaydiumAPIClient, WSOL_MINT


class TestGetSolPriceUsd:

    def test_returns_cached_price(self):
        client = RaydiumAPIClient()
        client._sol_price_usd = 170.0
        client._sol_price_timestamp = time.time()
        assert client.get_sol_price_usd() == 170.0

    @patch.object(RaydiumAPIClient, '_fetch_price_jupiter', return_value=175.5)
    def test_jupiter_with_api_key(self, mock_jup):
        client = RaydiumAPIClient()
        client._jupiter_api_key = "test-key"
        client._sol_price_usd = 0  # force refresh
        price = client.get_sol_price_usd()
        assert price == 175.5
        mock_jup.assert_called_once()

    @patch.object(RaydiumAPIClient, '_fetch_price_coingecko', return_value=168.0)
    @patch.object(RaydiumAPIClient, '_fetch_price_jupiter', return_value=0.0)
    def test_fallback_to_coingecko(self, mock_jup, mock_cg):
        client = RaydiumAPIClient()
        client._jupiter_api_key = "key"
        client._sol_price_usd = 0
        price = client.get_sol_price_usd()
        assert price == 168.0

    @patch.object(RaydiumAPIClient, '_fetch_price_jupiter', return_value=0.0)
    @patch.object(RaydiumAPIClient, '_fetch_price_coingecko', return_value=0.0)
    def test_both_fail_returns_last_known(self, mock_cg, mock_jup):
        client = RaydiumAPIClient()
        client._sol_price_usd = 150.0
        client._sol_price_timestamp = 0  # force refresh
        price = client.get_sol_price_usd()
        assert price == 150.0


class TestFetchPriceJupiter:

    @patch('bot.raydium_client.requests.get')
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {WSOL_MINT: {"usdPrice": 172.5}},
        )
        mock_get.return_value.raise_for_status = MagicMock()
        client = RaydiumAPIClient()
        assert client._fetch_price_jupiter() == 172.5

    @patch('bot.raydium_client.requests.get', side_effect=Exception("timeout"))
    def test_exception_returns_zero(self, _):
        client = RaydiumAPIClient()
        assert client._fetch_price_jupiter() == 0.0


class TestFetchPriceCoingecko:

    @patch('bot.raydium_client.requests.get')
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"solana": {"usd": 165.0}},
        )
        mock_get.return_value.raise_for_status = MagicMock()
        client = RaydiumAPIClient()
        assert client._fetch_price_coingecko() == 165.0

    @patch('bot.raydium_client.requests.get', side_effect=Exception("fail"))
    def test_exception_returns_zero(self, _):
        client = RaydiumAPIClient()
        assert client._fetch_price_coingecko() == 0.0


class TestGetAllPools:

    @patch.object(RaydiumAPIClient, '_fetch_wsol_pools', return_value=[{"ammId": "a"}])
    def test_fresh_fetch(self, mock_fetch):
        client = RaydiumAPIClient()
        client._cache = None
        pools = client.get_all_pools()
        assert len(pools) == 1
        mock_fetch.assert_called_once()

    @patch.object(RaydiumAPIClient, '_fetch_wsol_pools')
    def test_uses_cache(self, mock_fetch):
        client = RaydiumAPIClient()
        client._cache = [{"ammId": "cached"}]
        client._cache_timestamp = time.time()
        pools = client.get_all_pools()
        assert pools[0]["ammId"] == "cached"
        mock_fetch.assert_not_called()

    @patch.object(RaydiumAPIClient, '_fetch_wsol_pools')
    def test_force_refresh_bypasses_cache(self, mock_fetch):
        mock_fetch.return_value = [{"ammId": "new"}]
        client = RaydiumAPIClient()
        client._cache = [{"ammId": "old"}]
        client._cache_timestamp = time.time()
        pools = client.get_all_pools(force_refresh=True)
        assert pools[0]["ammId"] == "new"

    @patch.object(RaydiumAPIClient, '_fetch_wsol_pools', side_effect=requests.RequestException("net"))
    def test_returns_stale_cache_on_error(self, _):
        client = RaydiumAPIClient()
        client._cache = [{"ammId": "stale"}]
        client._cache_timestamp = 0
        pools = client.get_all_pools()
        assert pools[0]["ammId"] == "stale"


class TestNormalizePool:

    def test_adds_backward_compatible_fields(self):
        client = RaydiumAPIClient()
        raw = {
            "id": "abc123",
            "tvl": 50_000,
            "burnPercent": 90,
            "mintA": {"address": "mintA_addr", "symbol": "BONK", "decimals": 5},
            "mintB": {"address": "mintB_addr", "symbol": "WSOL", "decimals": 9},
            "mintAmountA": 1_000_000,
            "mintAmountB": 500,
            "day": {"apr": 120, "volume": 60000, "volumeFee": 180},
        }
        norm = client._normalize_pool(raw)
        assert norm["ammId"] == "abc123"
        assert norm["name"] == "BONK/WSOL"
        assert norm["liquidity"] == 50_000
        assert norm["apr24h"] == 120
        assert norm["volume24h"] == 60000
        assert norm["fee24h"] == 180
        assert norm["baseMint"] == "mintA_addr"
        assert norm["quoteMint"] == "mintB_addr"

    def test_price_from_reserves(self):
        client = RaydiumAPIClient()
        raw = {
            "mintA": {"address": "a", "symbol": "A", "decimals": 6},
            "mintB": {"address": "b", "symbol": "B", "decimals": 6},
            "mintAmountA": 100,
            "mintAmountB": 200,
            "day": {},
        }
        norm = client._normalize_pool(raw)
        assert norm["price"] == pytest.approx(2.0)

    def test_price_fallback_when_no_reserves(self):
        client = RaydiumAPIClient()
        raw = {
            "mintA": {"address": "a", "symbol": "X", "decimals": 6},
            "mintB": {"address": "b", "symbol": "Y", "decimals": 6},
            "mintAmountA": 0,
            "mintAmountB": 0,
            "price": 42.5,
            "day": {},
        }
        norm = client._normalize_pool(raw)
        assert norm["price"] == 42.5


class TestGetPoolById:

    @patch.object(RaydiumAPIClient, 'get_all_pools')
    def test_found_in_cache(self, mock_all):
        mock_all.return_value = [{"ammId": "p1", "name": "A/B"}]
        client = RaydiumAPIClient()
        result = client.get_pool_by_id("p1")
        assert result["name"] == "A/B"

    @patch('bot.raydium_client.requests.get')
    @patch.object(RaydiumAPIClient, 'get_all_pools', return_value=[])
    def test_direct_api_lookup(self, _, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"id": "xyz", "mintA": {"address": "", "symbol": "?", "decimals": 0},
                                     "mintB": {"address": "", "symbol": "?", "decimals": 0}, "day": {}}]},
        )
        client = RaydiumAPIClient()
        result = client.get_pool_by_id("xyz")
        assert result is not None
        assert result["ammId"] == "xyz"

    @patch('bot.raydium_client.requests.get', side_effect=requests.RequestException("fail"))
    @patch.object(RaydiumAPIClient, 'get_all_pools', return_value=[])
    def test_returns_none_on_failure(self, _, __):
        client = RaydiumAPIClient()
        assert client.get_pool_by_id("missing") is None


class TestGetFilteredPools:

    @patch.object(RaydiumAPIClient, 'get_all_pools')
    def test_min_liquidity(self, mock_all):
        mock_all.return_value = [
            {"tvl": 10_000, "day": {"apr": 50, "volume": 5000}},
            {"tvl": 1_000, "day": {"apr": 50, "volume": 500}},
        ]
        client = RaydiumAPIClient()
        filtered = client.get_filtered_pools(min_liquidity=5_000)
        assert len(filtered) == 1

    @patch.object(RaydiumAPIClient, 'get_all_pools')
    def test_min_apr(self, mock_all):
        mock_all.return_value = [
            {"tvl": 10_000, "day": {"apr": 200, "volume": 10000}},
            {"tvl": 10_000, "day": {"apr": 50, "volume": 10000}},
        ]
        client = RaydiumAPIClient()
        filtered = client.get_filtered_pools(min_apr=100)
        assert len(filtered) == 1

    @patch.object(RaydiumAPIClient, 'get_all_pools')
    def test_min_volume_tvl_ratio(self, mock_all):
        mock_all.return_value = [
            {"tvl": 10_000, "day": {"apr": 50, "volume": 20_000}},  # 2.0x
            {"tvl": 10_000, "day": {"apr": 50, "volume": 1_000}},   # 0.1x
        ]
        client = RaydiumAPIClient()
        filtered = client.get_filtered_pools(min_volume_tvl_ratio=1.0)
        assert len(filtered) == 1

    @patch.object(RaydiumAPIClient, 'get_all_pools')
    def test_zero_tvl_excluded(self, mock_all):
        mock_all.return_value = [
            {"tvl": 0, "day": {"apr": 500, "volume": 10_000}},
        ]
        client = RaydiumAPIClient()
        filtered = client.get_filtered_pools()
        assert len(filtered) == 0
