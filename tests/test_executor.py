"""Tests for bot/trading/executor.py — mocked subprocess calls to Node.js bridge."""
import json
import os
from unittest.mock import patch, MagicMock
import pytest

from bot.config import config


# Stub the wallet loading so RaydiumExecutor.__init__ doesn't fail.
# We patch the env + solana imports at module level.
@pytest.fixture
def executor():
    with patch.dict(os.environ, {
        "WALLET_PRIVATE_KEY": "5" * 87 + "A",
        "SOLANA_RPC_URL": "https://test.rpc",
    }):
        with patch("bot.trading.executor.Client"):
            from bot.trading.executor import RaydiumExecutor
            ex = RaydiumExecutor.__new__(RaydiumExecutor)
            ex.rpc_url = "https://test.rpc"
            ex.client = MagicMock()
            ex.wallet = MagicMock()
            ex.wallet.pubkey.return_value = "TestPubkey"
            return ex


def _bridge_result(data: dict, returncode=0, stderr=""):
    """Create a mock subprocess.run result mimicking bridge JSON output."""
    return MagicMock(
        returncode=returncode,
        stdout=json.dumps(data),
        stderr=stderr,
    )


class TestGetBalance:

    def test_success(self, executor):
        executor.client.get_balance.return_value = MagicMock(value=5_000_000_000)
        assert executor.get_balance() == pytest.approx(5.0)

    def test_exception(self, executor):
        executor.client.get_balance.side_effect = Exception("RPC fail")
        assert executor.get_balance() == 0.0


class TestGetWsolBalance:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"balance": 2_000_000_000})
        assert executor.get_wsol_balance() == pytest.approx(2.0)

    @patch("bot.trading.executor.subprocess.run")
    def test_failure(self, mock_run, executor):
        mock_run.return_value = _bridge_result({}, returncode=1, stderr="err")
        assert executor.get_wsol_balance() == 0.0


class TestUnwrapWsol:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"success": True, "unwrapped": 1.5})
        assert executor.unwrap_wsol() == pytest.approx(1.5)

    @patch("bot.trading.executor.subprocess.run")
    def test_nothing_to_unwrap(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"success": False})
        assert executor.unwrap_wsol() == 0.0

    @patch("bot.trading.executor.subprocess.run", side_effect=Exception("boom"))
    def test_exception(self, mock_run, executor):
        assert executor.unwrap_wsol() == 0.0


class TestGetTokenBalance:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"balance": 999_999})
        assert executor.get_token_balance("mint123") == pytest.approx(999_999)

    @patch("bot.trading.executor.subprocess.run")
    def test_failure(self, mock_run, executor):
        mock_run.return_value = _bridge_result({}, returncode=1)
        assert executor.get_token_balance("bad") == 0.0


class TestCloseEmptyAccounts:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"closed": 3, "reclaimedSol": 0.006})
        result = executor.close_empty_accounts()
        assert result["closed"] == 3
        assert result["reclaimedSol"] == pytest.approx(0.006)

    @patch("bot.trading.executor.subprocess.run")
    def test_with_keep_mints(self, mock_run, executor):
        mock_run.return_value = _bridge_result({"closed": 1, "reclaimedSol": 0.002})
        executor.close_empty_accounts(keep_mints=["mintA", "mintB"])
        args_used = mock_run.call_args[0][0]
        assert "mintA,mintB" in args_used


class TestGetLpValueSol:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "valueSol": 1.234,
            "priceRatio": 0.0001,
            "lpBalance": 5000000,
        })
        result = executor.get_lp_value_sol("pool1", "lpmint1")
        assert result["valueSol"] == pytest.approx(1.234)
        assert result["lpBalance"] == 5000000

    @patch("bot.trading.executor.subprocess.run")
    def test_failure_returns_empty(self, mock_run, executor):
        mock_run.return_value = _bridge_result({}, returncode=1)
        assert executor.get_lp_value_sol("p", "lp") == {}


class TestBatchGetLpValues:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "results": {
                "poolA": {"valueSol": 1.0, "priceRatio": 0.001, "lpBalance": 100},
                "poolB": {"valueSol": 2.0, "priceRatio": 0.002, "lpBalance": 200},
            }
        })
        result = executor.batch_get_lp_values([
            {"pool_id": "poolA", "lp_mint": "lpA"},
            {"pool_id": "poolB", "lp_mint": "lpB"},
        ])
        assert result["poolA"]["valueSol"] == pytest.approx(1.0)
        assert result["poolB"]["lpBalance"] == 200

    def test_empty_input(self, executor):
        assert executor.batch_get_lp_values([]) == {}

    @patch("bot.trading.executor.subprocess.run", side_effect=Exception("fail"))
    def test_exception_returns_empty(self, mock_run, executor):
        assert executor.batch_get_lp_values([{"pool_id": "p", "lp_mint": "l"}]) == {}


class TestSwapTokens:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "success": True,
            "signatures": ["sig123"],
        })
        sig = executor.swap_tokens("pool1", 0.5, "buy")
        assert sig == "sig123"

    @patch("bot.trading.executor.subprocess.run")
    def test_failure(self, mock_run, executor):
        mock_run.return_value = _bridge_result(
            {"success": False, "error": "slippage"}, returncode=1,
        )
        assert executor.swap_tokens("pool1", 0.5) is None

    def test_trading_disabled(self, executor):
        with patch.object(config, 'TRADING_ENABLED', False):
            assert executor.swap_tokens("pool1", 0.5) is None

    def test_dry_run(self, executor):
        with patch.object(config, 'DRY_RUN', True):
            sig = executor.swap_tokens("pool1", 0.5, "buy")
            assert sig is not None
            assert "DRY_RUN" in sig


class TestAddLiquidity:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "success": True,
            "signatures": ["sig_add"],
            "lpMint": "lpmint123",
        })
        result = executor.add_liquidity("pool1", 100.0, 0.5)
        assert result["signature"] == "sig_add"
        assert result["lpMint"] == "lpmint123"

    @patch("bot.trading.executor.subprocess.run")
    def test_failure(self, mock_run, executor):
        mock_run.return_value = _bridge_result(
            {"success": False, "error": "insufficient"}, returncode=1,
        )
        assert executor.add_liquidity("p", 1, 1) is None

    def test_dry_run(self, executor):
        with patch.object(config, 'DRY_RUN', True):
            result = executor.add_liquidity("pool1", 100, 0.5)
            assert result is not None
            assert "DRY_RUN" in result["signature"]

    def test_trading_disabled(self, executor):
        with patch.object(config, 'TRADING_ENABLED', False):
            assert executor.add_liquidity("p", 1, 1) is None


class TestRemoveLiquidity:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "success": True,
            "signatures": ["sig_rm"],
        })
        sig = executor.remove_liquidity("pool1", 50000)
        assert sig == "sig_rm"

    @patch("bot.trading.executor.subprocess.run")
    def test_timeout(self, mock_run, executor):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("node", 60)
        assert executor.remove_liquidity("p", 1) is None

    def test_trading_disabled(self, executor):
        with patch.object(config, 'TRADING_ENABLED', False):
            assert executor.remove_liquidity("p", 1) is None


class TestListAllTokens:

    @patch("bot.trading.executor.subprocess.run")
    def test_success(self, mock_run, executor):
        mock_run.return_value = _bridge_result({
            "success": True,
            "tokens": [{"mint": "abc", "balance": "1000000"}],
        })
        tokens = executor.list_all_tokens()
        assert len(tokens) == 1
        assert tokens[0]["mint"] == "abc"

    @patch("bot.trading.executor.subprocess.run")
    def test_failure(self, mock_run, executor):
        mock_run.return_value = _bridge_result({}, returncode=1)
        assert executor.list_all_tokens() == []

    @patch("bot.trading.executor.subprocess.run", side_effect=Exception("err"))
    def test_exception(self, mock_run, executor):
        assert executor.list_all_tokens() == []
