"""
Raydium API Client with caching
"""
import time
import requests
from typing import List, Dict, Optional
from bot.config import config


class RaydiumAPIClient:
    def __init__(self):
        self.api_url = config.RAYDIUM_API_URL
        self._cache: Optional[List[Dict]] = None
        self._cache_timestamp: float = 0
        self._cache_ttl = config.API_CACHE_TTL

    def get_all_pools(self, force_refresh: bool = False) -> List[Dict]:
        """
        Fetch all pools from Raydium API with caching.

        NOTE: This data is NOT real-time. Raydium updates this endpoint
        approximately every 5-15 minutes. Use this for pool discovery,
        but track prices via RPC for real-time IL calculations.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of pool dictionaries with keys:
            - name, ammId, liquidity, price, apr24h, apr7d, apr30d
            - volume24h, fee24h, tokenAmountCoin, tokenAmountPc, etc.
        """
        current_time = time.time()

        # Return cached data if still valid
        if not force_refresh and self._cache is not None:
            if current_time - self._cache_timestamp < self._cache_ttl:
                return self._cache

        # Fetch fresh data
        try:
            response = requests.get(self.api_url, timeout=10)
            response.raise_for_status()
            pools = response.json()

            # Update cache
            self._cache = pools
            self._cache_timestamp = current_time

            print(f"✓ Fetched {len(pools):,} pools from Raydium API")
            return pools

        except requests.RequestException as e:
            print(f"✗ Error fetching pools: {e}")

            # Return stale cache if available
            if self._cache is not None:
                print("⚠ Using stale cache data")
                return self._cache

            return []

    def get_pool_by_id(self, amm_id: str) -> Optional[Dict]:
        """Get specific pool by AMM ID"""
        pools = self.get_all_pools()
        for pool in pools:
            if pool.get('ammId') == amm_id:
                return pool
        return None

    def get_filtered_pools(
        self,
        min_liquidity: float = None,
        min_volume_tvl_ratio: float = None,
        min_apr: float = None,
        quote_tokens: List[str] = None,
    ) -> List[Dict]:
        """
        Get filtered pools based on criteria.

        Args:
            min_liquidity: Minimum TVL in USD
            min_volume_tvl_ratio: Minimum 24h volume / TVL ratio
            min_apr: Minimum 24h APR
            quote_tokens: List of allowed quote token mints
        """
        pools = self.get_all_pools()
        filtered = []

        for pool in pools:
            # Skip pools with missing data
            liquidity = pool.get('liquidity', 0)
            volume24h = pool.get('volume24h', 0)
            apr24h = pool.get('apr24h', 0)

            if liquidity == 0:
                continue

            # Apply filters
            if min_liquidity and liquidity < min_liquidity:
                continue

            if min_volume_tvl_ratio:
                volume_tvl = volume24h / liquidity if liquidity > 0 else 0
                if volume_tvl < min_volume_tvl_ratio:
                    continue

            if min_apr and apr24h < min_apr:
                continue

            if quote_tokens:
                quote_mint = pool.get('quoteMint', '')
                base_mint = pool.get('baseMint', '')

                # For WSOL-only mode, check if WSOL is either base or quote
                if config.REQUIRE_WSOL_PAIRS:
                    wsol_address = "So11111111111111111111111111111111111111112"
                    if not (quote_mint == wsol_address or base_mint == wsol_address):
                        continue
                # Otherwise check if quote token is in allowed list
                elif quote_mint not in quote_tokens:
                    continue

            filtered.append(pool)

        return filtered


if __name__ == "__main__":
    # Test the client
    client = RaydiumAPIClient()

    print("Fetching pools...")
    pools = client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
        quote_tokens=config.ALLOWED_QUOTE_TOKENS,
    )

    print(f"\n{len(pools)} pools match criteria:")
    print(f"Sorted by 24h APR:\n")

    # Sort by APR and show top 10
    top_pools = sorted(pools, key=lambda x: x.get('apr24h', 0), reverse=True)[:10]

    for i, pool in enumerate(top_pools, 1):
        print(f"{i}. {pool['name']}")
        print(f"   TVL: ${pool['liquidity']:,.2f}")
        print(f"   24h Volume: ${pool['volume24h']:,.2f}")
        print(f"   Volume/TVL: {pool['volume24h']/pool['liquidity']:.2f}x")
        print(f"   APR (24h): {pool['apr24h']:.2f}%")
        print(f"   AMM ID: {pool['ammId']}")
        print()
