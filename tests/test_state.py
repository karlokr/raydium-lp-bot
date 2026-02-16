"""Tests for bot/state.py — serialization, persistence, trade history."""
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest

from bot.state import (
    position_to_dict,
    position_from_dict,
    _sanitize_pool_data,
    snapshots_to_dict,
    snapshots_from_dict,
    append_trade_history,
    load_trade_history,
    save_state,
    load_state,
    clear_state,
)
from bot.trading.position_manager import Position
from bot.analysis.snapshot_tracker import SnapshotTracker, Snapshot


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Redirect state module paths to a temp directory."""
    state_file = str(tmp_path / "bot_state.json")
    history_file = str(tmp_path / "trade_history.jsonl")
    with patch("bot.state.STATE_DIR", str(tmp_path)), \
         patch("bot.state.STATE_FILE", state_file), \
         patch("bot.state.HISTORY_FILE", history_file):
        yield tmp_path, state_file, history_file


def _make_position(**overrides):
    defaults = dict(
        amm_id="pool1",
        pool_name="BONK/WSOL",
        entry_time=datetime(2025, 6, 1, 12, 0, 0),
        entry_price_ratio=0.00001,
        position_size_sol=1.0,
        token_a_amount=500_000,
        token_b_amount=0.5,
        sol_is_base=False,
        lp_mint="lpMint123",
        lp_token_amount=1_000_000,
        lp_decimals=9,
    )
    defaults.update(overrides)
    return Position(**defaults)


# ── position_to_dict / position_from_dict ────────────────────────────

class TestPositionSerialization:

    def test_roundtrip(self):
        pos = _make_position()
        d = position_to_dict(pos)
        restored = position_from_dict(d)
        assert restored.amm_id == pos.amm_id
        assert restored.pool_name == pos.pool_name
        assert restored.entry_time == pos.entry_time
        assert restored.entry_price_ratio == pos.entry_price_ratio
        assert restored.position_size_sol == pos.position_size_sol
        assert restored.sol_is_base == pos.sol_is_base
        assert restored.lp_mint == pos.lp_mint
        assert restored.lp_token_amount == pos.lp_token_amount

    def test_dict_has_all_keys(self):
        pos = _make_position()
        d = position_to_dict(pos)
        expected_keys = [
            "amm_id", "pool_name", "entry_time", "entry_price_ratio",
            "position_size_sol", "token_a_amount", "token_b_amount",
            "sol_is_base", "lp_mint", "lp_token_amount", "lp_decimals",
            "current_price_ratio", "current_il_percent", "fees_earned_sol",
            "unrealized_pnl_sol", "current_lp_value_sol", "pool_data",
        ]
        for key in expected_keys:
            assert key in d

    def test_entry_time_isoformat(self):
        pos = _make_position()
        d = position_to_dict(pos)
        assert "T" in d["entry_time"]  # ISO format

    def test_from_dict_defaults(self):
        """Older state files may not have all keys — defaults should apply."""
        minimal = {
            "amm_id": "p1",
            "pool_name": "X/Y",
            "entry_time": "2025-01-01T00:00:00",
            "entry_price_ratio": 1.0,
            "position_size_sol": 1.0,
            "token_a_amount": 100,
            "token_b_amount": 100,
        }
        pos = position_from_dict(minimal)
        assert pos.sol_is_base is False  # default
        assert pos.lp_mint == ""
        assert pos.lp_token_amount == 0.0


# ── _sanitize_pool_data ─────────────────────────────────────────────

class TestSanitizePoolData:

    def test_primitives_pass_through(self):
        data = {"a": 1, "b": "hello", "c": 3.14, "d": True, "e": None}
        assert _sanitize_pool_data(data) == data

    def test_nested_dict(self):
        data = {"outer": {"inner": 42}}
        assert _sanitize_pool_data(data) == {"outer": {"inner": 42}}

    def test_non_serializable_removed(self):
        data = {"ok": 1, "bad": lambda x: x}
        result = _sanitize_pool_data(data)
        assert "ok" in result
        assert "bad" not in result

    def test_list_filtering(self):
        data = {"items": [1, "two", lambda: None, 3.0]}
        result = _sanitize_pool_data(data)
        assert result["items"] == [1, "two", 3.0]

    def test_empty_dict(self):
        assert _sanitize_pool_data({}) == {}

    def test_none_input(self):
        assert _sanitize_pool_data(None) == {}


# ── snapshots_to_dict / snapshots_from_dict ──────────────────────────

class TestSnapshotSerialization:

    def test_roundtrip(self):
        tracker = SnapshotTracker(max_snapshots=5)
        tracker.record("pool1", 1000, 50000, 1.5)
        tracker.record("pool1", 1100, 51000, 1.6)
        tracker.record("pool2", 2000, 80000, 0.5)

        data = snapshots_to_dict(tracker)
        assert "pool1" in data
        assert len(data["pool1"]) == 2
        assert len(data["pool2"]) == 1

        # Restore into a fresh tracker
        restored = SnapshotTracker(max_snapshots=5)
        snapshots_from_dict(restored, data)
        assert restored.pool_count() == 2
        assert len(restored._history["pool1"]) == 2

    def test_empty_tracker(self):
        tracker = SnapshotTracker()
        data = snapshots_to_dict(tracker)
        assert data == {}


# ── append_trade_history / load_trade_history ────────────────────────

class TestTradeHistory:

    def test_append_and_load(self, tmp_data_dir):
        pos = _make_position()
        pos.unrealized_pnl_sol = 0.05
        pos.current_price_ratio = 0.000012
        pos.current_il_percent = -1.5
        pos.fees_earned_sol = 0.03

        append_trade_history(pos, "Take Profit", sol_price_usd=170.0)
        records = load_trade_history()
        assert len(records) == 1
        assert records[0]["reason"] == "Take Profit"
        assert records[0]["pool_name"] == "BONK/WSOL"
        assert records[0]["sol_price_usd"] == 170.0

    def test_multiple_appends(self, tmp_data_dir):
        for i in range(3):
            pos = _make_position(amm_id=f"pool{i}")
            append_trade_history(pos, f"Reason {i}")
        records = load_trade_history()
        assert len(records) == 3

    def test_load_missing_file(self, tmp_data_dir):
        records = load_trade_history()
        assert records == []

    def test_jsonl_format(self, tmp_data_dir):
        _, _, history_file = tmp_data_dir
        pos = _make_position()
        append_trade_history(pos, "Test")
        with open(history_file, "r") as f:
            lines = f.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert "amm_id" in parsed


# ── save_state / load_state / clear_state ────────────────────────────

class TestSaveLoadState:

    def test_save_and_load_roundtrip(self, tmp_data_dir):
        pos = _make_position()
        tracker = SnapshotTracker()
        tracker.record("pool1", 1000, 50000, 1.0)

        save_state(
            positions={"pool1": pos},
            exit_cooldowns={"pool1": (1000.0, 86400)},
            failed_pools={"bad_pool"},
            snapshot_tracker=tracker,
            last_scan_pools=[{"ammId": "scan1", "score": 80}],
            stop_loss_strikes={"pool1": 2},
            permanent_blacklist={"banned_pool"},
        )

        state = load_state()
        assert state is not None
        assert "pool1" in state["positions"]
        assert state["positions"]["pool1"].amm_id == "pool1"
        assert "pool1" in state["exit_cooldowns"]
        assert "bad_pool" in state["failed_pools"]
        assert state["stop_loss_strikes"]["pool1"] == 2
        assert "banned_pool" in state["permanent_blacklist"]

    def test_load_missing_file(self, tmp_data_dir):
        assert load_state() is None

    def test_clear_state(self, tmp_data_dir):
        _, state_file, _ = tmp_data_dir
        pos = _make_position()
        save_state(positions={"p": pos}, exit_cooldowns={}, failed_pools=set())
        assert os.path.exists(state_file)
        clear_state()
        assert not os.path.exists(state_file)

    def test_clear_nonexistent_file(self, tmp_data_dir):
        clear_state()  # should not raise

    def test_atomic_write(self, tmp_data_dir):
        """Verify tmp file is used for atomic write."""
        _, state_file, _ = tmp_data_dir
        pos = _make_position()
        save_state(positions={"p": pos}, exit_cooldowns={}, failed_pools=set())
        # The .tmp file should NOT exist after a successful write
        assert not os.path.exists(state_file + ".tmp")
        assert os.path.exists(state_file)

    def test_cooldown_tuple_serialization(self, tmp_data_dir):
        """Cooldowns stored as tuples should survive JSON roundtrip."""
        save_state(
            positions={},
            exit_cooldowns={"p1": (1700000000.0, 86400)},
            failed_pools=set(),
        )
        state = load_state()
        # JSON converts tuple → list, so we check list form
        assert state["exit_cooldowns"]["p1"] == [1700000000.0, 86400]

    def test_empty_state(self, tmp_data_dir):
        save_state(positions={}, exit_cooldowns={}, failed_pools=set())
        state = load_state()
        assert state["positions"] == {}
        assert state["exit_cooldowns"] == {}
        assert state["failed_pools"] == set()
