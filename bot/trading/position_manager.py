"""
Position Manager - Entry/Exit Logic and Tracking
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from bot.config import config
from bot.analysis.pool_analyzer import PoolAnalyzer


# Shared analyzer instance (avoid creating one per update_metrics call)
_analyzer = PoolAnalyzer()


@dataclass
class Position:
    """Represents an active LP position."""
    amm_id: str
    pool_name: str
    entry_time: datetime
    entry_price_ratio: float
    position_size_sol: float
    token_a_amount: float
    token_b_amount: float

    # Tracking
    current_price_ratio: float = 0.0
    current_il_percent: float = 0.0
    fees_earned_sol: float = 0.0
    unrealized_pnl_sol: float = 0.0

    # Metadata
    pool_data: Dict = field(default_factory=dict)

    @property
    def time_held_hours(self) -> float:
        delta = datetime.now() - self.entry_time
        return delta.total_seconds() / 3600

    @property
    def pnl_percent(self) -> float:
        if self.position_size_sol <= 0:
            return 0.0
        return (self.unrealized_pnl_sol / self.position_size_sol) * 100

    @property
    def should_exit_sl(self) -> bool:
        return self.pnl_percent <= config.STOP_LOSS_PERCENT

    @property
    def should_exit_tp(self) -> bool:
        return self.pnl_percent >= config.TAKE_PROFIT_PERCENT

    @property
    def should_exit_time(self) -> bool:
        return self.time_held_hours >= config.MAX_HOLD_TIME_HOURS

    @property
    def should_exit_il(self) -> bool:
        return self.current_il_percent <= config.MAX_IMPERMANENT_LOSS

    def update_metrics(self, current_price: float, pool_data: Dict):
        """Update position metrics with current data."""
        self.current_price_ratio = current_price
        self.pool_data = pool_data

        if self.entry_price_ratio > 0 and self.current_price_ratio > 0:
            self.current_il_percent = _analyzer.calculate_impermanent_loss(
                self.entry_price_ratio,
                self.current_price_ratio
            ) * 100
        else:
            self.current_il_percent = 0.0

        self.fees_earned_sol = _analyzer.estimate_fees_earned(
            pool_data,
            self.position_size_sol,
            self.time_held_hours
        )

        il_loss_sol = (self.current_il_percent / 100) * self.position_size_sol
        self.unrealized_pnl_sol = self.fees_earned_sol + il_loss_sol


class PositionManager:
    """Manages active LP positions."""

    def __init__(self):
        self.active_positions: Dict[str, Position] = {}
        self.analyzer = _analyzer

    def can_open_position(self, available_capital: float) -> bool:
        if len(self.active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False
        if available_capital <= 0:
            return False
        return True

    def open_position(
        self,
        pool: Dict,
        available_capital: float,
        current_price: float,
    ) -> Optional[Position]:
        """Open a new LP position."""
        if not self.can_open_position(available_capital):
            print(f"✗ Cannot open position: max positions or no capital")
            return None

        num_open = len(self.active_positions)
        position_size = self.analyzer.calculate_position_size(
            pool,
            available_capital,
            num_open_positions=num_open
        )

        if position_size < 0.01:
            print(f"✗ Position too small: {position_size:.4f} SOL")
            return None

        # Split position 50/50 between token A and WSOL
        token_a_value = position_size / 2
        token_b_value = position_size / 2
        token_a_amount = token_a_value / current_price if current_price > 0 else 0
        token_b_amount = token_b_value

        amm_id = pool.get('ammId', pool.get('id', ''))

        position = Position(
            amm_id=amm_id,
            pool_name=pool.get('name', 'Unknown'),
            entry_time=datetime.now(),
            entry_price_ratio=current_price,
            position_size_sol=position_size,
            token_a_amount=token_a_amount,
            token_b_amount=token_b_amount,
            pool_data=pool,
        )

        position.current_price_ratio = current_price
        self.active_positions[amm_id] = position

        positions_remaining = config.MAX_CONCURRENT_POSITIONS - num_open
        dynamic_percent = (1.0 / positions_remaining) * 100

        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)

        print(f"✓ Opened position in {pool.get('name', 'Unknown')}")
        print(f"  Size: {position_size:.4f} SOL ({dynamic_percent:.1f}% of available capital)")
        print(f"  Entry price: {current_price:.10f}")
        print(f"  Expected APR: {apr:.2f}%")
        print(f"  Position {num_open + 1}/{config.MAX_CONCURRENT_POSITIONS}")

        return position

    def close_position(self, amm_id: str, reason: str = "Manual") -> bool:
        """Close an LP position."""
        if amm_id not in self.active_positions:
            print(f"✗ Position not found: {amm_id}")
            return False

        position = self.active_positions[amm_id]

        print(f"✓ Closing position in {position.pool_name}")
        print(f"  Reason: {reason}")
        print(f"  Time held: {position.time_held_hours:.1f}h")
        print(f"  IL: {position.current_il_percent:.2f}%")
        print(f"  Fees earned: {position.fees_earned_sol:.4f} SOL")
        print(f"  Net P&L: {position.unrealized_pnl_sol:.4f} SOL")

        del self.active_positions[amm_id]
        return True

    def update_all_positions(self, current_prices: Dict[str, float], pools_data: Dict[str, Dict]):
        """Update all active positions with current data."""
        for amm_id, position in list(self.active_positions.items()):
            if amm_id in current_prices and amm_id in pools_data:
                position.update_metrics(
                    current_prices[amm_id],
                    pools_data[amm_id]
                )

    def check_exit_conditions(self) -> List[tuple]:
        """Check all positions for exit conditions."""
        to_close = []

        for amm_id, position in self.active_positions.items():
            if position.should_exit_sl:
                print(f"⚠ Stop loss hit: {position.pool_name} (P&L: {position.pnl_percent:.2f}%)")
                to_close.append((amm_id, "Stop Loss"))
            elif position.should_exit_tp:
                print(f"✓ Take profit hit: {position.pool_name} (P&L: {position.pnl_percent:.2f}%)")
                to_close.append((amm_id, "Take Profit"))
            elif position.should_exit_time:
                print(f"⏰ Max hold time: {position.pool_name} ({position.time_held_hours:.1f}h)")
                to_close.append((amm_id, "Max Time"))
            elif position.should_exit_il:
                print(f"⚠ High IL: {position.pool_name} ({position.current_il_percent:.2f}%)")
                to_close.append((amm_id, "High IL"))

        return to_close

    def get_total_deployed_capital(self) -> float:
        return sum(pos.position_size_sol for pos in self.active_positions.values())

    def get_summary(self) -> Dict:
        total_deployed = self.get_total_deployed_capital()
        total_pnl = sum(pos.unrealized_pnl_sol for pos in self.active_positions.values())
        total_fees = sum(pos.fees_earned_sol for pos in self.active_positions.values())
        avg_il = (
            sum(pos.current_il_percent for pos in self.active_positions.values())
            / len(self.active_positions)
            if self.active_positions else 0
        )

        return {
            'active_positions': len(self.active_positions),
            'total_deployed_sol': total_deployed,
            'total_pnl_sol': total_pnl,
            'total_fees_sol': total_fees,
            'avg_il_percent': avg_il,
        }
