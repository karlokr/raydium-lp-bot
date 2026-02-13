"""
RugCheck API Integration
Verifies token safety via RugCheck.xyz
"""
from typing import Dict, Optional
import requests
import time

from bot.config import config


class RugCheckAPI:
    """
    Integration with RugCheck.xyz API for token safety verification.
    
    RugCheck provides:
    - Liquidity lock status
    - Rug pull risk scoring
    - Top holder analysis
    - Market liquidity info
    """
    
    BASE_URL = "https://api.rugcheck.xyz/v1"
    
    def __init__(self):
        self._cache = {}
        self._cache_ttl = 300  # 5 minutes
    
    def get_token_report(self, mint_address: str) -> Optional[Dict]:
        """
        Get full RugCheck report for a token.
        """
        cache_key = mint_address
        if cache_key in self._cache:
            cached_data, cached_time = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data
        
        try:
            url = f"{self.BASE_URL}/tokens/{mint_address}/report"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                self._cache[cache_key] = (data, time.time())
                return data
            elif response.status_code == 404:
                print(f"Token not found in RugCheck: {mint_address}")
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
        """
        report = self.get_token_report(mint_address)
        
        if not report:
            return {
                'available': False,
                'score': 0,
                'risk_level': 'unknown',
                'is_rugged': None,
                'has_freeze_authority': None,
                'has_mint_authority': None,
                'top_holder_concentration': None,
                'message': 'RugCheck data unavailable'
            }
        
        score = report.get('score', 0)
        rugged = report.get('rugged', False)
        freeze_auth = report.get('freezeAuthority') is not None
        mint_auth = report.get('mintAuthority') is not None
        
        if score >= 800:
            risk_level = 'low'
        elif score >= 500:
            risk_level = 'medium'
        else:
            risk_level = 'high'
        
        top_holders = report.get('topHolders', [])
        if top_holders and len(top_holders) > 0:
            top_holder_pct = sum(h.get('pct', 0) for h in top_holders[:5])
        else:
            top_holder_pct = None
        
        return {
            'available': True,
            'score': score,
            'risk_level': risk_level,
            'is_rugged': rugged,
            'has_freeze_authority': freeze_auth,
            'has_mint_authority': mint_auth,
            'top_holder_concentration': top_holder_pct,
            'total_holders': report.get('totalHolders', 0),
            'message': f'RugCheck score: {score}/1000'
        }
