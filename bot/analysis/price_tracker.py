"""
Price Tracker

Gets current prices from the Raydium V3 API pool data.
Uses reserve ratios (mintAmountA/mintAmountB) or the price field.
"""
from typing import Dict


class PriceTracker:
    """
    Gets current prices from cached V3 API pool data.
    Prices are derived from reserve amounts or the API price field.
    """

    def __init__(self, api_client):
        self.api_client = api_client

    def get_current_price(self, amm_id: str, pool_data: Dict = None) -> float:
        """
        Get current price for a pool.

        Priority:
        1. Derive from reserve amounts in pool_data (mintAmountA/mintAmountB)
        2. Use the pre-computed 'price' field
        3. Look up pool by ID from API
        """
        if pool_data:
            price = self._price_from_pool(pool_data)
            if price > 0:
                return price

        # Fetch from API if not provided
        pool_data = self.api_client.get_pool_by_id(amm_id)
        if pool_data:
            price = self._price_from_pool(pool_data)
            if price > 0:
                return price

        return 0

    def _price_from_pool(self, pool: Dict) -> float:
        """Extract price from pool data (reserve ratio preferred, then price field)."""
        try:
            mint_a, mint_b = float(pool.get('mintAmountA', 0)), float(pool.get('mintAmountB', 0))
            if mint_a > 0 and mint_b > 0:
                return mint_b / mint_a
        except (ValueError, TypeError):
            pass
        try:
            return max(0.0, float(pool.get('price', 0)))
        except (ValueError, TypeError):
            return 0.0

    def get_current_prices_batch(self, positions: Dict) -> Dict[str, float]:
        """Get current prices for all active positions."""
        prices = {}
        for amm_id, position in positions.items():
            price = self.get_current_price(amm_id, position.pool_data)
            if price > 0:
                prices[amm_id] = price
        return prices
