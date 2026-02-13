"""
Liquidity Lock Checker
Verifies if LP tokens are locked to prevent rug pulls
Uses RugCheck API for verification
"""
from typing import Dict, Optional
import requests
import time

from bot.config import config
from bot.safety.rugcheck import RugCheckAPI


class LiquidityLockChecker:
    """
    Checks if pool liquidity is locked/burned.
    Uses RugCheck API for base token verification.
    """
    
    BURN_ADDRESS = "11111111111111111111111111111111"
    
    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or config.RPC_ENDPOINT
        self.rugcheck = RugCheckAPI()
        self._cache = {}
    
    def get_lp_lock_status(self, pool: Dict) -> Dict:
        """
        Check token safety using RugCheck.
        
        NOTE: This checks the BASE TOKEN safety, NOT LP lock status.
        """
        base_mint = pool.get('baseMint')
        
        if not base_mint:
            return self._default_lock_status()
        
        if base_mint in self._cache:
            return self._cache[base_mint]
        
        analysis = self.rugcheck.analyze_token_safety(base_mint)
        
        if not analysis['available']:
            return {
                'locked_percent': 0.0,
                'burned_percent': 0.0,
                'total_secured_percent': 0.0,
                'is_locked': False,
                'locked_until': None,
                'risk_level': 'unknown',
                'total_supply': 0.0,
                'rugcheck_available': False,
            }
        
        result = {
            'locked_percent': 0.0,
            'burned_percent': 0.0,
            'total_secured_percent': 0.0,
            'is_locked': False,
            'locked_until': None,
            'risk_level': analysis['risk_level'],
            'total_supply': 0.0,
            'rugcheck_score': analysis['score'],
            'is_rugged': analysis['is_rugged'],
            'has_freeze_authority': analysis['has_freeze_authority'],
            'has_mint_authority': analysis['has_mint_authority'],
            'top_holder_concentration': analysis['top_holder_concentration'],
            'total_holders': analysis['total_holders'],
            'rugcheck_available': True,
        }
        
        self._cache[base_mint] = result
        return result
    
    def _get_burned_lp_amount(self, lp_mint: str) -> float:
        """Get amount of LP tokens sent to burn address (not implemented)"""
        return 0.0
    
    def _get_locked_lp_amount(self, lp_mint: str) -> float:
        """Get amount of LP tokens locked in contracts (not implemented)"""
        return 0.0
    
    def _default_lock_status(self) -> Dict:
        """Return default status when check fails"""
        return {
            'locked_percent': 0.0,
            'burned_percent': 0.0,
            'total_secured_percent': 0.0,
            'is_locked': False,
            'locked_until': None,
            'risk_level': 'unknown',
            'total_supply': 0.0,
            'rugcheck_available': False,
        }


def check_pool_lock_safety(pool: Dict) -> str:
    """
    Quick check for token safety using RugCheck.
    
    NOTE: This checks TOKEN safety, NOT LP lock status!
    """
    checker = LiquidityLockChecker()
    lock_status = checker.get_lp_lock_status(pool)
    
    if lock_status.get('rugcheck_available'):
        score = lock_status.get('rugcheck_score', 0)
        risk = lock_status['risk_level']
        is_rugged = lock_status.get('is_rugged', False)
        
        if is_rugged:
            return "🚨 RUG PULL DETECTED by RugCheck - DO NOT ENTER!"
        
        freeze_auth = lock_status.get('has_freeze_authority', False)
        mint_auth = lock_status.get('has_mint_authority', False)
        
        msg = f"Token Safety (RugCheck): {score}/1000 (Risk: {risk.upper()})\n"
        
        if freeze_auth:
            msg += "   ⚠️  Token has freeze authority - can freeze transfers!\n"
        if mint_auth:
            msg += "   ⚠️  Token has mint authority - can print more tokens!\n"
        
        holders = lock_status.get('total_holders', 0)
        top_holder_pct = lock_status.get('top_holder_concentration')
        
        if holders:
            msg += f"   Holders: {holders:,}\n"
        if top_holder_pct:
            msg += f"   Top 5 holders: {top_holder_pct:.1f}%"
            if top_holder_pct > 50:
                msg += " ⚠️  High concentration!"
            msg += "\n"
        
        msg += "   ⚠️  LP lock NOT verified - check manually on Solscan!"
        
        return msg.strip()
    
    return ("⚠️  Token safety UNKNOWN (RugCheck unavailable)\n"
            "   Verify manually at https://rugcheck.xyz/\n"
            "   Also check LP locks on Solscan!")


if __name__ == "__main__":
    from bot.raydium_client import RaydiumAPIClient
    
    client = RaydiumAPIClient()
    checker = LiquidityLockChecker()
    
    pools = client.get_filtered_pools(min_liquidity=10_000, min_apr=5.0)[:3]
    
    print("Token Safety Check (via RugCheck)")
    print("=" * 80)
    print("NOTE: This checks TOKEN safety, NOT LP lock status!")
    print("Always verify LP locks manually on Solscan or similar tools.")
    print("=" * 80)
    
    for pool in pools:
        print(f"\nPool: {pool['name']}")
        print(f"TVL: ${pool['liquidity']:,.2f}")
        
        status = checker.get_lp_lock_status(pool)
        
        if status.get('rugcheck_available'):
            print(f"Token Safety (RugCheck):")
            print(f"  Score: {status['rugcheck_score']}/1000")
            print(f"  Risk Level: {status['risk_level'].upper()}")
            if status.get('is_rugged'):
                print(f"  🚨 RUG PULL DETECTED!")
            if status.get('has_freeze_authority'):
                print(f"  ⚠️  Has freeze authority")
            if status.get('has_mint_authority'):
                print(f"  ⚠️  Has mint authority")
            if status.get('total_holders'):
                print(f"  Holders: {status['total_holders']:,}")
        else:
            print(f"Token Safety: UNKNOWN (RugCheck unavailable)")
        
        print("\n" + check_pool_lock_safety(pool))
        print("-" * 80)
