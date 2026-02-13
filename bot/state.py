"""
Persistent bot state — saves all relevant data to disk between runs.

Saves to data/bot_state.json:
  - Active positions (full Position dataclass)
  - Closed position history (summary per trade)
  - Exit cooldowns (pool_id -> timestamp)
  - Failed pool IDs
  - Snapshot tracker history (per-pool rolling window)
  - Last scan results (top-ranked pools + scores)
  - Bot runtime metadata (last save time, version)

Auto-saves after every state change (entry, exit, scan).
Loads on startup to resume seamlessly.
"""
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

from bot.config import config


# Default state directory (next to the project root)
STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
STATE_FILE = os.path.join(STATE_DIR, 'bot_state.json')
HISTORY_FILE = os.path.join(STATE_DIR, 'trade_history.jsonl')


def _ensure_dir():
    """Create data directory if it doesn't exist."""
    os.makedirs(STATE_DIR, exist_ok=True)


# ── Position serialization ──────────────────────────────────────────

def position_to_dict(pos) -> dict:
    """Serialize a Position dataclass to a JSON-safe dict."""
    return {
        'amm_id': pos.amm_id,
        'pool_name': pos.pool_name,
        'entry_time': pos.entry_time.isoformat(),
        'entry_price_ratio': pos.entry_price_ratio,
        'position_size_sol': pos.position_size_sol,
        'token_a_amount': pos.token_a_amount,
        'token_b_amount': pos.token_b_amount,
        'sol_is_base': pos.sol_is_base,
        'lp_mint': pos.lp_mint,
        'lp_token_amount': pos.lp_token_amount,
        'lp_decimals': pos.lp_decimals,
        'current_price_ratio': pos.current_price_ratio,
        'current_il_percent': pos.current_il_percent,
        'fees_earned_sol': pos.fees_earned_sol,
        'unrealized_pnl_sol': pos.unrealized_pnl_sol,
        'current_lp_value_sol': pos.current_lp_value_sol,
        'pool_data': _sanitize_pool_data(pos.pool_data),
    }


def position_from_dict(d: dict):
    """Deserialize a dict back to a Position dataclass."""
    from bot.trading.position_manager import Position

    pos = Position(
        amm_id=d['amm_id'],
        pool_name=d['pool_name'],
        entry_time=datetime.fromisoformat(d['entry_time']),
        entry_price_ratio=d['entry_price_ratio'],
        position_size_sol=d['position_size_sol'],
        token_a_amount=d['token_a_amount'],
        token_b_amount=d['token_b_amount'],
        sol_is_base=d.get('sol_is_base', False),
        lp_mint=d.get('lp_mint', ''),
        lp_token_amount=d.get('lp_token_amount', 0.0),
        lp_decimals=d.get('lp_decimals', 0),
        current_price_ratio=d.get('current_price_ratio', 0.0),
        current_il_percent=d.get('current_il_percent', 0.0),
        fees_earned_sol=d.get('fees_earned_sol', 0.0),
        unrealized_pnl_sol=d.get('unrealized_pnl_sol', 0.0),
        current_lp_value_sol=d.get('current_lp_value_sol', 0.0),
        pool_data=d.get('pool_data', {}),
    )
    return pos


def _sanitize_pool_data(pool_data: dict) -> dict:
    """Remove non-serializable values from pool_data."""
    if not pool_data:
        return {}
    clean = {}
    for k, v in pool_data.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            clean[k] = v
        elif isinstance(v, dict):
            clean[k] = _sanitize_pool_data(v)
        elif isinstance(v, (list, tuple)):
            clean[k] = [
                x for x in v
                if isinstance(x, (str, int, float, bool, type(None)))
            ]
        # Skip non-serializable types
    return clean


# ── Snapshot tracker serialization ──────────────────────────────────

def snapshots_to_dict(tracker) -> dict:
    """Serialize a SnapshotTracker's history to a JSON-safe dict."""
    result = {}
    for pool_id, deq in tracker._history.items():
        result[pool_id] = [
            {
                'timestamp': s.timestamp,
                'volume_24h': s.volume_24h,
                'tvl': s.tvl,
                'price': s.price,
            }
            for s in deq
        ]
    return result


def snapshots_from_dict(tracker, data: dict):
    """Restore snapshot history into an existing SnapshotTracker."""
    from bot.analysis.snapshot_tracker import Snapshot

    for pool_id, snaps in data.items():
        for s in snaps:
            snap = Snapshot(
                timestamp=s['timestamp'],
                volume_24h=s['volume_24h'],
                tvl=s['tvl'],
                price=s['price'],
            )
            tracker._history[pool_id].append(snap)


# ── Trade history (append-only JSONL) ───────────────────────────────

def append_trade_history(position, reason: str, sol_price_usd: float = 0.0):
    """Append a closed position's summary to the trade history log."""
    _ensure_dir()
    record = {
        'closed_at': datetime.now().isoformat(),
        'amm_id': position.amm_id,
        'pool_name': position.pool_name,
        'entry_time': position.entry_time.isoformat(),
        'hold_time_hours': round(position.time_held_hours, 2),
        'reason': reason,
        'position_size_sol': position.position_size_sol,
        'pnl_sol': round(position.unrealized_pnl_sol, 6),
        'pnl_percent': round(position.pnl_percent, 2),
        'fees_earned_sol': round(position.fees_earned_sol, 6),
        'il_percent': round(position.current_il_percent, 4),
        'entry_price': position.entry_price_ratio,
        'exit_price': position.current_price_ratio,
        'sol_price_usd': round(sol_price_usd, 2),
    }
    try:
        with open(HISTORY_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        print(f"⚠ Could not write trade history: {e}")


def load_trade_history() -> List[dict]:
    """Load all trade history records."""
    if not os.path.exists(HISTORY_FILE):
        return []
    records = []
    try:
        with open(HISTORY_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except Exception as e:
        print(f"⚠ Could not load trade history: {e}")
    return records


# ── Full state save/load ────────────────────────────────────────────

def save_state(
    positions: Dict,
    exit_cooldowns: Dict[str, float],
    failed_pools: set,
    snapshot_tracker=None,
    last_scan_pools: List[dict] = None,
):
    """Save complete bot state to disk.

    Args:
        positions: Dict of amm_id -> Position (from PositionManager)
        exit_cooldowns: Dict of amm_id -> timestamp
        failed_pools: Set of pool IDs that failed
        snapshot_tracker: Optional SnapshotTracker instance
        last_scan_pools: Optional list of top-ranked pools from last scan
    """
    _ensure_dir()

    state = {
        'saved_at': datetime.now().isoformat(),
        'saved_timestamp': time.time(),
        'positions': {
            amm_id: position_to_dict(pos)
            for amm_id, pos in positions.items()
        },
        'exit_cooldowns': exit_cooldowns,
        'failed_pools': list(failed_pools),
        'snapshots': snapshots_to_dict(snapshot_tracker) if snapshot_tracker else {},
        'last_scan_pools': [
            _sanitize_pool_data(p) for p in (last_scan_pools or [])
        ],
    }

    # Write atomically (write to tmp then rename)
    tmp_path = STATE_FILE + '.tmp'
    try:
        with open(tmp_path, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"⚠ Could not save state: {e}")
        # Clean up tmp file
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def load_state() -> Optional[dict]:
    """Load bot state from disk.

    Returns None if no state file exists.
    Returns a dict with keys: positions, exit_cooldowns, failed_pools,
    snapshots, last_scan_pools, saved_at, saved_timestamp.
    """
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)

        # Deserialize positions
        positions = {}
        for amm_id, pos_dict in state.get('positions', {}).items():
            try:
                positions[amm_id] = position_from_dict(pos_dict)
            except Exception as e:
                print(f"⚠ Could not restore position {amm_id}: {e}")

        return {
            'positions': positions,
            'exit_cooldowns': state.get('exit_cooldowns', {}),
            'failed_pools': set(state.get('failed_pools', [])),
            'snapshots': state.get('snapshots', {}),
            'last_scan_pools': state.get('last_scan_pools', []),
            'saved_at': state.get('saved_at', ''),
            'saved_timestamp': state.get('saved_timestamp', 0),
        }

    except Exception as e:
        print(f"⚠ Could not load state from {STATE_FILE}: {e}")
        return None


def clear_state():
    """Delete the state file (e.g. after clean shutdown with no positions)."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except OSError:
        pass
