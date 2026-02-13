"""
Configuration for Raydium LP Bot
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
    # Solana RPC
    RPC_ENDPOINT: str = os.getenv('SOLANA_RPC_URL', "https://api.mainnet-beta.solana.com")

    # API Caching
    API_CACHE_TTL: int = 120  # 2 minutes (meme pools change fast)

    # Pool Filtering
    MIN_LIQUIDITY_USD: float = 10_000  # Minimum $10k TVL
    MIN_VOLUME_TVL_RATIO: float = 0.5  # 24h volume should be >50% of TVL
    MIN_APR_24H: float = 50.0  # Minimum 50% APR (meme pools typically show 100%+)
    MIN_BURN_PERCENT: float = 50.0  # Minimum 50% LP tokens burned
    REQUIRE_WSOL_PAIRS: bool = True  # Only trade pairs with WSOL

    # Token Safety (via RugCheck)
    CHECK_TOKEN_SAFETY: bool = True  # Check token safety via RugCheck
    MAX_RUGCHECK_SCORE: int = 60  # Max acceptable RugCheck risk score (0-100, lower=safer)
    MAX_TOP10_HOLDER_PERCENT: float = 50.0  # Reject if top 10 holders own more than this %
    MAX_SINGLE_HOLDER_PERCENT: float = 20.0  # Reject if any single holder owns more than this %

    # Position Sizing
    MAX_ABSOLUTE_POSITION_SOL: float = 5.0  # Hard cap per position in SOL
    MAX_CONCURRENT_POSITIONS: int = 3  # Max active positions
    RESERVE_SOL: float = 0.05  # Fixed SOL reserve for tx fees + ATA rent

    # Risk Management
    STOP_LOSS_PERCENT: float = -15.0  # Exit if down 15% (wide enough for meme volatility)
    TAKE_PROFIT_PERCENT: float = 10.0  # Exit if up 10% (covers ~2-3% slippage drag on exit)
    MAX_HOLD_TIME_HOURS: int = 24  # Force exit after 24 hours
    MAX_IMPERMANENT_LOSS: float = -3.0  # Exit if IL exceeds 3%

    # Trading Settings
    TRADING_ENABLED: bool = True  # Set to True to enable real transactions
    DRY_RUN: bool = False  # Paper trading mode
    SLIPPAGE_PERCENT: float = 5.0  # Slippage tolerance (5% for volatile meme pools)

    # Monitoring
    POOL_SCAN_INTERVAL_SEC: int = 180  # Scan for new pools every 3 minutes
    POSITION_CHECK_INTERVAL_SEC: int = 10  # Check positions every 10 seconds

    # Safety
    ENABLE_EMERGENCY_EXIT: bool = True  # Allow manual override

    # Paths
    BRIDGE_SCRIPT: str = os.path.join(PROJECT_ROOT, 'bridge', 'raydium_sdk_bridge.js')


# Global config instance
config = BotConfig()
