"""
Position Manager - Entry/Exit Logic and Tracking
"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from bot.config import config
from bot.analysis.pool_analyzer import PoolAnalyzer


# Shared analyzer instance (avoid creating one per update_metrics call)
_analyzer = PoolAnalyzer()


# SOL mint address (used to identify which side of a pool is SOL)
SOL_MINT = "So11111111111111111111111111111111111111112"


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

    # Which side is SOL
    sol_is_base: bool = False

    # LP token tracking (set after addLiquidity)
    lp_mint: str = ""
    lp_token_amount: float = 0.0  # raw LP tokens held
    lp_decimals: int = 0

    # Entry value tracking
    entry_lp_value_sol: float = 0.0   # actual LP value right after entry (may differ from position_size_sol due to swap slippage)

    # Re-evaluation tracking (safety re-check every 24h)
    last_reeval_time: str = ""  # ISO timestamp of last safety re-evaluation

    # Tracking
    current_price_ratio: float = 0.0
    current_il_percent: float = 0.0
    fees_earned_sol: float = 0.0
    unrealized_pnl_sol: float = 0.0
    current_lp_value_sol: float = 0.0  # current value of LP tokens in SOL

    # Metadata
    pool_data: Dict = field(default_factory=dict)

    @property
    def sol_amount(self) -> float:
        """Amount of SOL in this position (half the position size)."""
        return self.token_a_amount if self.sol_is_base else self.token_b_amount

    @property
    def other_token_amount(self) -> float:
        """Amount of the non-SOL token in this position."""
        return self.token_b_amount if self.sol_is_base else self.token_a_amount

    @property
    def time_held_hours(self) -> float:
        delta = datetime.now() - self.entry_time
        return delta.total_seconds() / 3600

    @property
    def price_change_percent(self) -> float:
        """Percentage change in token price ratio since entry."""
        if self.entry_price_ratio <= 0:
            return 0.0
        return ((self.current_price_ratio - self.entry_price_ratio) / self.entry_price_ratio) * 100

    @property
    def pnl_percent(self) -> float:
        if self.position_size_sol <= 0:
            return 0.0
        return (self.unrealized_pnl_sol / self.position_size_sol) * 100

    @property
    def entry_slippage_sol(self) -> float:
        """SOL lost to swap slippage on entry (always >= 0).
        Returns 0 if entry LP value was never recorded."""
        if self.entry_lp_value_sol <= 0:
            return 0.0
        return max(0.0, self.position_size_sol - self.entry_lp_value_sol)

    @property
    def entry_slippage_percent(self) -> float:
        """Entry slippage as a percentage of position size."""
        if self.position_size_sol <= 0:
            return 0.0
        return (self.entry_slippage_sol / self.position_size_sol) * 100

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

    @property
    def needs_reeval(self) -> bool:
        """True if position needs a safety re-evaluation (every 24h)."""
        if not self.last_reeval_time:
            # Never re-evaluated yet; check if we've been held > interval
            return self.time_held_hours >= config.POSITION_REEVAL_INTERVAL_HOURS
        try:
            last = datetime.fromisoformat(self.last_reeval_time)
            hours_since = (datetime.now() - last).total_seconds() / 3600
            return hours_since >= config.POSITION_REEVAL_INTERVAL_HOURS
        except (ValueError, TypeError):
            return True

    def update_metrics(self, current_price: float, pool_data: Dict,
                        lp_value_sol: float = None):
        """Update position metrics with current data.
        
        If lp_value_sol is provided (from on-chain LP token valuation),
        PnL is computed directly as actual_value - entry_cost.
        Otherwise PnL stays at 0 (unknown) — we never guess from APR.
        IL is always computed from the price ratio.
        """
        self.current_price_ratio = current_price
        self.pool_data = pool_data

        # IL from price ratio (always computed when we have prices)
        if self.entry_price_ratio > 0 and self.current_price_ratio > 0:
            self.current_il_percent = _analyzer.calculate_impermanent_loss(
                self.entry_price_ratio,
                self.current_price_ratio
            ) * 100
        else:
            self.current_il_percent = 0.0

        if lp_value_sol is not None and lp_value_sol > 0:
            # Sanity check: LP value should not be wildly different from entry size.
            # Reject if > 5x entry size (clearly a calculation error).
            if self.position_size_sol > 0 and lp_value_sol > self.position_size_sol * 5:
                # Bad data — ignore this reading
                self.unrealized_pnl_sol = 0.0
                self.fees_earned_sol = 0.0
            else:
                # Real PnL from on-chain LP token value
                self.current_lp_value_sol = lp_value_sol
                self.unrealized_pnl_sol = lp_value_sol - self.position_size_sol

                # Back-derive fees using the LP return factor.
                # For a CPMM, the LP value (in SOL, excluding fees) changes by:
                #   sol_is_base:  factor = 1 / sqrt(r)
                #   sol_is_quote: factor = sqrt(r)
                # where r = current_price_ratio / entry_price_ratio.
                #
                # fees = current_lp_value - entry_baseline * factor
                # This correctly separates:
                #   - token price exposure (captured in factor)
                #   - divergence loss / IL  (captured in factor)
                #   - fee income (the residual)
                #
                # If entry_lp_value_sol is not yet set (first reading), skip fee calc.
                if self.entry_lp_value_sol > 0:
                    if self.entry_price_ratio > 0 and self.current_price_ratio > 0:
                        r = self.current_price_ratio / self.entry_price_ratio
                        if self.sol_is_base:
                            lp_return_factor = 1.0 / math.sqrt(r)
                        else:
                            lp_return_factor = math.sqrt(r)
                        no_fee_value = self.entry_lp_value_sol * lp_return_factor
                        self.fees_earned_sol = lp_value_sol - no_fee_value
                    else:
                        self.fees_earned_sol = 0.0
                else:
                    # First reading before entry_lp_value_sol is recorded
                    self.fees_earned_sol = 0.0
        else:
            # No on-chain data available — keep last known PnL values
            # (they'll be refreshed next cycle when the bridge responds)
            pass


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
        sol_price_usd: float = 0.0,
        total_wallet_balance: float = 0.0,
        rank: int = 0,
        total_ranked: int = 1,
    ) -> Optional[Position]:
        """Open a new LP position.
        
        Args:
            pool: Pool data dict.
            available_capital: Current available SOL (after deployed positions).
            current_price: Current price ratio for the pool.
            sol_price_usd: SOL/USD price for display.
            total_wallet_balance: Original total wallet balance (for reserve calc).
            rank: This pool's rank (0 = best score, higher = worse).
            total_ranked: Total number of ranked candidate pools.
        """
        if not self.can_open_position(available_capital):
            print(f"✗ Cannot open position: max positions or no capital")
            return None

        num_open = len(self.active_positions)
        position_size = self.analyzer.calculate_position_size(
            pool,
            available_capital,
            num_open_positions=num_open,
        )

        if position_size < config.MIN_POSITION_SOL:
            print(f"✗ Position too small: {position_size:.4f} SOL (min: {config.MIN_POSITION_SOL})")
            return None

        # Determine which side of the pool is SOL
        base_mint = pool.get('baseMint', '')
        sol_is_base = (base_mint == SOL_MINT)

        # Split position 50/50 between SOL and the other token
        sol_value = position_size / 2
        other_value = position_size / 2

        if sol_is_base:
            # SOL is mintA (base), other token is mintB (quote)
            # price = mintB_amount / mintA_amount, so other_amount = sol_value * price
            token_a_amount = sol_value  # SOL amount
            token_b_amount = other_value * current_price if current_price > 0 else 0
        else:
            # Other token is mintA (base), SOL is mintB (quote)
            # price = mintB_amount / mintA_amount = SOL_per_token
            token_a_amount = other_value / current_price if current_price > 0 else 0
            token_b_amount = sol_value  # SOL amount

        amm_id = pool.get('ammId', pool.get('id', ''))

        position = Position(
            amm_id=amm_id,
            pool_name=pool.get('name', 'Unknown'),
            entry_time=datetime.now(),
            entry_price_ratio=current_price,
            position_size_sol=position_size,
            token_a_amount=token_a_amount,
            token_b_amount=token_b_amount,
            sol_is_base=sol_is_base,
            pool_data=pool,
        )

        position.current_price_ratio = current_price
        self.active_positions[amm_id] = position

        # Show how much of the total wallet is being deployed
        deploy_percent = (position_size / total_wallet_balance * 100) if total_wallet_balance > 0 else 0
        reserve_after = available_capital - position_size
        reserve_pct = (reserve_after / total_wallet_balance * 100) if total_wallet_balance > 0 else 0

        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        score = pool.get('score', 0)

        print(f"✓ Opened position in {pool.get('name', 'Unknown')}")
        size_str = f"{position_size:.4f} SOL"
        if sol_price_usd > 0:
            size_str += f" (${position_size * sol_price_usd:.2f})"
        print(f"  Size: {size_str} ({deploy_percent:.1f}% of wallet)")
        print(f"  Reserve after: {reserve_after:.4f} SOL ({reserve_pct:.1f}% of wallet)")
        print(f"  Pool score: {score:.1f} (rank #{rank + 1})")
        print(f"  Entry price: {current_price:.10f}")
        print(f"  Expected APR: {apr:.2f}%")
        print(f"  Position {num_open + 1}/{config.MAX_CONCURRENT_POSITIONS}")

        return position

    def close_position(self, amm_id: str, reason: str = "Manual",
                        sol_price_usd: float = 0.0) -> bool:
        """Close an LP position."""
        if amm_id not in self.active_positions:
            print(f"✗ Position not found: {amm_id}")
            return False

        position = self.active_positions[amm_id]

        def _sol_usd(sol: float) -> str:
            if sol_price_usd > 0:
                return f"{sol:.4f} SOL (${sol * sol_price_usd:.2f})"
            return f"{sol:.4f} SOL"

        print(f"✓ Closing position in {position.pool_name}")
        print(f"  Reason: {reason}")
        print(f"  Time held: {position.time_held_hours:.1f}h")
        print(f"  IL: {position.current_il_percent:.2f}%")
        print(f"  Fees earned: {_sol_usd(position.fees_earned_sol)}")
        print(f"  Net P&L: {_sol_usd(position.unrealized_pnl_sol)}")

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
        checks = [
            ('should_exit_sl',   lambda p: f"⚠ Stop loss hit: {p.pool_name} (P&L: {p.pnl_percent:.2f}%)",   "Stop Loss"),
            ('should_exit_tp',   lambda p: f"✓ Take profit hit: {p.pool_name} (P&L: {p.pnl_percent:.2f}%)", "Take Profit"),
            ('should_exit_time', lambda p: f"⏰ Max hold time: {p.pool_name} ({p.time_held_hours:.1f}h)",    "Max Time"),
            ('should_exit_il',   lambda p: f"⚠ High IL: {p.pool_name} ({p.current_il_percent:.2f}%)",       "High IL"),
        ]
        to_close = []
        for amm_id, pos in self.active_positions.items():
            for attr, msg_fn, reason in checks:
                if getattr(pos, attr):
                    print(msg_fn(pos))
                    to_close.append((amm_id, reason))
                    break
        return to_close

    def get_total_deployed_capital(self) -> float:
        """Total current value of deployed capital.
        Uses on-chain LP value when available, falls back to entry cost."""
        return sum(
            pos.current_lp_value_sol if pos.current_lp_value_sol > 0 else pos.position_size_sol
            for pos in self.active_positions.values()
        )

    def get_summary(self) -> Dict:
        total_deployed = self.get_total_deployed_capital()
        total_pnl = sum(pos.unrealized_pnl_sol for pos in self.active_positions.values())
        total_fees = sum(pos.fees_earned_sol for pos in self.active_positions.values())
        total_slippage = sum(pos.entry_slippage_sol for pos in self.active_positions.values())
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
            'total_slippage_sol': total_slippage,
            'avg_il_percent': avg_il,
        }
