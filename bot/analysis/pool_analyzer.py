"""
Pool Scoring and Analysis — optimised for LP fee farming

⚠️  SAFETY CHECKS ARE PASS/FAIL GATES (handled by pool_quality.py):
    - RugCheck token safety, LP burn, liquidity lock analysis
    - Pools that fail safety checks are EXCLUDED before scoring
    - This scoring only applies to pools that have PASSED all safety checks

Core LP insight: LPs earn fees from trading volume (direction-irrelevant).
What kills LP returns is volatility drag — the adverse-selection cost
of market-making, aka LVR (Loss-Versus-Rebalancing).

The scoring is anchored on a **predicted net return** model.  Default
hold period is 7 days (configurable via MAX_HOLD_TIME_HOURS).  Every
24 hours, positions are re-evaluated through the full safety pipeline;
if a pool fails safety, the position is closed early.

  net_return = daily_yield × hold_days − LVR × hold_days

  daily_yield    — feeAPR / 365   (fees + rewards, in %)
                   Prefers 7-day average fee APR over 24h to
                   smooth out single-day spikes.
  LVR            — σ²/8 per day  (Milionis et al. 2022)
                   σ estimated via multi-period Parkinson (1980):
                   7 daily OHLCV candles from GeckoTerminal →
                   σ² = (1/n) × Σ (ln(Hi/Li))² / (4·ln2)
                   Falls back to single-window Parkinson from
                   API aggregates if candles unavailable.
  slippage       — one round-trip swap fee + price impact (flat cost)

LVR is the academically rigorous cost of being an LP in a CPMM.
It captures the continuous adverse selection from arbitrageurs
and scales linearly with time and with σ².
This single number captures yield, volatility cost,
and slippage without double-counting any signal.  Two guardrails sit
alongside it:

Scoring factors for SAFE pools (0–100):

  Predicted Net Return (75 pts) — the model's total-period prediction
    net_return_pct / (0.5% × hold_days) × 75

  Pool Depth (10 pts) — practical: can you enter/exit without slippage?
    TVL / $50k × 10

  Data Quality (15 pts) — confidence: how much do we trust the inputs?
    +5  real price-range data  (vs default σ assumption)
    +5  feeApr available       (vs total-APR fallback)
    +5  7-day fee data         (vs day-only, more stable yield estimate)
"""
import math
from typing import Dict, List
from bot.config import config


class PoolAnalyzer:
    def __init__(self):
        self.config = config
        # Optional reference to a SnapshotTracker (kept for monitoring, not scoring)
        self._snapshot_tracker = None

    def set_snapshot_tracker(self, tracker):
        """Attach a SnapshotTracker for monitoring (not used in scoring)."""
        self._snapshot_tracker = tracker

    def calculate_pool_score(self, pool: Dict, _out: Dict = None,
                             position_sol: float = 0,
                             sol_price_usd: float = 0) -> float:
        """
        Calculate a composite score for a pool (0-100).
        Higher score = better LP opportunity.
        
        ⚠️  IMPORTANT: This assumes the pool has already PASSED all safety checks
        (RugCheck, LP burn, liquidity lock) via pool_quality.py. Safety is
        pass/fail, not scored here.
        
        If _out dict provided, component scores are stashed into it.
        When position_sol > 0, slippage estimation is included in the
        prediction's SOL-denominated return.
        """
        score = 0.0

        # ══════════════════════════════════════════════════════════
        # PREDICTED NET RETURN (75 pts)
        # ══════════════════════════════════════════════════════════
        # net_return = daily_yield × hold_days − LVR × hold_days
        # 0.5%/day × hold_days net = full 75 pts.
        prediction = self._predict_position_fees(pool, position_sol, sol_price_usd)
        pct = prediction['net_return_pct']
        hold_days = prediction.get('hold_days', 7)
        full_marks_pct = 0.5 * hold_days  # e.g. 3.5% for 7-day hold
        fee_pred_score = min(75, (pct / full_marks_pct) * 75) if pct > 0 else 0.0
        score += fee_pred_score

        # ══════════════════════════════════════════════════════════
        # POOL DEPTH (10 pts) — can you enter/exit without slippage?
        # ══════════════════════════════════════════════════════════
        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        depth_score = min(10, (tvl / 50_000) * 10) if tvl > 0 else 0.0
        score += depth_score

        # ══════════════════════════════════════════════════════════
        # DATA QUALITY (15 pts) — confidence in the prediction
        # ══════════════════════════════════════════════════════════
        quality_score = 0.0
        if prediction['has_price_data']:
            quality_score += 5   # real σ from price range (vs default)
        if prediction['has_fee_apr']:
            quality_score += 5   # real fee data (not total-APR fallback)
        if prediction['has_week_data']:
            quality_score += 5   # 7d avg APR (more stable than 24h)
        score += quality_score

        if _out is not None:
            _out['predicted_fees'] = fee_pred_score
            _out['depth'] = depth_score
            _out['data_quality'] = quality_score
            _out['prediction'] = prediction   # full prediction details

        return round(score, 2)

    # ------------------------------------------------------------------
    # Predicted fee model
    # ------------------------------------------------------------------

    def _predict_position_fees(self, pool: Dict,
                               position_sol: float = 0,
                               sol_price_usd: float = 0) -> Dict:
        """Predict net return for a multi-day LP position.

        Model
        -----
        hold_days  = MAX_HOLD_TIME_HOURS / 24  (default 7)
        total_yield = daily_yield × hold_days
                      (prefers 7d avg fee APR; falls back to 24h)
        LVR_cost   = σ²/8 × hold_days  (LVR: Milionis et al. 2022)
        net_return = total_yield − LVR_cost

        Volatility σ is estimated via multi-period Parkinson (1980),
        using daily OHLCV candles from GeckoTerminal:
          σ² = (1/n) × Σ (ln(Hᵢ/Lᵢ))² / (4·ln2)
        This averages n independent daily estimates.

        Fallback 1: single-window Parkinson from API aggregates:
          σ_daily = ln(H_Nd / L_Nd) / (2 √(N · ln2))
          Uses 7d range (N=7) or 24h range (N=1).

        Fallback 2: conservative default σ = 15% for meme tokens.

        When position_sol and sol_price_usd are provided, the model also
        estimates round-trip slippage (CPMM swap fee + price impact) and
        returns SOL-denominated P&L:

          net_return_sol = net_return_pct/100 × position_sol − slippage_sol

        Slippage model (per leg = entry or exit):
          swap_fee  = feeRate / 2 × position   (half is swapped)
          impact    = position / (2 × TVL_sol)  (CPMM price impact)
          round_trip = 2 × (swap_fee + impact)

        Returns a dict with full prediction details for transparency.
        """
        hold_days = config.MAX_HOLD_TIME_HOURS / 24
        day = pool.get('day', {})
        week = pool.get('week', {})
        _zero = {
            'hold_days': hold_days,
            'net_return_pct': 0.0, 'daily_fees_pct': 0.0, 'daily_rewards_pct': 0.0,
            'daily_total_pct': 0.0,
            'total_yield_pct': 0.0,
            'sigma_daily': 0.0, 'lvr_daily_pct': 0.0, 'lvr_total_pct': 0.0,
            'lvr_apr': 0.0, 'parkinson_n': 0, 'parkinson_src': 'default',
            'fee_apr': 0.0, 'reward_apr': 0.0, 'total_apr': 0.0,
            'has_price_data': False, 'has_fee_apr': False,
            'has_week_data': False,
            'position_sol': position_sol,
            'roundtrip_slip_pct': 0.0, 'roundtrip_slip_sol': 0.0,
            'gross_return_sol': 0.0, 'net_return_sol': 0.0,
        }

        # --- Daily yield (fees + rewards) ---
        # Prefer 7-day average fee APR over 24h (more stable, less noise).
        # Fall back to day.feeApr only when week data is unavailable.
        week_fee_apr = week.get('feeApr', 0)
        day_fee_apr = day.get('feeApr', 0)
        fee_apr = week_fee_apr if week_fee_apr > 0 else day_fee_apr
        has_fee_apr = fee_apr > 0
        has_week_data = week_fee_apr > 0

        week_total_apr = week.get('apr', 0)
        day_total_apr = day.get('apr', 0) or pool.get('apr24h', 0)
        total_apr = week_total_apr if week_total_apr > 0 else day_total_apr

        if total_apr > 0:
            reward_apr = max(0, total_apr - fee_apr) if fee_apr > 0 else 0
            daily_fees_pct = fee_apr / 365 if fee_apr > 0 else total_apr * 0.7 / 365
            daily_rewards_pct = reward_apr / 365
            daily_total_pct = daily_fees_pct + daily_rewards_pct
        elif fee_apr > 0:
            reward_apr = 0
            daily_fees_pct = fee_apr / 365
            daily_rewards_pct = 0.0
            daily_total_pct = daily_fees_pct
        else:
            return _zero

        # --- Total yield over hold period (linear scaling) ---
        total_yield_pct = daily_total_pct * hold_days

        # --- Volatility & LVR (Loss-Versus-Rebalancing) ---
        # Parkinson (1980) high-low volatility estimator.
        # LVR rate for CPMM (Milionis et al. 2022):
        #   LVR = σ²/8 per unit time (as fraction of position)
        #
        # PREFERRED: Multi-period Parkinson from daily candles
        #   Fetch 7 daily OHLCV candles from GeckoTerminal and compute:
        #     σ² = (1/n) × Σ (ln(Hi/Li))² / (4·ln2)
        #     σ  = √(σ²)
        #   This averages n independent daily estimates, removing the
        #   upward bias of using a single multi-day high-low window
        #   (where H and L likely occur on different days).
        #
        # FALLBACK 1: Single-window Parkinson from API aggregates
        #   σ_daily = ln(H/L) / (2√(N·ln2))
        #   Uses week.priceMin/priceMax (N=7) or day (N=1).
        #
        # FALLBACK 2: Conservative default σ = 15% for meme tokens.

        daily_candles = pool.get('_daily_candles', [])
        week_pmin = week.get('priceMin', 0)
        week_pmax = week.get('priceMax', 0)
        day_pmin = day.get('priceMin', 0)
        day_pmax = day.get('priceMax', 0)
        ln2 = math.log(2)

        sigma_daily = 0.0
        parkinson_n = 0
        parkinson_src = 'default'
        has_price_data = False

        # Try multi-period Parkinson first (daily candles from GeckoTerminal)
        if len(daily_candles) >= 3:
            variances = []
            for high, low in daily_candles:
                if high > 0 and low > 0 and high > low:
                    log_hl = math.log(high / low)
                    variances.append(log_hl ** 2 / (4 * ln2))
            if len(variances) >= 3:
                sigma_daily = math.sqrt(sum(variances) / len(variances))
                parkinson_n = len(variances)
                parkinson_src = 'candles'
                has_price_data = True

        # Fallback: single-window Parkinson from API aggregates
        if not has_price_data:
            if week_pmin and week_pmax and week_pmin > 0 and week_pmax > 0:
                price_min, price_max = week_pmin, week_pmax
                parkinson_n = 7
                has_price_data = True
            elif day_pmin and day_pmax and day_pmin > 0 and day_pmax > 0:
                price_min, price_max = day_pmin, day_pmax
                parkinson_n = 1
                has_price_data = True

            if has_price_data:
                parkinson_src = 'window'
                range_ratio = price_max / price_min
                if range_ratio > 1:
                    log_range = math.log(range_ratio)
                    sigma_daily = log_range / (2 * math.sqrt(parkinson_n * ln2))
                else:
                    sigma_daily = 0.0
            else:
                # No price data — conservative default σ for meme tokens
                sigma_daily = 0.15
                parkinson_n = 0

        # LVR cost: σ²/8 per day (as percentage of position)
        lvr_daily_pct = (sigma_daily ** 2) / 8 * 100
        lvr_total_pct = lvr_daily_pct * hold_days
        lvr_apr = lvr_daily_pct * 365

        # --- Net predicted return (over full hold period) ---
        net_return_pct = total_yield_pct - lvr_total_pct

        # --- Slippage & SOL-denominated P&L (position-specific) ---
        if position_sol > 0 and sol_price_usd > 0:
            tvl_usd = pool.get('tvl', 0) or pool.get('liquidity', 0)
            tvl_sol = tvl_usd / sol_price_usd if sol_price_usd > 0 else 0
            fee_rate = pool.get('feeRate', 0.0025)  # Raydium V4 default

            # CPMM slippage model: each leg swaps half the position
            #   swap_fee  = feeRate/2 of total position (per leg)
            #   impact    = position / (2×tvl_sol)      (per leg)
            #   round_trip = 2 legs
            if tvl_sol > 0:
                price_impact_pct = (position_sol / (2 * tvl_sol)) * 100
            else:
                price_impact_pct = 1.0  # unknown TVL fallback
            swap_fee_pct = fee_rate * 100 / 2  # 0.125% per leg for 0.25% fee
            entry_slip_pct = swap_fee_pct + price_impact_pct
            roundtrip_slip_pct = 2 * entry_slip_pct
            roundtrip_slip_sol = roundtrip_slip_pct / 100 * position_sol

            gross_return_sol = net_return_pct / 100 * position_sol
            net_return_sol = gross_return_sol - roundtrip_slip_sol
        else:
            roundtrip_slip_pct = 0.0
            roundtrip_slip_sol = 0.0
            gross_return_sol = 0.0
            net_return_sol = 0.0

        return {
            'hold_days': hold_days,
            'net_return_pct': round(net_return_pct, 4),
            'daily_fees_pct': round(daily_fees_pct, 4),
            'daily_rewards_pct': round(daily_rewards_pct, 4),
            'daily_total_pct': round(daily_total_pct, 4),
            'total_yield_pct': round(total_yield_pct, 4),
            'sigma_daily': round(sigma_daily, 4),
            'lvr_daily_pct': round(lvr_daily_pct, 4),
            'lvr_total_pct': round(lvr_total_pct, 4),
            'lvr_apr': round(lvr_apr, 1),
            'parkinson_n': parkinson_n,
            'parkinson_src': parkinson_src,
            'fee_apr': round(fee_apr, 1),
            'reward_apr': round(reward_apr, 1),
            'total_apr': round(total_apr, 1),
            'has_price_data': has_price_data,
            'has_fee_apr': has_fee_apr,
            'has_week_data': has_week_data,
            'position_sol': round(position_sol, 4),
            'roundtrip_slip_pct': round(roundtrip_slip_pct, 4),
            'roundtrip_slip_sol': round(roundtrip_slip_sol, 6),
            'gross_return_sol': round(gross_return_sol, 6),
            'net_return_sol': round(net_return_sol, 6),
        }

    # ------------------------------------------------------------------
    # Fee consistency: day vs week fee stability
    # ------------------------------------------------------------------

    def _calculate_fee_consistency(self, day: Dict, week: Dict, fee_24h: float) -> float:
        """Score fee consistency by comparing today to weekly average (0-15 pts).

        Stable fees = sustainable LP income. Volatile fees suggest bot activity,
        wash trading, or short-term hype.
        """
        score = 0.0

        day_fee = day.get('volumeFee', 0) or fee_24h
        week_fee = week.get('volumeFee', 0)
        week_avg_fee = week_fee / 7 if week_fee > 0 else 0

        if week_avg_fee > 0 and day_fee > 0:
            fee_ratio = day_fee / week_avg_fee
            # Ideal: fee ratio near 1.0 (consistent)
            if 0.8 <= fee_ratio <= 1.2:
                score = 15.0  # Very consistent
            elif 0.5 <= fee_ratio <= 2.0:
                # Moderately consistent
                deviation = abs(fee_ratio - 1.0)
                score = max(0, 15.0 - deviation * 12)
            else:
                # Highly volatile fees
                score = 3.0
        elif day_fee > 0 and week_avg_fee == 0:
            # Brand new pool generating fees — partial credit
            score = 7.0

        return round(min(15.0, score), 2)

    # ------------------------------------------------------------------
    # IL safety: use actual price range data from the API (0-15 pts)
    # ------------------------------------------------------------------

    def _estimate_il_safety(self, pool: Dict) -> float:
        """Estimate IL safety score (0-25 pts).

        Uses day.priceMin / day.priceMax from the V3 API to measure the
        actual 24h price range. Tighter range = less IL = better for LPs.

        Falls back to name-based heuristic if price range data is missing.
        """
        day = pool.get('day', {})
        price_min = day.get('priceMin', 0)
        price_max = day.get('priceMax', 0)

        # Try to use real price range data
        if price_min and price_max and price_min > 0 and price_max > 0:
            range_ratio = price_max / price_min
            if range_ratio <= 1.05:
                return 25.0   # < 5% range, negligible IL
            elif range_ratio <= 1.10:
                return 20.0   # 5-10% range
            elif range_ratio <= 1.15:
                return 16.0   # 10-15% range
            elif range_ratio <= 1.25:
                return 11.0   # 15-25% range
            elif range_ratio <= 1.50:
                return 6.0    # 25-50% range
            elif range_ratio <= 2.0:
                return 2.0    # 50-100% range, rough
            else:
                return 0.0    # > 100% range, extreme IL

        # Fallback: name-based heuristic (no price data available)
        name = pool.get('name', '').upper()
        if any(pair in name for pair in ['USDC/USDT', 'USDT/USDC']):
            return 25.0
        if ('SOL' in name or 'WSOL' in name) and any(s in name for s in ['USDC', 'USDT']):
            return 15.0
        # Unknown meme pairs — assume moderate IL risk
        return 6.0

    def rank_pools(self, pools: List[Dict], top_n: int = 10,
                   position_sol: float = 0,
                   sol_price_usd: float = 0) -> List[Dict]:
        """Score, gate, and rank pools by predicted net return.

        Gates:
        1. Predicted net APR >= MIN_PREDICTED_NET_APR (pool quality)
        2. If position_sol given, net_return_sol > 0 (trade profitability
           after round-trip slippage)

        Remaining pools are sorted by predicted net return (descending).
        """
        scored = []
        min_net_apr = config.MIN_PREDICTED_NET_APR
        for pool in pools:
            copy = pool.copy()
            components = {}
            copy['score'] = self.calculate_pool_score(
                pool, _out=components,
                position_sol=position_sol, sol_price_usd=sol_price_usd)
            copy['_predicted_fees'] = components.get('predicted_fees', 0)
            copy['_depth'] = components.get('depth', 0)
            copy['_data_quality'] = components.get('data_quality', 0)
            # Expose prediction details for display
            pred = components.get('prediction', {})
            net_pct = pred.get('net_return_pct', 0)
            hold_days = pred.get('hold_days', 7)
            copy['_pred_net_pct'] = net_pct
            copy['_pred_yield_pct'] = pred.get('total_yield_pct', 0)
            copy['_pred_lvr_pct'] = pred.get('lvr_total_pct', 0)
            copy['_pred_lvr_apr'] = pred.get('lvr_apr', 0)
            copy['_pred_sigma'] = pred.get('sigma_daily', 0)
            copy['_pred_net_apr'] = round(net_pct / hold_days * 365, 1)
            copy['_pred_parkinson_n'] = pred.get('parkinson_n', 0)
            copy['_pred_parkinson_src'] = pred.get('parkinson_src', 'default')
            copy['_pred_slip_pct'] = pred.get('roundtrip_slip_pct', 0)
            copy['_pred_slip_sol'] = pred.get('roundtrip_slip_sol', 0)
            copy['_pred_gross_sol'] = pred.get('gross_return_sol', 0)
            copy['_pred_net_sol'] = pred.get('net_return_sol', 0)
            copy['_pred_pos_sol'] = pred.get('position_sol', 0)
            copy['_pred_hold_days'] = hold_days
            # Gate 1: pool quality — reject below minimum predicted net APR
            net_apr = net_pct / hold_days * 365
            if net_apr < min_net_apr:
                continue
            # Gate 2: trade profitability — if position known, must be net positive
            if position_sol > 0 and pred.get('net_return_sol', 0) <= 0:
                continue
            scored.append(copy)
        return sorted(scored, key=lambda x: x['_pred_net_pct'], reverse=True)[:top_n]

    def calculate_position_size(
        self,
        pool: Dict,
        available_capital: float,
        num_open_positions: int = 0,
    ) -> float:
        """
        Calculate optimal position size in SOL.

        Split available capital evenly across remaining slots after
        keeping a small fixed reserve for transaction fees.

        Rules:
        1. Reserve = RESERVE_SOL (fixed, e.g. 0.05 SOL)
        2. Size = deployable / positions_remaining
        3. Never exceed MAX_ABSOLUTE_POSITION_SOL
        """
        positions_remaining = config.MAX_CONCURRENT_POSITIONS - num_open_positions
        if positions_remaining <= 0:
            return 0.0

        deployable = available_capital - config.RESERVE_SOL
        if deployable <= 0:
            return 0.0

        # Equal split across remaining slots
        size = deployable / positions_remaining

        size = min(size, config.MAX_ABSOLUTE_POSITION_SOL)

        return size

    @staticmethod
    def calculate_impermanent_loss(
        entry_price_ratio: float,
        current_price_ratio: float,
    ) -> float:
        """
        Standard impermanent loss (divergence loss) for a 50/50 CPMM.

        Returns the fractional loss of LP value relative to holding:
            IL = 2*sqrt(k) / (1+k) - 1

        where k = current_price / entry_price.

        Properties:
        - Always <= 0 (LP never outperforms HODL, ignoring fees)
        - Symmetric: IL(2x) == IL(0.5x) == -5.72%
        - Denomination-independent (same whether measured in SOL, USD, etc.)

        Reference: Pintail "Uniswap: A Good Deal for Liquidity Providers?"
        """
        if entry_price_ratio <= 0 or current_price_ratio <= 0:
            return 0.0

        k = current_price_ratio / entry_price_ratio

        return 2 * math.sqrt(k) / (1 + k) - 1
