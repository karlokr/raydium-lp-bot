"""
Raydium V3 API Client with caching

Uses the V3 mint-filtered endpoint which provides:
- burnPercent (LP burn data)
- Nested day/week/month stats (apr, volume, feeApr, volumeFee)
- Proper mint objects with symbol/name/decimals
- Paginated results with sort by fee24h (direct fee yield signal)

Available poolSortField values (V3 /pools/info/mint):
  default, liquidity, volume24h, fee24h, apr24h,
  volume7d, fee7d, apr7d, volume30d, fee30d, apr30d
"""
import os
import time
import requests
from typing import List, Dict, Optional
from bot.config import config


WSOL_MINT = "So11111111111111111111111111111111111111112"

# Raydium V4 AMM program — the only pool type our bridge supports.
# CPMM (CPMMoo8L...) and CLMM have different on-chain layouts.
RAYDIUM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


class RaydiumAPIClient:
    """Client for Raydium V3 API with caching and WSOL-pair filtering."""

    BASE_URL = "https://api-v3.raydium.io"
    GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
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
        # GeckoTerminal OHLCV cache: {pool_id: ([(high,low),...], timestamp)}
        self._ohlcv_cache: Dict[str, tuple] = {}
        self._ohlcv_cache_ttl: float = 6 * 3600  # 6h — daily candles don't change fast
        self._last_gecko_call: float = 0

    def get_sol_price_usd(self) -> float:
        """Get current SOL/USD price (Jupiter → CoinGecko fallback, cached 60s)."""
        now = time.time()
        if self._sol_price_usd > 0 and now - self._sol_price_timestamp < self._sol_price_ttl:
            return self._sol_price_usd

        for fetch in (self._fetch_price_jupiter, self._fetch_price_coingecko):
            price = fetch()
            if price > 0:
                self._sol_price_usd = price
                self._sol_price_timestamp = now
                return price

        return self._sol_price_usd  # last known price

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
        """Fetch WSOL pools from V3 API sorted by fee generation.

        Single query: poolSortField=fee24h, pageSize=1000.
        This directly surfaces pools generating the most trading fees,
        which is the only signal that matters for fee farming.

        Filters to V4 AMM pools only (our bridge doesn't support CPMM/CLMM).
        """
        url = (
            f"{self.BASE_URL}/pools/info/mint"
            f"?mint1={WSOL_MINT}"
            f"&poolType=standard"
            f"&poolSortField=fee24h"
            f"&sortType=desc"
            f"&pageSize=1000"
            f"&page=1"
        )

        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        pools_data = data.get('data', {})
        raw_pools = pools_data.get('data', [])

        pools = []
        for pool in raw_pools:
            if pool.get('programId', '') != RAYDIUM_V4_PROGRAM:
                continue
            pools.append(self._normalize_pool(pool))

        return pools

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
        min_fee_apr: float = None,
        max_price_range_ratio: float = None,
        min_volume_24h: float = None,
        min_fee_24h: float = None,
        min_volume_growth: float = None,  # day vs week growth rate
        quote_tokens: List[str] = None,
    ) -> List[Dict]:
        """
        Get filtered pools based on comprehensive fee farming criteria.
        
        Enhanced filters for LP fee farming:
        - min_fee_apr: Minimum pure fee APR (excludes rewards)
        - max_price_range_ratio: Maximum 24h price range (IL control)
        - min_fee_24h: Minimum absolute fees generated
        - min_volume_growth: Minimum volume acceleration (day/week ratio)
        """
        pools = self.get_all_pools()
        filtered = []

        for pool in pools:
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            day = pool.get('day', {})
            week = pool.get('week', {})
            
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            fee_apr = day.get('feeApr', 0)
            volume = day.get('volume', 0) or pool.get('volume24h', 0)
            fee_24h = day.get('volumeFee', 0) or pool.get('fee24h', 0)
            
            price_min = day.get('priceMin', 0)
            price_max = day.get('priceMax', 0)

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
            
            # Fee APR filter (pure trading fees, not rewards)
            if min_fee_apr and fee_apr < min_fee_apr:
                continue
            
            # Price range filter (IL control)
            if max_price_range_ratio and price_min > 0 and price_max > 0:
                range_ratio = price_max / price_min
                if range_ratio > max_price_range_ratio:
                    continue
            
            # Minimum volume filter
            if min_volume_24h and volume < min_volume_24h:
                continue
            
            # Minimum absolute fees filter
            if min_fee_24h and fee_24h < min_fee_24h:
                continue
            
            # Volume growth filter (momentum)
            if min_volume_growth:
                week_vol = week.get('volume', 0)
                week_avg = week_vol / 7 if week_vol > 0 else 0
                if week_avg > 0:
                    growth_rate = volume / week_avg
                    if growth_rate < min_volume_growth:
                        continue
                elif volume == 0:  # No day volume, reject
                    continue

            filtered.append(pool)

        return filtered

    # ------------------------------------------------------------------
    # GeckoTerminal OHLCV — daily candles for multi-period Parkinson σ
    # ------------------------------------------------------------------

    def get_pool_ohlcv_daily(self, pool_id: str, days: int = 7) -> List[tuple]:
        """Fetch daily OHLCV candles from GeckoTerminal (free, no API key).

        GeckoTerminal endpoint:
          GET /api/v2/networks/solana/pools/{pool_id}/ohlcv/day?limit={days}
        Returns OHLCV list: [[timestamp, open, high, low, close, volume], ...]

        Returns list of (high, low) tuples (newest first).  Empty list on error
        or if the pool is not found on GeckoTerminal.

        Rate-limited to 30 calls/minute (GeckoTerminal free tier).
        Results cached for 6 hours — daily candles don't change fast.
        """
        # Check cache
        now = time.time()
        cache_key = f"{pool_id}_{days}"
        if cache_key in self._ohlcv_cache:
            data, ts = self._ohlcv_cache[cache_key]
            if now - ts < self._ohlcv_cache_ttl:
                return data

        # Rate limit: 2.1s between calls → ~28 req/min, safely under 30
        elapsed = now - self._last_gecko_call
        if elapsed < 2.1:
            time.sleep(2.1 - elapsed)

        try:
            url = f"{self.GECKOTERMINAL_BASE}/networks/solana/pools/{pool_id}/ohlcv/day"
            resp = requests.get(url, params={'limit': days}, timeout=10)
            self._last_gecko_call = time.time()
            resp.raise_for_status()
            ohlcv_list = resp.json().get('data', {}).get('attributes', {}).get('ohlcv_list', [])
            # Extract (high, low) from each candle: [ts, open, HIGH, LOW, close, vol]
            candles = []
            for candle in ohlcv_list:
                if len(candle) >= 5:
                    high, low = float(candle[2]), float(candle[3])
                    if high > 0 and low > 0:
                        candles.append((high, low))
            self._ohlcv_cache[cache_key] = (candles, time.time())
            return candles
        except Exception:
            return []

    def enrich_pools_with_candles(self, pools: List[Dict], days: int = 7) -> None:
        """Fetch daily candles for each pool and add '_daily_candles' key.

        Pools already enriched (from cache) are skipped.
        Rate limiting is handled by get_pool_ohlcv_daily().
        """
        for pool in pools:
            if '_daily_candles' in pool:
                continue  # already enriched
            pool_id = pool.get('id', pool.get('ammId', ''))
            if pool_id:
                pool['_daily_candles'] = self.get_pool_ohlcv_daily(pool_id, days)
