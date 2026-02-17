"""Tests for bot/trading/position_manager.py — Position dataclass and PositionManager."""
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest

from bot.trading.position_manager import Position, PositionManager, SOL_MINT
from bot.config import config


# ── Position dataclass properties ────────────────────────────────────

class TestPositionProperties:

    def test_sol_amount_when_sol_is_base(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0.5, token_b_amount=100,
            sol_is_base=True,
        )
        assert pos.sol_amount == 0.5  # token_a
        assert pos.other_token_amount == 100  # token_b

    def test_sol_amount_when_sol_is_quote(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=100, token_b_amount=0.5,
            sol_is_base=False,
        )
        assert pos.sol_amount == 0.5  # token_b
        assert pos.other_token_amount == 100  # token_a

    def test_time_held_hours(self):
        pos = Position(
            amm_id="p", pool_name="P",
            entry_time=datetime.now() - timedelta(hours=3),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        assert 2.9 < pos.time_held_hours < 3.1

    def test_price_change_percent(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=100.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.current_price_ratio = 120.0
        assert pos.price_change_percent == pytest.approx(20.0)

    def test_price_change_percent_zero_entry(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=0.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        assert pos.price_change_percent == 0.0

    def test_pnl_percent(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=2.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.unrealized_pnl_sol = 0.5
        assert pos.pnl_percent == pytest.approx(25.0)

    def test_pnl_percent_zero_size(self):
        pos = Position(
            amm_id="p", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=0.0,
            token_a_amount=0, token_b_amount=0,
        )
        assert pos.pnl_percent == 0.0


class TestExitConditions:

    def _make_pos(self, pnl_pct=0, hours=1, il_pct=0):
        pos = Position(
            amm_id="p", pool_name="P",
            entry_time=datetime.now() - timedelta(hours=hours),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.unrealized_pnl_sol = pnl_pct / 100.0  # since size=1.0
        pos.current_il_percent = il_pct
        return pos

    def test_stop_loss(self):
        pos = self._make_pos(pnl_pct=config.STOP_LOSS_PERCENT - 5)
        assert pos.should_exit_sl is True

    def test_no_stop_loss(self):
        pos = self._make_pos(pnl_pct=config.STOP_LOSS_PERCENT + 5)
        assert pos.should_exit_sl is False

    def test_take_profit(self):
        pos = self._make_pos(pnl_pct=config.TAKE_PROFIT_PERCENT + 5)
        assert pos.should_exit_tp is True

    def test_no_take_profit(self):
        pos = self._make_pos(pnl_pct=config.TAKE_PROFIT_PERCENT - 5)
        assert pos.should_exit_tp is False

    def test_max_time(self):
        pos = self._make_pos(hours=config.MAX_HOLD_TIME_HOURS + 1)
        assert pos.should_exit_time is True

    def test_no_max_time(self):
        pos = self._make_pos(hours=max(1, config.MAX_HOLD_TIME_HOURS - 12))
        assert pos.should_exit_time is False

    def test_il_exit(self):
        pos = self._make_pos(il_pct=config.MAX_IMPERMANENT_LOSS - 1.0)
        assert pos.should_exit_il is True

    def test_no_il_exit(self):
        pos = self._make_pos(il_pct=config.MAX_IMPERMANENT_LOSS + 3.0)
        assert pos.should_exit_il is False


class TestUpdateMetrics:

    def test_pnl_from_lp_value(self, sample_position):
        sample_position.position_size_sol = 1.0
        sample_position.entry_price_ratio = 1.0
        sample_position.update_metrics(1.0, {}, lp_value_sol=1.10)
        assert sample_position.unrealized_pnl_sol == pytest.approx(0.10)

    def test_sanity_check_rejects_wild_value(self, sample_position):
        sample_position.position_size_sol = 1.0
        sample_position.update_metrics(1.0, {}, lp_value_sol=100.0)
        assert sample_position.unrealized_pnl_sol == 0.0

    def test_il_computed(self, sample_position):
        sample_position.entry_price_ratio = 1.0
        sample_position.update_metrics(2.0, {})
        assert sample_position.current_il_percent < 0  # IL is negative

    def test_no_lp_value_preserves_last(self, sample_position):
        sample_position.unrealized_pnl_sol = 0.05
        sample_position.entry_price_ratio = 1.0
        sample_position.update_metrics(1.0, {}, lp_value_sol=None)
        # Should keep last known value
        assert sample_position.unrealized_pnl_sol == pytest.approx(0.05)


# ── PositionManager ──────────────────────────────────────────────────

class TestPositionManagerCanOpen:

    def test_can_open_with_capital(self):
        pm = PositionManager()
        assert pm.can_open_position(5.0) is True

    def test_cannot_open_no_capital(self):
        pm = PositionManager()
        assert pm.can_open_position(0) is False

    def test_cannot_open_at_max(self):
        pm = PositionManager()
        for i in range(3):
            pm.active_positions[f"pool{i}"] = Position(
                amm_id=f"pool{i}", pool_name="P", entry_time=datetime.now(),
                entry_price_ratio=1.0, position_size_sol=1.0,
                token_a_amount=0, token_b_amount=0,
            )
        assert pm.can_open_position(10.0) is False


class TestOpenPosition:

    def test_opens_successfully(self, sample_pool):
        pm = PositionManager()
        pos = pm.open_position(
            sample_pool, available_capital=5.0, current_price=0.00001234,
            total_wallet_balance=10.0,
        )
        assert pos is not None
        assert "pool123" in pm.active_positions
        assert pos.sol_is_base is False  # quoteMint is WSOL → SOL is quote

    def test_sol_is_base_detection(self, sample_pool_sol_base):
        pm = PositionManager()
        pos = pm.open_position(
            sample_pool_sol_base, available_capital=5.0, current_price=50000,
            total_wallet_balance=10.0,
        )
        assert pos is not None
        assert pos.sol_is_base is True

    def test_too_small_position_rejected(self, sample_pool):
        pm = PositionManager()
        pos = pm.open_position(
            sample_pool, available_capital=0.08, current_price=0.00001234,
            total_wallet_balance=0.1,
        )
        assert pos is None

    def test_max_positions_prevents_open(self, sample_pool):
        pm = PositionManager()
        for i in range(3):
            pm.active_positions[f"p{i}"] = Position(
                amm_id=f"p{i}", pool_name="P", entry_time=datetime.now(),
                entry_price_ratio=1.0, position_size_sol=1.0,
                token_a_amount=0, token_b_amount=0,
            )
        pos = pm.open_position(
            sample_pool, available_capital=10.0, current_price=0.00001,
            total_wallet_balance=15.0,
        )
        assert pos is None


class TestClosePosition:

    def test_closes_successfully(self, sample_pool):
        pm = PositionManager()
        pm.open_position(sample_pool, 5.0, 0.00001, total_wallet_balance=10.0)
        result = pm.close_position("pool123", "Take Profit")
        assert result is True
        assert "pool123" not in pm.active_positions

    def test_close_nonexistent(self):
        pm = PositionManager()
        assert pm.close_position("nope") is False


class TestCheckExitConditions:

    def test_detects_stop_loss(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.unrealized_pnl_sol = -0.30  # -30%
        pm.active_positions["p1"] = pos
        exits = pm.check_exit_conditions()
        assert len(exits) == 1
        assert exits[0] == ("p1", "Stop Loss")

    def test_detects_take_profit(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.unrealized_pnl_sol = 0.25  # +25%
        pm.active_positions["p1"] = pos
        exits = pm.check_exit_conditions()
        assert len(exits) == 1
        assert exits[0] == ("p1", "Take Profit")

    def test_no_exits(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
        )
        pm.active_positions["p1"] = pos
        exits = pm.check_exit_conditions()
        assert len(exits) == 0


class TestGetSummary:

    def test_empty_summary(self):
        pm = PositionManager()
        s = pm.get_summary()
        assert s["active_positions"] == 0
        assert s["total_deployed_sol"] == 0
        assert s["avg_il_percent"] == 0

    def test_with_positions(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=2.0,
            token_a_amount=0, token_b_amount=0,
        )
        pos.unrealized_pnl_sol = 0.2
        pos.fees_earned_sol = 0.1
        pos.current_il_percent = -1.5
        pos.current_lp_value_sol = 2.2
        pm.active_positions["p1"] = pos

        s = pm.get_summary()
        assert s["active_positions"] == 1
        assert s["total_deployed_sol"] == pytest.approx(2.2)
        assert s["total_pnl_sol"] == pytest.approx(0.2)
        assert s["total_fees_sol"] == pytest.approx(0.1)
        assert s["avg_il_percent"] == pytest.approx(-1.5)


class TestGetTotalDeployedCapital:

    def test_uses_lp_value_when_available(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=1.0,
            token_a_amount=0, token_b_amount=0,
            current_lp_value_sol=1.5,
        )
        pm.active_positions["p1"] = pos
        assert pm.get_total_deployed_capital() == pytest.approx(1.5)

    def test_falls_back_to_entry_size(self):
        pm = PositionManager()
        pos = Position(
            amm_id="p1", pool_name="P", entry_time=datetime.now(),
            entry_price_ratio=1.0, position_size_sol=2.0,
            token_a_amount=0, token_b_amount=0,
            current_lp_value_sol=0,
        )
        pm.active_positions["p1"] = pos
        assert pm.get_total_deployed_capital() == pytest.approx(2.0)
