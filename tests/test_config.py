"""Tests for bot/config.py — BotConfig dataclass and defaults."""
from bot.config import BotConfig


class TestBotConfigDefaults:
    """Verify every default field value on a fresh BotConfig."""

    def test_rpc_endpoint_default(self):
        """RPC_ENDPOINT defaults to os.getenv at class-definition time.
        We can't monkeypatch that, so just check the field exists and is a URL."""
        cfg = BotConfig()
        assert cfg.RPC_ENDPOINT.startswith("https://")

    def test_rpc_endpoint_explicit_override(self):
        cfg = BotConfig(RPC_ENDPOINT="https://custom-rpc.io")
        assert cfg.RPC_ENDPOINT == "https://custom-rpc.io"

    def test_api_cache_ttl(self):
        cfg = BotConfig()
        assert cfg.API_CACHE_TTL == 120

    def test_pool_filtering_defaults(self):
        cfg = BotConfig()
        assert cfg.MIN_LIQUIDITY_USD == 5_000
        assert cfg.MIN_VOLUME_TVL_RATIO == 0.5
        assert cfg.MIN_APR_24H == 100.0
        assert cfg.MIN_BURN_PERCENT == 50.0
        assert cfg.REQUIRE_WSOL_PAIRS is True

    def test_token_safety_defaults(self):
        cfg = BotConfig()
        assert cfg.CHECK_TOKEN_SAFETY is True
        assert cfg.MAX_RUGCHECK_SCORE == 50
        assert cfg.MAX_TOP10_HOLDER_PERCENT == 35.0
        assert cfg.MAX_SINGLE_HOLDER_PERCENT == 15.0
        assert cfg.MIN_TOKEN_HOLDERS == 100

    def test_lp_lock_safety_defaults(self):
        cfg = BotConfig()
        assert cfg.CHECK_LP_LOCK is True
        assert cfg.MIN_SAFE_LP_PERCENT == 50.0
        assert cfg.MAX_SINGLE_LP_HOLDER_PERCENT == 25.0

    def test_position_sizing_defaults(self):
        cfg = BotConfig()
        assert cfg.MAX_ABSOLUTE_POSITION_SOL == 5.0
        assert cfg.MIN_POSITION_SOL == 0.05
        assert cfg.MAX_CONCURRENT_POSITIONS == 3
        assert cfg.RESERVE_SOL == 0.05

    def test_risk_management_defaults(self):
        cfg = BotConfig()
        assert cfg.STOP_LOSS_PERCENT == -25.0
        assert cfg.TAKE_PROFIT_PERCENT == 20.0
        assert cfg.MAX_HOLD_TIME_HOURS == 24
        assert cfg.MAX_IMPERMANENT_LOSS == -5.0
        assert cfg.PERMANENT_BLACKLIST_STRIKES == 3

    def test_trading_defaults(self):
        cfg = BotConfig()
        assert cfg.TRADING_ENABLED is True
        assert cfg.DRY_RUN is False
        assert cfg.SLIPPAGE_PERCENT == 5.0

    def test_monitoring_defaults(self):
        cfg = BotConfig()
        assert cfg.POOL_SCAN_INTERVAL_SEC == 180
        assert cfg.POSITION_CHECK_INTERVAL_SEC == 1
        assert cfg.DISPLAY_INTERVAL_SEC == 4

    def test_bridge_script_path_ends_correctly(self):
        cfg = BotConfig()
        assert cfg.BRIDGE_SCRIPT.endswith("raydium_sdk_bridge.js")


class TestBotConfigPostInit:
    """__post_init__ sets STOP_LOSS_COOLDOWNS when None."""

    def test_cooldowns_default_set(self):
        cfg = BotConfig()
        assert cfg.STOP_LOSS_COOLDOWNS == [86400, 172800]

    def test_cooldowns_none_becomes_list(self):
        cfg = BotConfig(STOP_LOSS_COOLDOWNS=None)
        assert cfg.STOP_LOSS_COOLDOWNS == [86400, 172800]

    def test_cooldowns_explicit_preserved(self):
        cfg = BotConfig(STOP_LOSS_COOLDOWNS=[3600, 7200])
        assert cfg.STOP_LOSS_COOLDOWNS == [3600, 7200]

    def test_cooldowns_empty_list_preserved(self):
        cfg = BotConfig(STOP_LOSS_COOLDOWNS=[])
        assert cfg.STOP_LOSS_COOLDOWNS == []
