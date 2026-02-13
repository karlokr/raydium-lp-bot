"""
Raydium V3 API Client with caching

Uses the V3 mint-filtered endpoint which provides:
- burnPercent (LP burn data)
- Nested day/week/month stats (apr, volume, feeApr)
- Proper mint objects with symbol/name/decimals
- Paginated results (no 704k dead pool downloads)
"""
import os
import time
import requests
from typing import List, Dict, Optional
from bot.config import config


WSOL_MINT = "So11111111111111111111111111111111111111112"


class RaydiumAPIClient:
    """Client for Raydium V3 API with caching and WSOL-pair filtering."""

    BASE_URL = "https://api-v3.raydium.io"
    JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"
    COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

    def __init__(self):
        self._cache: Optional[List[Dict]] = None
        self._cache_timestamp: float = 0
        self._cache_ttl = config.API_CACHE_TTL
        self._sol_price_usd: float = 0.0
        self._sol_price_timestamp: float = 0
        self._sol_price_ttl: float = 60  # refresh every 60s
        self._jupiter_api_key: str = os.getenv('JUPITER_API_KEY', '')

    def get_sol_price_usd(self) -> float:
        """Get current SOL/USD price.

        Tries Jupiter Price API v3 first (requires JUPITER_API_KEY in .env).
        Falls back to CoinGecko free API if Jupiter is unavailable.
        Cached for 60 seconds. Returns last known price on failure.
        """
        current_time = time.time()
        if self._sol_price_usd > 0 and current_time - self._sol_price_timestamp < self._sol_price_ttl:
            return self._sol_price_usd

        # Try Jupiter first (if API key is set)
        if self._jupiter_api_key:
            price = self._fetch_price_jupiter()
            if price > 0:
                self._sol_price_usd = price
                self._sol_price_timestamp = current_time
                return self._sol_price_usd

        # Fallback to CoinGecko (no API key required)
        price = self._fetch_price_coingecko()
        if price > 0:
            self._sol_price_usd = price
            self._sol_price_timestamp = current_time
            return self._sol_price_usd

        # If both fail and no Jupiter key, try Jupiter without key as last resort
        if not self._jupiter_api_key:
            price = self._fetch_price_jupiter()
            if price > 0:
                self._sol_price_usd = price
                self._sol_price_timestamp = current_time
                return self._sol_price_usd

        return self._sol_price_usd  # return last known price

    def _fetch_price_jupiter(self) -> float:
        """Fetch SOL/USD from Jupiter Price API v3."""
        try:
            headers = {}
            if self._jupiter_api_key:
                headers['x-api-key'] = self._jupiter_api_key
            resp = requests.get(
                self.JUPITER_PRICE_URL,
                params={'ids': WSOL_MINT},
                headers=headers,
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get(WSOL_MINT, {}).get('usdPrice', 0))
        except Exception:
            return 0.0

    def _fetch_price_coingecko(self) -> float:
        """Fetch SOL/USD from CoinGecko free API (no key required)."""
        try:
            resp = requests.get(
                self.COINGECKO_PRICE_URL,
                params={'ids': 'solana', 'vs_currencies': 'usd'},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get('solana', {}).get('usd', 0))
        except Exception:
            return 0.0

    def get_all_pools(self, force_refresh: bool = False) -> List[Dict]:
        """
        Fetch WSOL pools from Raydium V3 API with caching.

        Queries by liquidity and by volume separately, merges and deduplicates.
        This surfaces both deep-liquidity and high-activity pools.

        Returns normalized pool dicts with both V3 fields and backward-compatible aliases.
        """
        current_time = time.time()

        if not force_refresh and self._cache is not None:
            if current_time - self._cache_timestamp < self._cache_ttl:
                return self._cache

        try:
            pools = self._fetch_wsol_pools()
            self._cache = pools
            self._cache_timestamp = current_time
            print(f"✓ Fetched {len(pools)} WSOL pools from Raydium V3 API")
            return pools

        except requests.RequestException as e:
            print(f"✗ Error fetching pools: {e}")
            if self._cache is not None:
                print("⚠ Using stale cache data")
                return self._cache
            return []

    def _fetch_wsol_pools(self) -> List[Dict]:
        """Fetch WSOL pools from V3 API using multiple sort strategies.

        Queries by liquidity and by volume separately, then merges and
        deduplicates. This surfaces both deep-liquidity pools and
        high-activity pools that might rank lower by TVL alone.
        """
        seen_ids = set()
        merged = []

        for sort_field in ('liquidity', 'volume24h'):
            try:
                page = 1
                while True:
                    url = (
                        f"{self.BASE_URL}/pools/info/mint"
                        f"?mint1={WSOL_MINT}"
                        f"&poolType=standard"
                        f"&poolSortField={sort_field}"
                        f"&sortType=desc"
                        f"&pageSize=100"
                        f"&page={page}"
                    )

                    response = requests.get(url, timeout=15)
                    response.raise_for_status()
                    data = response.json()

                    pools_data = data.get('data', {})
                    pools = pools_data.get('data', [])

                    if not pools:
                        break

                    for pool in pools:
                        # Only Raydium V4 AMM pools are supported by our bridge.
                        # CPMM (CPMMoo8L...) and CLMM pools have different layouts.
                        pool_program = pool.get('programId', '')
                        if pool_program and pool_program != '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8':
                            continue
                        normalized = self._normalize_pool(pool)
                        pool_id = normalized.get('ammId', '')
                        if pool_id and pool_id not in seen_ids:
                            seen_ids.add(pool_id)
                            merged.append(normalized)

                    if not pools_data.get('hasNextPage', False):
                        break

                    page += 1
                    if page > 10:  # 1000 per sort field max
                        break
            except Exception as e:
                print(f"⚠ Error fetching pools sorted by {sort_field}: {e}")

        return merged

    def _normalize_pool(self, pool: Dict) -> Dict:
        """
        Add backward-compatible field names alongside V3 originals.
        V3 fields: id, tvl, burnPercent, feeRate, openTime,
                    mintA{address,symbol,decimals}, mintB{...},
                    day{apr,volume,volumeFee,feeApr}
        """
        mint_a = pool.get('mintA', {})
        mint_b = pool.get('mintB', {})
        day = pool.get('day', {})

        sym_a = mint_a.get('symbol', '?')
        sym_b = mint_b.get('symbol', '?')

        pool['name'] = f"{sym_a}/{sym_b}"
        pool['ammId'] = pool.get('id', '')
        pool['liquidity'] = pool.get('tvl', 0)
        pool['apr24h'] = day.get('apr', 0)
        pool['volume24h'] = day.get('volume', 0)
        pool['fee24h'] = day.get('volumeFee', 0)
        pool['baseMint'] = mint_a.get('address', '')
        pool['quoteMint'] = mint_b.get('address', '')

        # Derive price from reserve amounts
        mint_a_amount = pool.get('mintAmountA', 0)
        mint_b_amount = pool.get('mintAmountB', 0)
        try:
            if float(mint_a_amount) > 0 and float(mint_b_amount) > 0:
                pool['price'] = float(mint_b_amount) / float(mint_a_amount)
            else:
                pool['price'] = pool.get('price', 0)
        except (ValueError, TypeError):
            pool['price'] = pool.get('price', 0)

        return pool

    def get_pool_by_id(self, amm_id: str) -> Optional[Dict]:
        """Get specific pool by AMM ID. Checks cache first, then direct API."""
        pools = self.get_all_pools()
        for pool in pools:
            if pool.get('ammId') == amm_id or pool.get('id') == amm_id:
                return pool

        # Direct API lookup for pools not in WSOL cache
        try:
            url = f"{self.BASE_URL}/pools/info/ids?ids={amm_id}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json().get('data', [])
                if data:
                    return self._normalize_pool(data[0])
        except requests.RequestException:
            pass

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
        V3 API already returns only WSOL pools sorted by liquidity.
        """
        pools = self.get_all_pools()
        filtered = []

        for pool in pools:
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            day = pool.get('day', {})
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            volume = day.get('volume', 0) or pool.get('volume24h', 0)

            if tvl <= 0:
                continue

            if min_liquidity and tvl < min_liquidity:
                continue

            if min_volume_tvl_ratio:
                vol_tvl = volume / tvl if tvl > 0 else 0
                if vol_tvl < min_volume_tvl_ratio:
                    continue

            if min_apr and apr < min_apr:
                continue

            filtered.append(pool)

        return filtered
