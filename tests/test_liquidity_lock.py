"""Tests for bot/safety/liquidity_lock.py — on-chain LP lock analysis."""
import time
from unittest.mock import patch, MagicMock
import pytest

from bot.safety.liquidity_lock import (
    LiquidityLockAnalyzer,
    BURN_ADDRESSES,
    RAYDIUM_LP_AUTHORITY,
    KNOWN_LOCKER_PROGRAMS,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
)


@pytest.fixture
def analyzer():
    a = LiquidityLockAnalyzer(rpc_url="https://test.rpc")
    a._rpc_min_interval = 0  # no throttle in tests
    return a


class TestConstants:

    def test_burn_addresses_are_strings(self):
        for addr in BURN_ADDRESSES:
            assert isinstance(addr, str)
            assert len(addr) > 30

    def test_known_lockers_not_empty(self):
        assert len(KNOWN_LOCKER_PROGRAMS) >= 4


class TestRpcCall:

    @patch("bot.safety.liquidity_lock.requests.post")
    def test_success(self, mock_post, analyzer):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"jsonrpc": "2.0", "result": {"value": 42}},
        )
        mock_post.return_value.raise_for_status = MagicMock()
        result = analyzer._rpc_call("getTokenSupply", ["mint123"])
        assert result == {"value": 42}

    @patch("bot.safety.liquidity_lock.requests.post")
    def test_rpc_error(self, mock_post, analyzer):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"jsonrpc": "2.0", "error": {"message": "bad", "code": -32600}},
        )
        mock_post.return_value.raise_for_status = MagicMock()
        assert analyzer._rpc_call("getBalance", []) is None

    @patch("bot.safety.liquidity_lock.requests.post", side_effect=Exception("net"))
    def test_exception_retries(self, mock_post, analyzer):
        assert analyzer._rpc_call("test", []) is None
        assert mock_post.call_count == 3  # 1 original + 2 retries


class TestAnalyzeLpLock:

    def test_caching(self, analyzer):
        fake_result = {"available": True, "safe_pct": 99}
        analyzer._cache["lp_mint_cached"] = (fake_result, time.time())
        result = analyzer.analyze_lp_lock("lp_mint_cached")
        assert result["safe_pct"] == 99

    def test_expired_cache_refetches(self, analyzer):
        fake_result = {"available": True, "safe_pct": 99}
        analyzer._cache["lp_old"] = (fake_result, time.time() - 600)
        with patch.object(analyzer, '_do_analyze', return_value={"available": True, "safe_pct": 50}):
            result = analyzer.analyze_lp_lock("lp_old")
            assert result["safe_pct"] == 50


class TestDoAnalyze:

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_unavailable_on_supply_failure(self, mock_rpc, analyzer):
        mock_rpc.return_value = None
        result = analyzer._do_analyze("lp_mint")
        assert result["available"] is False

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_unavailable_on_zero_supply(self, mock_rpc, analyzer):
        mock_rpc.side_effect = [
            {"value": {"amount": "0"}},  # supply = 0
        ]
        result = analyzer._do_analyze("lp_mint")
        assert result["available"] is False

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners', return_value={})
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_all_burned(self, mock_rpc, mock_owners, mock_auth, analyzer):
        total_supply = 1_000_000
        burn_addr = list(BURN_ADDRESSES)[0]
        mock_rpc.side_effect = [
            # getTokenSupply
            {"value": {"amount": str(total_supply)}},
            # getTokenLargestAccounts
            {"value": [{"address": "holder1", "amount": str(total_supply)}]},
        ]
        mock_owners.return_value = {"holder1": burn_addr}

        result = analyzer._do_analyze("lp_mint")
        assert result["available"] is True
        assert result["burned_pct"] == pytest.approx(100.0)
        assert result["safe_pct"] == pytest.approx(100.0)
        assert result["is_safe"] is True

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners', return_value={})
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_protocol_locked(self, mock_rpc, mock_owners, mock_auth, analyzer):
        total = 1_000_000
        mock_rpc.side_effect = [
            {"value": {"amount": str(total)}},
            {"value": [{"address": "h1", "amount": str(total)}]},
        ]
        mock_owners.return_value = {"h1": RAYDIUM_LP_AUTHORITY}

        result = analyzer._do_analyze("lp_mint")
        assert result["protocol_locked_pct"] == pytest.approx(100.0)
        assert result["is_safe"] is True

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners')
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_contract_locked_via_pda(self, mock_rpc, mock_owners, mock_auth, analyzer):
        """Authority is a PDA whose owner is a known locker program."""
        total = 1_000_000
        locker = list(KNOWN_LOCKER_PROGRAMS)[0]
        mock_rpc.side_effect = [
            {"value": {"amount": str(total)}},
            {"value": [{"address": "h1", "amount": str(total)}]},
        ]
        mock_owners.return_value = {"h1": "somePDA"}
        mock_auth.return_value = {"somePDA": locker}

        result = analyzer._do_analyze("lp_mint")
        assert result["contract_locked_pct"] == pytest.approx(100.0)

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners', return_value={})
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_unlocked_whale(self, mock_rpc, mock_owners, mock_auth, analyzer):
        total = 1_000_000
        mock_rpc.side_effect = [
            {"value": {"amount": str(total)}},
            {"value": [{"address": "h1", "amount": str(total)}]},
        ]
        mock_owners.return_value = {"h1": "someRandomWallet"}

        result = analyzer._do_analyze("lp_mint")
        assert result["unlocked_pct"] == pytest.approx(100.0)
        assert result["max_single_unlocked_pct"] == pytest.approx(100.0)
        assert result["is_safe"] is False

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners', return_value={})
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_mixed_holders(self, mock_rpc, mock_owners, mock_auth, analyzer):
        total = 1_000_000
        burn_addr = list(BURN_ADDRESSES)[0]
        mock_rpc.side_effect = [
            {"value": {"amount": str(total)}},
            {"value": [
                {"address": "burned", "amount": "600000"},    # 60%
                {"address": "wallet1", "amount": "300000"},   # 30%
                {"address": "wallet2", "amount": "100000"},   # 10%
            ]},
        ]
        mock_owners.return_value = {
            "burned": burn_addr,
            "wallet1": "randomUser",
            "wallet2": "randomUser2",
        }

        result = analyzer._do_analyze("lp_mint")
        assert result["burned_pct"] == pytest.approx(60.0)
        assert result["unlocked_pct"] == pytest.approx(40.0)
        assert result["max_single_unlocked_pct"] == pytest.approx(30.0)

    @patch.object(LiquidityLockAnalyzer, '_batch_get_authority_owners', return_value={})
    @patch.object(LiquidityLockAnalyzer, '_batch_get_account_owners')
    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_system_program_owner_classified_as_burned(self, mock_rpc, mock_owners, mock_auth, analyzer):
        """Tokens where authority = System Program are treated as burned."""
        total = 1_000_000
        mock_rpc.side_effect = [
            {"value": {"amount": str(total)}},
            {"value": [{"address": "h1", "amount": str(total)}]},
        ]
        mock_owners.return_value = {"h1": SYSTEM_PROGRAM}

        result = analyzer._do_analyze("lp_mint")
        assert result["burned_pct"] == pytest.approx(100.0)


class TestBatchGetAccountOwners:

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_empty_input(self, mock_rpc, analyzer):
        assert analyzer._batch_get_account_owners([]) == {}
        mock_rpc.assert_not_called()

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_parses_token_authority(self, mock_rpc, analyzer):
        mock_rpc.return_value = {
            "value": [{
                "data": {"parsed": {"info": {"owner": "wallet_pubkey"}}},
                "owner": TOKEN_PROGRAM,
            }]
        }
        result = analyzer._batch_get_account_owners(["tokenAcct1"])
        assert result["tokenAcct1"] == "wallet_pubkey"

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_null_account_returns_system(self, mock_rpc, analyzer):
        mock_rpc.return_value = {"value": [None]}
        result = analyzer._batch_get_account_owners(["closed_acct"])
        assert result["closed_acct"] == SYSTEM_PROGRAM


class TestBatchGetAuthorityOwners:

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_empty_input(self, mock_rpc, analyzer):
        assert analyzer._batch_get_authority_owners([]) == {}

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_returns_owner_program(self, mock_rpc, analyzer):
        locker = list(KNOWN_LOCKER_PROGRAMS)[0]
        mock_rpc.return_value = {
            "value": [{"owner": locker}]
        }
        result = analyzer._batch_get_authority_owners(["pda1"])
        assert result["pda1"] == locker

    @patch.object(LiquidityLockAnalyzer, '_rpc_call')
    def test_null_account_returns_system(self, mock_rpc, analyzer):
        mock_rpc.return_value = {"value": [None]}
        result = analyzer._batch_get_authority_owners(["missing"])
        assert result["missing"] == SYSTEM_PROGRAM
