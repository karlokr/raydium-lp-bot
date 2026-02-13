"""
Configuration for Raydium Liquidity Bot
"""
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv


load_dotenv()


# Project root directory (for resolving paths to bridge/, etc.)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class BotConfig:
    # API Settings
    RAYDIUM_API_URL: str = "https://api.raydium.io/v2/main/pairs"
    API_CACHE_TTL: int = 300  # 5 minutes (API updates ~5-15min, so cache makes sense)

    # Solana RPC (for real-time price tracking)
    RPC_ENDPOINT: str = os.getenv('SOLANA_RPC_URL', "https://api.mainnet-beta.solana.com")

    # Pool Filtering
    MIN_LIQUIDITY_USD: float = 10_000  # Minimum $10k TVL
    MIN_VOLUME_TVL_RATIO: float = 0.5  # 24h volume should be >50% of TVL
    MIN_APR_24H: float = 5.0  # Minimum 5% APR (annualized from 24h)
    MAX_POOL_AGE_DAYS: int = 365  # Avoid brand new pools if < X days old (optional)

    # Token Safety
    ALLOWED_QUOTE_TOKENS: List[str] = field(default_factory=lambda: [
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        "So11111111111111111111111111111111111111112",  # SOL (wrapped)
    ])
    VERIFIED_TOKENS_ONLY: bool = True  # Use Jupiter strict list

    # Position Sizing
    MAX_POSITION_PERCENT: float = 0.20  # Max 20% of capital per position
    MAX_TVL_PERCENT: float = 0.02  # Don't be >2% of pool TVL
    MAX_ABSOLUTE_POSITION_USD: float = 5_000  # Hard cap per position (used for TVL % check)
    MAX_ABSOLUTE_POSITION_SOL: float = 5.0  # Hard cap per position in SOL
    MAX_CONCURRENT_POSITIONS: int = 5  # Max active positions
    RESERVE_PERCENT: float = 0.20  # Always keep 20% of capital in reserve (never deploy more than 80%)

    # Risk Management
    STOP_LOSS_PERCENT: float = -2.0  # Exit if down 2% (after fees/IL)
    TAKE_PROFIT_PERCENT: float = 5.0  # Exit if up 5% (after fees/IL)
    MAX_HOLD_TIME_HOURS: int = 24  # Force exit after 24 hours

    # IL Risk
    MAX_IMPERMANENT_LOSS: float = -3.0  # Exit if IL exceeds 3%
    IL_CHECK_INTERVAL_SEC: int = 10  # Check IL every 10 seconds

    # Token Safety (via RugCheck)
    CHECK_TOKEN_SAFETY: bool = False  # Check token safety via RugCheck (NOT LP locks!)
    # Note: LP lock verification is NOT implemented - always verify manually

    # Trading Settings
    TRADING_ENABLED: bool = True  # CRITICAL: Set to True to enable real transactions
    TRADING_TOKEN: str = "So11111111111111111111111111111111111111112"  # WSOL only
    REQUIRE_WSOL_PAIRS: bool = True  # Only trade pairs with WSOL

    # Monitoring
    POOL_SCAN_INTERVAL_SEC: int = 300  # Scan for new pools every 5 minutes (matches API cache)
    POSITION_CHECK_INTERVAL_SEC: int = 10  # Check positions every 10 seconds

    # Safety
    DRY_RUN: bool = False  # Paper trading mode - SET TO FALSE FOR REAL TRADING
    ENABLE_EMERGENCY_EXIT: bool = True  # Allow manual override

    # Paths
    BRIDGE_DIR: str = os.path.join(PROJECT_ROOT, 'bridge')
    BRIDGE_SCRIPT: str = os.path.join(PROJECT_ROOT, 'bridge', 'raydium_sdk_bridge.js')


# Global config instance
config = BotConfig()
