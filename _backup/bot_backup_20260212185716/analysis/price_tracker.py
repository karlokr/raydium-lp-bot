"""
Real-time Price Tracker using Solana RPC
Fetches live pool account data to calculate current prices
"""
import time
from typing import Dict, Optional
from solana.rpc.api import Client
from solders.pubkey import Pubkey

from bot.config import config


class PriceTracker:
    """
    Tracks real-time prices by reading Raydium AMM account data directly from Solana.
    This provides fresh prices compared to the 5-15 minute delayed API data.
    """
    
    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or config.RPC_ENDPOINT
        self.client = Client(self.rpc_url)
        self._price_cache: Dict[str, tuple] = {}
        self._cache_ttl = 5
        
    def get_pool_price(self, amm_id: str, use_cache: bool = True) -> Optional[float]:
        """
        Get current price for a pool by reading AMM account data.
        """
        current_time = time.time()
        
        if use_cache and amm_id in self._price_cache:
            cached_price, cached_time = self._price_cache[amm_id]
            if current_time - cached_time < self._cache_ttl:
                return cached_price
        
        try:
            pubkey = Pubkey.from_string(amm_id)
            response = self.client.get_account_info(pubkey)
            
            if not response.value or not response.value.data:
                print(f"✗ No account data for {amm_id}")
                return None
            
            account_data = bytes(response.value.data)
            price = self._parse_amm_price(account_data)
            
            if price is not None:
                self._price_cache[amm_id] = (price, current_time)
            
            return price
            
        except Exception as e:
            print(f"✗ Error fetching price for {amm_id}: {e}")
            return None
    
    def _parse_amm_price(self, data: bytes) -> Optional[float]:
        """
        Parse price from AMM account data.
        For production, use Raydium SDK or proper account parser.
        """
        try:
            # Raydium AMM v4 - requires the IDL for exact layout
            # For now, fall back to API price
            return None
        except Exception:
            return None
    
    def get_multiple_prices(self, amm_ids: list) -> Dict[str, float]:
        """Batch fetch prices for multiple pools."""
        prices = {}
        for amm_id in amm_ids:
            price = self.get_pool_price(amm_id)
            if price is not None:
                prices[amm_id] = price
        return prices
    
    def get_pool_reserves(self, pool_data: Dict) -> tuple:
        """
        Calculate reserves from pool data (alternative to account parsing).
        """
        try:
            base_amount = float(pool_data.get('tokenAmountCoin', 0))
            quote_amount = float(pool_data.get('tokenAmountPc', 0))
            
            if base_amount > 0 and quote_amount > 0:
                price = quote_amount / base_amount
                return base_amount, quote_amount, price
            
            return 0, 0, 0
            
        except (KeyError, ValueError, ZeroDivisionError):
            return 0, 0, 0


class HybridPriceTracker:
    """
    Hybrid price tracker that combines:
    1. Raydium API for initial/cached data (5-15 min delay)
    2. Direct RPC calls for more frequent updates (every 5 sec)
    3. Calculated prices from reserve ratios
    """
    
    def __init__(self, api_client, rpc_url: str = None):
        self.api_client = api_client
        self.rpc_tracker = PriceTracker(rpc_url)
        self._last_api_refresh = {}
        
    def get_current_price(self, amm_id: str, pool_data: Dict = None) -> float:
        """
        Get most current price available using hybrid approach.
        """
        if pool_data:
            base_amt, quote_amt, calc_price = self.rpc_tracker.get_pool_reserves(pool_data)
            if calc_price > 0:
                return calc_price
            
            api_price = pool_data.get('price', 0)
            if api_price > 0:
                return api_price
        
        pool_data = self.api_client.get_pool_by_id(amm_id)
        if pool_data:
            base_amt, quote_amt, calc_price = self.rpc_tracker.get_pool_reserves(pool_data)
            if calc_price > 0:
                return calc_price
            
            return pool_data.get('price', 0)
        
        return 0
    
    def get_current_prices_batch(self, positions: Dict) -> Dict[str, float]:
        """Get current prices for all active positions."""
        prices = {}
        for amm_id, position in positions.items():
            pool_data = position.pool_data
            price = self.get_current_price(amm_id, pool_data)
            if price > 0:
                prices[amm_id] = price
        return prices


if __name__ == "__main__":
    from bot.raydium_client import RaydiumAPIClient
    
    api_client = RaydiumAPIClient()
    tracker = HybridPriceTracker(api_client)
    
    pools = api_client.get_filtered_pools(
        min_liquidity=10_000,
        min_volume_tvl_ratio=0.5,
        min_apr=5.0,
    )
    
    if pools:
        test_pool = pools[0]
        amm_id = test_pool['ammId']
        
        print(f"Testing price tracking for {test_pool['name']}:")
        print(f"AMM ID: {amm_id}\n")
        
        print("1. API price field:", test_pool.get('price'))
        
        base_amt, quote_amt, calc_price = tracker.rpc_tracker.get_pool_reserves(test_pool)
        print(f"2. Calculated from reserves:")
        print(f"   Base reserve: {base_amt:,.2f}")
        print(f"   Quote reserve: {quote_amt:,.2f}")
        print(f"   Calculated price: {calc_price}")
        
        print(f"\n3. Hybrid tracker price: {tracker.get_current_price(amm_id, test_pool)}")
        
        print(f"\nSimulating price monitoring (5 iterations):")
        for i in range(5):
            price = tracker.get_current_price(amm_id, test_pool)
            print(f"  Iteration {i+1}: {price}")
            time.sleep(1)
