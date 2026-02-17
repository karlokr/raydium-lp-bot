"""Tests for bot/config.py — BotConfig dataclass and structural behavior."""
from bot.config import BotConfig


class TestBotConfigStructure:
    """Verify BotConfig fields exist, have correct types, and accept overrides."""

    def test_rpc_endpoint_is_url(self):
        cfg = BotConfig()
        assert isinstance(cfg.RPC_ENDPOINT, str)
        assert cfg.RPC_ENDPOINT.startswith("https://")

    def test_rpc_endpoint_explicit_override(self):
        cfg = BotConfig(RPC_ENDPOINT="https://custom-rpc.io")
        assert cfg.RPC_ENDPOINT == "https://custom-rpc.io"

    def test_pool_filtering_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.MIN_LIQUIDITY_USD, (int, float))
        assert isinstance(cfg.MIN_VOLUME_TVL_RATIO, float)
        assert isinstance(cfg.MIN_APR_24H, float)
        assert isinstance(cfg.MIN_BURN_PERCENT, float)
        assert isinstance(cfg.REQUIRE_WSOL_PAIRS, bool)

    def test_token_safety_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.CHECK_TOKEN_SAFETY, bool)
        assert isinstance(cfg.MAX_RUGCHECK_SCORE, (int, float))
        assert isinstance(cfg.MAX_TOP10_HOLDER_PERCENT, float)
        assert isinstance(cfg.MAX_SINGLE_HOLDER_PERCENT, float)
        assert isinstance(cfg.MIN_TOKEN_HOLDERS, int)

    def test_lp_lock_safety_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.CHECK_LP_LOCK, bool)
        assert isinstance(cfg.MIN_LP_LOCK_PERCENT, float)
        assert isinstance(cfg.MIN_SAFE_LP_PERCENT, float)
        assert isinstance(cfg.MAX_SINGLE_LP_HOLDER_PERCENT, float)

    def test_position_sizing_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.MAX_ABSOLUTE_POSITION_SOL, float)
        assert isinstance(cfg.MIN_POSITION_SOL, float)
        assert isinstance(cfg.MAX_CONCURRENT_POSITIONS, int)
        assert isinstance(cfg.RESERVE_SOL, float)

    def test_risk_management_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.STOP_LOSS_PERCENT, float)
        assert cfg.STOP_LOSS_PERCENT < 0, "STOP_LOSS_PERCENT should be negative"
        assert isinstance(cfg.TAKE_PROFIT_PERCENT, float)
        assert cfg.TAKE_PROFIT_PERCENT > 0, "TAKE_PROFIT_PERCENT should be positive"
        assert isinstance(cfg.MAX_HOLD_TIME_HOURS, (int, float))
        assert isinstance(cfg.MAX_IMPERMANENT_LOSS, float)
        assert cfg.MAX_IMPERMANENT_LOSS < 0, "MAX_IMPERMANENT_LOSS should be negative"
        assert isinstance(cfg.PERMANENT_BLACKLIST_STRIKES, int)

    def test_trading_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.TRADING_ENABLED, bool)
        assert isinstance(cfg.DRY_RUN, bool)
        assert isinstance(cfg.SLIPPAGE_PERCENT, float)

    def test_monitoring_fields_exist(self):
        cfg = BotConfig()
        assert isinstance(cfg.POOL_SCAN_INTERVAL_SEC, (int, float))
        assert isinstance(cfg.POSITION_CHECK_INTERVAL_SEC, (int, float))
        assert isinstance(cfg.DISPLAY_INTERVAL_SEC, (int, float))

    def test_bridge_script_path_ends_correctly(self):
        cfg = BotConfig()
        assert cfg.BRIDGE_SCRIPT.endswith("raydium_sdk_bridge.js")

    def test_overrides_work(self):
        """Verify fields can be overridden at construction time."""
        cfg = BotConfig(STOP_LOSS_PERCENT=-50.0, TAKE_PROFIT_PERCENT=100.0,
                        MAX_CONCURRENT_POSITIONS=10)
        assert cfg.STOP_LOSS_PERCENT == -50.0
        assert cfg.TAKE_PROFIT_PERCENT == 100.0
        assert cfg.MAX_CONCURRENT_POSITIONS == 10


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
