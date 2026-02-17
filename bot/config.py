"""
Configuration for Raydium LP Bot
"""
import os
from dataclasses import dataclass
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
    MIN_LIQUIDITY_USD: float = 5_000  # Minimum $5k TVL (lower = riskier but juicier)
    MIN_VOLUME_TVL_RATIO: float = 0.5  # 24h volume should be >50% of TVL
    MIN_APR_24H: float = 100.0  # Minimum 100% APR (demand high yield for the risk)
    MIN_PREDICTED_NET_APR: float = 30.0  # Min predicted net APR (fees×sust − IL, annualised)
    MIN_FEE_APR: float = 50.0  # Minimum 50% pure fee APR (excludes rewards)
    MAX_PRICE_RANGE_RATIO: float = 2.0  # Max 24h price range (2.0 = 100% swing, controls IL)
    MIN_VOLUME_24H: float = 1_000  # Minimum $1k daily volume
    MIN_FEE_24H: float = 10.0  # Minimum $10 daily fees generated
    MIN_VOLUME_GROWTH: float = 0.8  # Min day/week volume ratio (0.8 = 80% of weekly avg)
    MIN_BURN_PERCENT: float = 50.0  # Minimum 50% LP tokens burned (lower = more rug risk)
    REQUIRE_WSOL_PAIRS: bool = True  # Only trade pairs with WSOL

    # Token Safety (via RugCheck)
    CHECK_TOKEN_SAFETY: bool = True  # Check token safety via RugCheck
    MAX_RUGCHECK_SCORE: int = 50  # Max acceptable RugCheck risk score (0-100, lower=safer; >40 = "high risk")
    MAX_TOP10_HOLDER_PERCENT: float = 40.0  # Reject if top 10 holders own more than this %
    MAX_SINGLE_HOLDER_PERCENT: float = 25.0  # Reject if any single holder owns more than this %
    MIN_TOKEN_HOLDERS: int = 100  # Reject tokens with fewer holders (thin markets = easy to manipulate)

    # LP Lock Safety (on-chain analysis of LP token distribution)
    # NOTE: On-chain analysis only sees CIRCULATING supply (SPL-burned tokens
    # vanish from supply).  pool_quality.py combines burn + lock for the
    # definitive safety verdict.
    #
    # Mathematical relationship (at worst-case MIN_BURN_PERCENT):
    #   MIN_BURN + MIN_LP_LOCK × (1 - MIN_BURN/100) = MIN_SAFE_LP
    #   50       + 80          × 0.5                 = 90
    CHECK_LP_LOCK: bool = True  # Check on-chain LP holder distribution
    MIN_LP_LOCK_PERCENT: float = 80.0  # Min % of CIRCULATING LP that must be locked (liquidity_lock module standalone)
    MIN_SAFE_LP_PERCENT: float = 90.0  # Min % of TOTAL initial LP that must be safe (burned + locked combined)
    MAX_SINGLE_LP_HOLDER_PERCENT: float = 25.0  # Max % of TOTAL initial LP any single wallet can pull

    # Position Sizing
    MAX_ABSOLUTE_POSITION_SOL: float = 5.0  # Hard cap per position in SOL
    MIN_POSITION_SOL: float = 0.05  # Minimum position size (below this, fees eat returns)
    MAX_CONCURRENT_POSITIONS: int = 3  # Max active positions
    RESERVE_SOL: float = 0.05  # Fixed SOL reserve for tx fees + ATA rent

    # Risk Management
    STOP_LOSS_PERCENT: float = -15.0  # Exit if down 15% (wide for high-risk meme pools)
    TAKE_PROFIT_PERCENT: float = 10.0  # Exit if up 10% (capture bigger swings)
    MAX_HOLD_TIME_HOURS: int = 168  # Force exit after 7 days
    POSITION_REEVAL_INTERVAL_HOURS: int = 24  # Re-evaluate safety every 24 hours
    MAX_IMPERMANENT_LOSS: float = -5.0  # Exit if IL exceeds 5%
    STOP_LOSS_COOLDOWNS: list = None  # Escalating cooldowns per consecutive stop loss [24h, 48h]
    PERMANENT_BLACKLIST_STRIKES: int = 3  # Permanently blacklist after this many consecutive stop losses

    def __post_init__(self):
        if self.STOP_LOSS_COOLDOWNS is None:
            self.STOP_LOSS_COOLDOWNS = [86400, 172800]  # [24h, 48h] in seconds

    # Trading Settings
    TRADING_ENABLED: bool = True  # Set to True to enable real transactions
    DRY_RUN: bool = False  # Paper trading mode
    SLIPPAGE_PERCENT: float = 5.0  # Slippage tolerance (5% for volatile meme pools)

    # Monitoring
    POOL_SCAN_INTERVAL_SEC: int = 180  # Scan for new pools every 3 minutes
    POSITION_CHECK_INTERVAL_SEC: int = 1  # Check positions every 1 second (threaded)
    DISPLAY_INTERVAL_SEC: int = 4  # Refresh status display every 4 seconds

    # Safety
    ENABLE_EMERGENCY_EXIT: bool = True  # Allow manual override

    # Paths
    BRIDGE_SCRIPT: str = os.path.join(PROJECT_ROOT, 'bridge', 'raydium_sdk_bridge.js')


# Global config instance
config = BotConfig()
