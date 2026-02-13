"""
Pool Quality Analyzer

Combines V3 API data (burnPercent, TVL, APR) with RugCheck token safety
to assess pool quality and filter out risky pools.

Hard-reject criteria (any = pool rejected):
- Any danger-level RugCheck risk item
- Top 10 holders own > MAX_TOP10_HOLDER_PERCENT (default 50%)
- Single holder owns > MAX_SINGLE_HOLDER_PERCENT (default 20%)
- RugCheck risk score > MAX_RUGCHECK_SCORE (default 60)
- Token marked as rugged
- LP burn < 50%
- Low TVL + extreme APR (rug pull pattern)
"""
from typing import Dict, List
from bot.config import config
from bot.safety.rugcheck import RugCheckAPI
from bot.raydium_client import WSOL_MINT

# RugCheck danger risk names that are automatic hard rejections.
# Any danger-level item rejects the pool, but these are the ones
# we specifically look for and tag even outside the danger level.
CRITICAL_RISK_PATTERNS = [
    'top 10 holders',       # Top 10 holders high ownership
    'single holder',        # Single holder owns large amount
    'freeze authority',     # Token can be frozen
    'mint authority',       # Infinite minting possible
    'copycat',              # Copycat / impersonation token
    'low liquidity',        # Low liquidity flagged by RugCheck
    'rug pull',             # Explicit rug pull flag
]


class PoolQualityAnalyzer:
    """Analyzes pool quality using V3 API data and RugCheck."""

    def __init__(self):
        self.rugcheck = RugCheckAPI()

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
        if burn_percent < 50:
            risks.append(f"Low LP burn ({burn_percent:.1f}%) - rug pull risk")
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
                    if total_holders < 50:
                        risks.append(f"Very few holders ({total_holders}) - illiquid token")
                    elif total_holders < 200:
                        warnings.append(f"Low holder count ({total_holders})")

                else:
                    warnings.append("RugCheck data unavailable for this token")

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
        }

    @staticmethod
    def get_safe_pools(pools: List[Dict], check_locks: bool = False) -> List[Dict]:
        """
        Filter pools to only safe ones.

        Args:
            pools: List of V3 pool dicts
            check_locks: If True, also check RugCheck token safety
        """
        analyzer = PoolQualityAnalyzer()
        safe_pools = []

        for pool in pools:
            analysis = analyzer.analyze_pool(pool, check_safety=check_locks)
            if analysis['is_safe']:
                safe_pools.append(pool)

        return safe_pools
