"""
RugCheck API Integration
Verifies token safety via RugCheck.xyz

IMPORTANT: RugCheck scoring:
- score: raw risk score, HIGHER = MORE RISKY (USDC=1, stSOL=50601)
- score_normalised: 0-100, LOWER = SAFER
- risks[]: array of {name, value, description, score, level} items
  - level: "danger", "warn", "info", "good"
- Top-level freezeAuthority/mintAuthority can be None even when
  the risks array reports the authority exists - always parse risks[].
"""
from typing import Dict, Optional, List
import requests
import time


class RugCheckAPI:
    """Integration with RugCheck.xyz API for token safety verification."""

    BASE_URL = "https://api.rugcheck.xyz/v1"

    def __init__(self):
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = 300  # 5 minutes

    def get_token_report(self, mint_address: str) -> Optional[Dict]:
        """Get full RugCheck report for a token, with caching."""
        if mint_address in self._cache:
            cached_data, cached_time = self._cache[mint_address]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data

        try:
            url = f"{self.BASE_URL}/tokens/{mint_address}/report"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                self._cache[mint_address] = (data, time.time())
                return data
            elif response.status_code == 404:
                return None
            else:
                print(f"RugCheck API error {response.status_code}: {mint_address}")
                return None

        except requests.RequestException as e:
            print(f"Error fetching RugCheck data: {e}")
            return None

    def analyze_token_safety(self, mint_address: str) -> Dict:
        """
        Analyze token safety using RugCheck data.

        Returns a dict with:
        - available: bool
        - risk_score: int (0-100 normalised, lower=safer)
        - risk_level: 'low'/'medium'/'high'/'unknown'
        - is_rugged: bool
        - dangers: list of danger-level risk descriptions
        - warnings: list of warn-level risk descriptions
        - has_freeze_authority: bool (from risks array, not top-level field)
        - has_mint_authority: bool (from risks array, not top-level field)
        - top5_holder_pct: float (top 5 holders %)
        - top10_holder_pct: float (top 10 holders %)
        - max_single_holder_pct: float (largest single holder %)
        - total_holders: int
        """
        report = self.get_token_report(mint_address)

        if not report:
            return {
                'available': False,
                'risk_score': 100,
                'risk_level': 'unknown',
                'is_rugged': None,
                'dangers': [],
                'warnings': [],
                'has_freeze_authority': None,
                'has_mint_authority': None,
                'top_holder_concentration': None,
                'total_holders': 0,
            }

        # Use normalised score (0-100, lower = safer)
        risk_score = report.get('score_normalised', 100)
        rugged = report.get('rugged', False)

        # Parse risks array for actual risk items
        risks: List[Dict] = report.get('risks') or []
        dangers = []
        warnings = []
        has_freeze = False
        has_mint = False

        for risk in risks:
            level = risk.get('level', '')
            name = risk.get('name', '')
            description = risk.get('description', '')
            display = f"{name}: {description}" if description else name

            if 'freeze' in name.lower():
                has_freeze = True
            if 'mint' in name.lower() and 'authority' in name.lower():
                has_mint = True

            if level == 'danger':
                dangers.append(display)
            elif level == 'warn':
                warnings.append(display)

        # Determine risk level from normalised score
        if risk_score <= 10:
            risk_level = 'low'
        elif risk_score <= 40:
            risk_level = 'medium'
        else:
            risk_level = 'high'

        # Top holder concentration (top 5, top 10, and largest single)
        top_holders = report.get('topHolders', [])
        top5_pct = 0.0
        top10_pct = 0.0
        max_single_pct = 0.0
        if top_holders:
            top5_pct = sum(h.get('pct', 0) for h in top_holders[:5])
            top10_pct = sum(h.get('pct', 0) for h in top_holders[:10])
            max_single_pct = max((h.get('pct', 0) for h in top_holders), default=0.0)

        return {
            'available': True,
            'risk_score': risk_score,
            'risk_level': risk_level,
            'is_rugged': rugged,
            'dangers': dangers,
            'warnings': warnings,
            'has_freeze_authority': has_freeze,
            'has_mint_authority': has_mint,
            'top5_holder_pct': top5_pct,
            'top10_holder_pct': top10_pct,
            'max_single_holder_pct': max_single_pct,
            'total_holders': report.get('totalHolders', 0),
        }
