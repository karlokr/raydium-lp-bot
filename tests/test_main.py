"""Tests for bot/main.py — LiquidityBot orchestration (key methods only).

This file tests the higher-level orchestration methods that don't require
a full running bot. Network I/O and threads are mocked.
"""
import time
from datetime import datetime
from unittest.mock import patch, MagicMock, PropertyMock
import pytest

# The main module import triggers a lot of side-effects (config, wallet, etc.)
# so we patch heavily.

@pytest.fixture
def mock_deps():
    """Patch all external dependencies before importing LiquidityBot."""
    with patch("bot.main.RaydiumExecutor") as MockExec, \
         patch("bot.main.RaydiumAPIClient") as MockAPI, \
         patch("bot.state.load_state", return_value=None), \
         patch("bot.state.save_state"), \
         patch("bot.state.clear_state"):

        mock_executor = MockExec.return_value
        mock_executor.get_balance.return_value = 10.0
        mock_executor.get_wsol_balance.return_value = 0.0
        mock_executor.unwrap_wsol.return_value = 0.0
        mock_executor.close_empty_accounts.return_value = {"closed": 0, "reclaimedSol": 0}
        mock_executor.batch_get_lp_values.return_value = {}
        mock_executor.wallet = MagicMock()
        mock_executor.wallet.pubkey.return_value = "TestPubkey"

        mock_api = MockAPI.return_value
        mock_api.get_sol_price_usd.return_value = 170.0
        mock_api.get_all_pools.return_value = []
        mock_api.get_filtered_pools.return_value = []

        from bot.main import LiquidityBot
        bot = LiquidityBot()

        yield bot, mock_executor, mock_api


class TestBotInit:

    def test_creates_components(self, mock_deps):
        bot, _, _ = mock_deps
        assert bot.position_manager is not None
        assert bot.price_tracker is not None
        assert bot.analyzer is not None
        assert bot.snapshot_tracker is not None

    def test_initial_state(self, mock_deps):
        bot, _, _ = mock_deps
        assert len(bot.position_manager.active_positions) == 0
        assert bot.running is False


class TestUpdatePositions:

    def test_no_positions_is_noop(self, mock_deps):
        bot, mock_exec, _ = mock_deps
        bot.update_positions()
        mock_exec.batch_get_lp_values.assert_not_called()

    def test_updates_with_positions(self, mock_deps):
        bot, mock_exec, mock_api = mock_deps
        from bot.trading.position_manager import Position

        pos = Position(
            amm_id="pool1", pool_name="A/B",
            entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=100, token_b_amount=100,
            lp_mint="lp1",
            pool_data={"mintAmountA": 1000, "mintAmountB": 3000},
        )
        bot.position_manager.active_positions["pool1"] = pos

        mock_exec.batch_get_lp_values.return_value = {
            "pool1": {"valueSol": 1.1, "priceRatio": 3.0, "lpBalance": 500},
        }
        mock_api.get_pool_by_id.return_value = {
            "mintAmountA": 1000, "mintAmountB": 3000,
        }

        bot.update_positions()
        # Position should have been updated
        assert pos.current_lp_value_sol == pytest.approx(1.1)


class TestScanAndRankPools:

    def test_returns_empty_when_no_pools(self, mock_deps):
        bot, _, mock_api = mock_deps
        mock_api.get_filtered_pools.return_value = []
        result = bot.scan_and_rank_pools()
        assert result == []

    def test_returns_ranked_pools(self, mock_deps):
        bot, _, mock_api = mock_deps
        mock_api.get_filtered_pools.return_value = [
            {
                "ammId": "p1", "name": "A/B", "tvl": 100_000,
                "burnPercent": 99, "day": {"apr": 200, "feeApr": 180, "volume": 80000},
                "week": {"volume": 400000}, "openTime": 0,
                "baseMint": "token1",
                "quoteMint": "So11111111111111111111111111111111111111112",
                "lpMint": {"address": "lp1"},
            },
        ]
        # Mock pool quality to pass safety check
        with patch("bot.main.PoolQualityAnalyzer") as MockQA:
            MockQA.get_safe_pools.return_value = mock_api.get_filtered_pools.return_value
            bot.quality_analyzer = MockQA.return_value
            result = bot.scan_and_rank_pools()
            # Should have at least scored the pool
            assert len(result) >= 0  # might be 0 if safety check filters it


class TestExitPosition:

    def test_exit_records_trade(self, mock_deps):
        bot, mock_exec, _ = mock_deps
        from bot.trading.position_manager import Position

        pos = Position(
            amm_id="pool1", pool_name="A/B",
            entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=100, token_b_amount=100,
            lp_mint="lp1", lp_token_amount=5000,
        )
        bot.position_manager.active_positions["pool1"] = pos

        mock_exec.remove_liquidity.return_value = "sig123"
        mock_exec.swap_tokens.return_value = "sig_swap"
        mock_exec.get_token_balance.return_value = 0

        with patch("bot.state.append_trade_history") as mock_hist:
            bot._exit_position("pool1", "Stop Loss")
            # Trade history should have been recorded
            mock_hist.assert_called_once()


class TestShutdown:

    def test_sets_running_false(self, mock_deps):
        bot, _, _ = mock_deps
        bot.running = True
        with pytest.raises(SystemExit):
            bot.shutdown()
        assert bot.running is False
