"""
Pool Quality Analyzer

Combines V3 API data (burnPercent, TVL, APR) with RugCheck token safety
to assess pool quality and filter out risky pools.

Hard-reject criteria (any = pool rejected):
- Any danger-level RugCheck risk item
- RugCheck risk score > MAX_RUGCHECK_SCORE (default 50)
- Token marked as rugged
- Top 10 holders own > MAX_TOP10_HOLDER_PERCENT (default 45%)
- Single holder owns > MAX_SINGLE_HOLDER_PERCENT (default 15%)
- Fewer than MIN_TOKEN_HOLDERS holders (default 100)
- Token has mutable metadata (owner can change name/symbol)
- Very few LP providers (liquidity can be pulled)
- Token has freeze or mint authority
- LP burn < 50%
- Low TVL + extreme APR (rug pull pattern)
- On-chain LP lock < MIN_SAFE_LP_PERCENT (default 90%)
- Single wallet holds > MAX_SINGLE_LP_HOLDER_PERCENT (default 25%) of unlocked LP
"""
from typing import Dict, List
from bot.config import config
from bot.safety.rugcheck import RugCheckAPI
from bot.safety.liquidity_lock import LiquidityLockAnalyzer
from bot.raydium_client import WSOL_MINT


class PoolQualityAnalyzer:
    """Analyzes pool quality using V3 API data and RugCheck."""

    def __init__(self):
        self.rugcheck = RugCheckAPI()
        self.lp_lock = LiquidityLockAnalyzer()

    def analyze_pool(self, pool: Dict, check_safety: bool = True) -> Dict:
        """
        Analyze a single pool for quality and risks.

        Uses:
        - V3 API burnPercent for LP burn status
        - RugCheck API for token safety (strict mode)
        - Pool metrics (TVL, APR, volume) for quality assessment
        """
        risks = []
        warnings = []

        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        day = pool.get('day', {})
        apr = day.get('apr', 0) or pool.get('apr24h', 0)
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        burn_percent = pool.get('burnPercent', 0)

        # --- LP Burn Check (from V3 API - real data!) ---
        if burn_percent < config.MIN_BURN_PERCENT:
            risks.append(f"Low LP burn ({burn_percent:.1f}%, min: {config.MIN_BURN_PERCENT}%) - rug pull risk")
        elif burn_percent < 80:
            warnings.append(f"Moderate LP burn ({burn_percent:.1f}%)")

        # --- APR Sanity ---
        if apr > 1000:
            risks.append(f"Extreme APR ({apr:.1f}%) - likely fake/manipulated")
        elif apr > 200:
            warnings.append(f"Very high APR ({apr:.1f}%) - high volatility expected")

        # --- Liquidity Check ---
        if tvl < 50_000:
            warnings.append(f"Low liquidity (${tvl:,.0f}) - high slippage risk")

        # --- Volume/TVL Ratio ---
        # High vol/TVL is actually GOOD for LPs (more fees per unit capital).
        # Only flag as extreme wash-trading concern above 10x.
        vol_tvl = volume / tvl if tvl > 0 else 0
        if vol_tvl > 10:
            warnings.append(f"Extreme volume/TVL ({vol_tvl:.1f}x) - possible wash trading")

        # --- Rug Pull Pattern ---
        if tvl < 5_000 and apr > 500:
            risks.append("Very low liquidity + extreme APR = likely rug pull")

        # --- RugCheck Token Safety (STRICT) ---
        rugcheck_result = None
        if check_safety:
            # Check the non-WSOL token
            base_mint = pool.get('baseMint', '')
            quote_mint = pool.get('quoteMint', '')
            token_to_check = base_mint if quote_mint == WSOL_MINT else quote_mint

            if token_to_check and token_to_check != WSOL_MINT:
                rugcheck_result = self.rugcheck.analyze_token_safety(token_to_check)

                if rugcheck_result.get('available'):
                    # --- Hard rejection: rugged token ---
                    if rugcheck_result.get('is_rugged'):
                        risks.append("🚨 RUG PULL DETECTED by RugCheck")

                    # --- Hard rejection: high risk score ---
                    risk_score = rugcheck_result.get('risk_score', 100)
                    if risk_score > config.MAX_RUGCHECK_SCORE:
                        risks.append(f"High RugCheck risk score ({risk_score}/100, max allowed: {config.MAX_RUGCHECK_SCORE})")
                    elif risk_score > 30:
                        warnings.append(f"Moderate RugCheck risk score ({risk_score}/100)")

                    # --- Hard rejection: ALL danger-level items ---
                    for danger in rugcheck_result.get('dangers', []):
                        risks.append(f"RugCheck DANGER: {danger}")

                    # --- Warnings from RugCheck ---
                    for warn in rugcheck_result.get('warnings', []):
                        warnings.append(f"RugCheck WARNING: {warn}")

                    # --- Hard rejection: freeze / mint authority ---
                    if rugcheck_result.get('has_freeze_authority'):
                        risks.append("Token has freeze authority - can freeze your tokens")
                    if rugcheck_result.get('has_mint_authority'):
                        risks.append("Token has mint authority - unlimited supply possible")

                    # --- Hard rejection: mutable metadata ---
                    if rugcheck_result.get('has_mutable_metadata'):
                        risks.append("Token has mutable metadata - owner can change name/symbol")

                    # --- Hard rejection: low LP providers ---
                    if rugcheck_result.get('low_lp_providers'):
                        risks.append("Very few LP providers - liquidity can be pulled easily")

                    # --- Hard rejection: top 10 holder concentration ---
                    top10_pct = rugcheck_result.get('top10_holder_pct', 0)
                    if top10_pct > config.MAX_TOP10_HOLDER_PERCENT:
                        risks.append(
                            f"Top 10 holders own {top10_pct:.1f}% "
                            f"(max allowed: {config.MAX_TOP10_HOLDER_PERCENT}%)"
                        )
                    elif top10_pct > 30:
                        warnings.append(f"Top 10 holders own {top10_pct:.1f}%")

                    # --- Hard rejection: single whale holder ---
                    max_single = rugcheck_result.get('max_single_holder_pct', 0)
                    if max_single > config.MAX_SINGLE_HOLDER_PERCENT:
                        risks.append(
                            f"Single holder owns {max_single:.1f}% "
                            f"(max allowed: {config.MAX_SINGLE_HOLDER_PERCENT}%)"
                        )
                    elif max_single > 10:
                        warnings.append(f"Largest single holder owns {max_single:.1f}%")

                    # --- Low total holders = illiquid / early token ---
                    total_holders = rugcheck_result.get('total_holders', 0)
                    if total_holders < config.MIN_TOKEN_HOLDERS:
                        risks.append(f"Very few holders ({total_holders}, min: {config.MIN_TOKEN_HOLDERS}) - illiquid token")
                    elif total_holders < 500:
                        warnings.append(f"Low holder count ({total_holders})")

                else:
                    warnings.append("RugCheck data unavailable for this token")

        # --- Short-circuit: skip expensive LP lock RPC calls if already rejected ---
        if risks:
            return {
                'risk_level': 'HIGH',
                'risks': risks,
                'warnings': warnings,
                'is_safe': False,
                'burn_percent': burn_percent,
                'liquidity_tier': 'high' if tvl > 100_000 else 'medium' if tvl > 50_000 else 'low',
                'rugcheck': rugcheck_result,
                'lp_lock': None,
            }

        # --- On-chain LP Lock Analysis ---
        # Raydium's "Burn & Earn" uses SPL Token `burn`, which REDUCES total
        # supply — burned tokens cease to exist.  They are NOT sent to dead
        # addresses, so they're invisible to on-chain holder queries.
        #
        # burnPercent (from Raydium V3 API) = what % of initial LP was
        #   destroyed via SPL Token burn instruction.
        # On-chain LP lock (from liquidity_lock.py) = of the CIRCULATING
        #   (post-burn) LP tokens, who controls them?  All percentages from
        #   that module are relative to circulating supply.
        #
        # To convert on-chain %-of-circulating into %-of-total-initial:
        #   remaining_frac = (100 - burnPercent) / 100
        #   on_chain_pct_of_total = on_chain_pct × remaining_frac
        #
        # Combined risk formula:
        #   effective_safe = burnPercent + safe_pct_of_circulating × remaining_frac
        #   max_pullable   = max_single_unlocked_of_circulating × remaining_frac
        #
        # Example: burnPercent=99%, top holder has 50% of circulating LP
        #   -> they can pull 50% × 1% = 0.5% of total liquidity (negligible)
        # Example: burnPercent=50%, top holder has 60% of circulating LP
        #   -> they can pull 60% × 50% = 30% of total liquidity (dangerous)
        lp_lock_result = None
        if check_safety and config.CHECK_LP_LOCK:
            lp_mint_info = pool.get('lpMint', {})
            lp_mint_addr = lp_mint_info.get('address', '') if isinstance(lp_mint_info, dict) else ''
            if lp_mint_addr:
                lp_lock_result = self.lp_lock.analyze_lp_lock(lp_mint_addr)
                if lp_lock_result.get('available'):
                    # Convert on-chain %-of-circulating to %-of-total-initial
                    remaining_frac = (100 - burn_percent) / 100  # fraction of initial LP still circulating
                    max_single_unlocked = lp_lock_result.get('max_single_unlocked_pct', 0)  # % of circulating
                    safe_pct = lp_lock_result.get('safe_pct', 0)  # % of circulating locked on-chain

                    # What % of TOTAL initial pool liquidity can the biggest holder pull?
                    max_pullable_pct = max_single_unlocked * remaining_frac

                    # What % of TOTAL initial pool liquidity is in unlocked wallets?
                    total_unlocked_pct = lp_lock_result.get('unlocked_pct', 0) * remaining_frac

                    if max_pullable_pct > config.MAX_SINGLE_LP_HOLDER_PERCENT:
                        risks.append(
                            f"LP whale can pull {max_pullable_pct:.1f}% of pool liquidity "
                            f"({max_single_unlocked:.0f}% of circulating LP × "
                            f"{remaining_frac*100:.1f}% still circulating, "
                            f"max allowed: {config.MAX_SINGLE_LP_HOLDER_PERCENT}%)"
                        )

                    # Overall safety: burned% + locked% of remaining
                    effective_safe_pct = burn_percent + safe_pct * remaining_frac
                    if effective_safe_pct < config.MIN_SAFE_LP_PERCENT:
                        risks.append(
                            f"Only {effective_safe_pct:.1f}% of total LP is safe "
                            f"(burned={burn_percent:.0f}% + locked={safe_pct*remaining_frac:.1f}%, "
                            f"min required: {config.MIN_SAFE_LP_PERCENT}%)"
                        )
                    elif effective_safe_pct < 90:
                        warnings.append(
                            f"LP safety {effective_safe_pct:.1f}% "
                            f"(burned={burn_percent:.0f}% + locked={safe_pct*remaining_frac:.1f}%)"
                        )
                else:
                    warnings.append("On-chain LP lock data unavailable")

        # Determine overall risk level
        if risks:
            risk_level = "HIGH"
        elif warnings:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        return {
            'risk_level': risk_level,
            'risks': risks,
            'warnings': warnings,
            'is_safe': len(risks) == 0,
            'burn_percent': burn_percent,
            'liquidity_tier': 'high' if tvl > 100_000 else 'medium' if tvl > 50_000 else 'low',
            'rugcheck': rugcheck_result,
            'lp_lock': lp_lock_result,
        }

    @staticmethod
    def get_safe_pools(pools: List[Dict], check_locks: bool = False,
                       analyzer: 'PoolQualityAnalyzer' = None) -> List[Dict]:
        """
        Filter pools to only safe ones.

        Args:
            pools: List of V3 pool dicts
            check_locks: If True, also check RugCheck token safety
            analyzer: Optional persistent instance (reuses caches across scans)
        """
        if analyzer is None:
            analyzer = PoolQualityAnalyzer()
        safe_pools = []

        for pool in pools:
            analysis = analyzer.analyze_pool(pool, check_safety=check_locks)
            if analysis['is_safe']:
                safe_pools.append(pool)

        return safe_pools
