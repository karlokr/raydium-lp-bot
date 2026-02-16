"""
Shared fixtures for the Raydium LP Bot test suite.
"""
import os
import sys
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Environment stubs ────────────────────────────────────────────────
# Patch env vars BEFORE importing any bot modules so config.py picks them up.

# Fresh throwaway wallet generated per test session (no funds, never used on-chain).
import base58 as _b58
from solders.keypair import Keypair as _Keypair

_test_keypair = _Keypair()  # new random keypair every pytest invocation
_TEST_WALLET_KEY = _b58.b58encode(bytes(_test_keypair)).decode()


@pytest.fixture(autouse=True)
def _patch_env(request, monkeypatch):
    """Set minimal env vars so config / executor don't blow up on import.

    Unit tests:        dummy RPC URL + dummy wallet.
    Integration tests: real RPC URL from .env + dummy wallet (never risk
                       the configured wallet in automated tests).
    """
    if request.node.get_closest_marker("integration"):
        # Keep real RPC, but ALWAYS use the throwaway wallet
        monkeypatch.setenv("WALLET_PRIVATE_KEY", _TEST_WALLET_KEY)
        return
    monkeypatch.setenv("SOLANA_RPC_URL", "https://test-rpc.example.com")
    monkeypatch.setenv("WALLET_PRIVATE_KEY",
                       "5" * 87 + "A")  # 88-char base58 dummy


# ── Sample pool data ─────────────────────────────────────────────────

@pytest.fixture
def sample_pool():
    """A realistic V3-normalised pool dict."""
    return {
        "id": "pool123",
        "ammId": "pool123",
        "name": "BONK/WSOL",
        "tvl": 75_000,
        "liquidity": 75_000,
        "burnPercent": 95,
        "feeRate": 0.003,
        "openTime": int(datetime.now().timestamp()) - 7200,  # 2 hours ago
        "price": 0.00001234,
        "mintAmountA": 1_000_000_000,
        "mintAmountB": 12_340,
        "baseMint": "BonkMint111111111111111111111111111111111111",
        "quoteMint": "So11111111111111111111111111111111111111112",
        "mintA": {"address": "BonkMint111111111111111111111111111111111111", "symbol": "BONK", "decimals": 5},
        "mintB": {"address": "So11111111111111111111111111111111111111112", "symbol": "WSOL", "decimals": 9},
        "lpMint": {"address": "LPMint1111111111111111111111111111111111111"},
        "day": {
            "apr": 180,
            "feeApr": 160,
            "volume": 120_000,
            "volumeFee": 360,
            "priceMin": 0.0000118,
            "priceMax": 0.0000129,
        },
        "week": {
            "apr": 150,
            "volume": 700_000,
        },
        "month": {"apr": 130, "volume": 2_800_000},
        "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "score": 0,
    }


@pytest.fixture
def sample_pool_sol_base():
    """Pool where SOL is the base (mintA)."""
    return {
        "id": "poolSOLbase",
        "ammId": "poolSOLbase",
        "name": "WSOL/MEME",
        "tvl": 50_000,
        "liquidity": 50_000,
        "burnPercent": 80,
        "price": 50000,
        "baseMint": "So11111111111111111111111111111111111111112",
        "quoteMint": "MEMEMint11111111111111111111111111111111111",
        "mintA": {"address": "So11111111111111111111111111111111111111112", "symbol": "WSOL", "decimals": 9},
        "mintB": {"address": "MEMEMint11111111111111111111111111111111111", "symbol": "MEME", "decimals": 6},
        "day": {"apr": 200, "feeApr": 180, "volume": 80_000, "priceMin": 48000, "priceMax": 52000},
        "week": {"apr": 170, "volume": 500_000},
        "openTime": 0,
    }


@pytest.fixture
def sample_position():
    """A Position dataclass instance for testing."""
    from bot.trading.position_manager import Position
    return Position(
        amm_id="pool123",
        pool_name="BONK/WSOL",
        entry_time=datetime(2025, 1, 15, 12, 0, 0),
        entry_price_ratio=0.00001234,
        position_size_sol=1.0,
        token_a_amount=500_000,
        token_b_amount=0.5,
        sol_is_base=False,
        lp_mint="LPMint1111111111111111111111111111111111111",
        lp_token_amount=1_000_000,
        lp_decimals=9,
        current_price_ratio=0.00001234,
        current_lp_value_sol=1.05,
        pool_data={"name": "BONK/WSOL"},
    )


@pytest.fixture
def mock_api_client():
    """A mock RaydiumAPIClient."""
    client = MagicMock()
    client.get_sol_price_usd.return_value = 170.0
    client.get_all_pools.return_value = []
    client.get_pool_by_id.return_value = None
    return client
