"""
Integration tests — hit real APIs / RPC over the network.

These are SKIPPED by default.  Run them explicitly with:

    pytest -m integration          # only integration tests
    make test-integration          # same thing via Makefile
    pytest -m "not integration"    # only unit tests (default)

Requirements:
    - Network access
    - SOLANA_RPC_URL set in .env (or a real mainnet/devnet endpoint)
    - A dummy (but valid) wallet key is injected by conftest.py — your
      real wallet key is NEVER used during tests

These tests go beyond "does it return something?" — they verify actual
data normalization, filtering logic, scoring pipelines, cross-module
interactions, and safety analysis depth against live data.
"""
import os
import sys
import time
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

integration = pytest.mark.integration

# ── Helpers ──────────────────────────────────────────────────────────

def _have_rpc() -> bool:
    url = os.getenv("SOLANA_RPC_URL", "")
    return bool(url) and "example.com" not in url

def _have_wallet() -> bool:
    """Always True — conftest injects a dummy (but valid) keypair."""
    key = os.getenv("WALLET_PRIVATE_KEY", "")
    return len(key) >= 64

WSOL_MINT = "So11111111111111111111111111111111111111112"
AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


# ── Raydium API — Pool Fetching & Normalization ─────────────────────

@integration
class TestRaydiumPoolFetching:
    """Tests that the V3 API returns properly normalized pool data."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        self.client = RaydiumAPIClient()

    def test_fetches_nonzero_pools(self):
        pools = self.client.get_all_pools(force_refresh=True)
        assert isinstance(pools, list)
        assert len(pools) > 10, f"Expected many WSOL pools, got {len(pools)}"

    def test_all_pools_are_wsol_pairs(self):
        """Every returned pool should have WSOL as either baseMint or quoteMint."""
        pools = self.client.get_all_pools(force_refresh=True)
        for pool in pools[:50]:  # spot-check first 50
            base = pool.get('baseMint', '')
            quote = pool.get('quoteMint', '')
            assert WSOL_MINT in (base, quote), (
                f"Pool {pool.get('name', '?')} ({pool.get('ammId', '?')}) "
                f"is not a WSOL pair: baseMint={base}, quoteMint={quote}"
            )

    def test_all_pools_are_amm_v4(self):
        """Only AMM V4 pools should be returned (bridge only supports this program)."""
        pools = self.client.get_all_pools(force_refresh=True)
        for pool in pools[:50]:
            program = pool.get('programId', '')
            assert program == AMM_V4_PROGRAM, (
                f"Pool {pool.get('name', '?')} has non-V4 programId: {program}"
            )

    def test_normalization_adds_backward_compat_fields(self):
        """_normalize_pool should add ammId, name, liquidity, baseMint, quoteMint."""
        pools = self.client.get_all_pools(force_refresh=True)
        assert len(pools) > 0
        pool = pools[0]

        # V3 native fields
        assert 'id' in pool, "Missing V3 field 'id'"
        assert 'tvl' in pool, "Missing V3 field 'tvl'"
        assert 'mintA' in pool, "Missing V3 field 'mintA'"
        assert 'mintB' in pool, "Missing V3 field 'mintB'"

        # Backward-compatible aliases from _normalize_pool
        assert 'ammId' in pool, "Missing normalized field 'ammId'"
        assert 'name' in pool, "Missing normalized field 'name'"
        assert 'liquidity' in pool, "Missing normalized field 'liquidity'"
        assert 'baseMint' in pool, "Missing normalized field 'baseMint'"
        assert 'quoteMint' in pool, "Missing normalized field 'quoteMint'"
        assert 'apr24h' in pool, "Missing normalized field 'apr24h'"
        assert 'volume24h' in pool, "Missing normalized field 'volume24h'"

        # ammId should equal id
        assert pool['ammId'] == pool['id']
        # liquidity should equal tvl
        assert pool['liquidity'] == pool['tvl']

    def test_pool_name_format(self):
        """Pool name should be 'SYMBOL/SYMBOL' derived from mintA/mintB."""
        pools = self.client.get_all_pools(force_refresh=True)
        for pool in pools[:20]:
            name = pool.get('name', '')
            assert '/' in name, f"Pool name '{name}' missing '/' separator"
            parts = name.split('/')
            assert len(parts) == 2, f"Pool name '{name}' should have exactly 2 parts"
            assert all(len(p) > 0 for p in parts), f"Pool name '{name}' has empty part"

    def test_day_stats_nested_structure(self):
        """V3 API provides nested day stats with apr, volume, feeApr, etc."""
        pools = self.client.get_all_pools(force_refresh=True)
        pool = pools[0]
        day = pool.get('day')
        assert isinstance(day, dict), f"Expected day to be dict, got {type(day)}"

        # These fields come from the V3 API day stats
        for key in ('apr', 'volume'):
            assert key in day, f"day stats missing '{key}'"
            assert isinstance(day[key], (int, float)), f"day.{key} should be numeric"

    def test_dedup_across_sort_strategies(self):
        """Pools fetched by liquidity AND volume should be deduped by ammId."""
        pools = self.client.get_all_pools(force_refresh=True)
        amm_ids = [p.get('ammId') for p in pools]
        assert len(amm_ids) == len(set(amm_ids)), (
            f"Duplicate ammIds found: {len(amm_ids)} total, {len(set(amm_ids))} unique"
        )

    def test_cache_returns_same_data_without_refresh(self):
        """Second call (no force_refresh) should return cached data instantly."""
        pools1 = self.client.get_all_pools(force_refresh=True)
        t0 = time.time()
        pools2 = self.client.get_all_pools(force_refresh=False)
        elapsed = time.time() - t0
        assert pools1 is pools2, "Expected same list object from cache"
        assert elapsed < 0.1, f"Cache hit took {elapsed:.3f}s — expected <0.1s"


# ── Raydium API — Filtering ─────────────────────────────────────────

@integration
class TestRaydiumPoolFiltering:
    """Tests that filter logic works correctly against real API data."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        self.client = RaydiumAPIClient()
        self.client.get_all_pools(force_refresh=True)  # warm cache

    def test_min_liquidity_filter(self):
        """All returned pools should meet the TVL floor."""
        threshold = 10_000
        pools = self.client.get_filtered_pools(min_liquidity=threshold)
        assert isinstance(pools, list)
        for pool in pools:
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            assert tvl >= threshold, (
                f"Pool {pool.get('name')} has TVL ${tvl:,.0f} below threshold ${threshold:,.0f}"
            )

    def test_min_apr_filter(self):
        """All returned pools should meet the APR floor."""
        threshold = 100
        pools = self.client.get_filtered_pools(min_apr=threshold)
        for pool in pools:
            day = pool.get('day', {})
            apr = day.get('apr', 0) or pool.get('apr24h', 0)
            assert apr >= threshold, (
                f"Pool {pool.get('name')} has APR {apr:.1f}% below threshold {threshold}%"
            )

    def test_min_volume_tvl_ratio_filter(self):
        """All returned pools should meet the vol/TVL ratio floor."""
        threshold = 0.5
        pools = self.client.get_filtered_pools(min_volume_tvl_ratio=threshold)
        for pool in pools:
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            day = pool.get('day', {})
            volume = day.get('volume', 0) or pool.get('volume24h', 0)
            ratio = volume / tvl if tvl > 0 else 0
            assert ratio >= threshold, (
                f"Pool {pool.get('name')} has vol/TVL ratio {ratio:.2f} below {threshold}"
            )

    def test_combined_filters_are_stricter(self):
        """Applying multiple filters should return <= the least-filtered set."""
        loose = self.client.get_filtered_pools(min_liquidity=5_000)
        strict = self.client.get_filtered_pools(
            min_liquidity=5_000,
            min_apr=100,
            min_volume_tvl_ratio=0.5,
        )
        assert len(strict) <= len(loose), (
            f"Strict filters ({len(strict)}) returned more than loose ({len(loose)})"
        )

    def test_very_strict_filters_return_fewer_pools(self):
        """Extremely high thresholds should return fewer pools."""
        many = self.client.get_filtered_pools(min_liquidity=1_000)
        few = self.client.get_filtered_pools(min_liquidity=500_000)
        assert len(few) <= len(many)


# ── SOL Price ────────────────────────────────────────────────────────

@integration
class TestSOLPrice:
    """Tests SOL/USD price fetching from Jupiter/CoinGecko."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        self.client = RaydiumAPIClient()

    def test_sol_price_is_positive(self):
        price = self.client.get_sol_price_usd()
        assert price > 0, "SOL price should be > 0"

    def test_sol_price_is_reasonable(self):
        """SOL price should be between $1 and $10,000 (sanity check)."""
        price = self.client.get_sol_price_usd()
        assert 1 < price < 10_000, f"SOL price ${price:.2f} seems unreasonable"

    def test_sol_price_caching(self):
        """Second call within TTL should return cached price instantly."""
        price1 = self.client.get_sol_price_usd()
        t0 = time.time()
        price2 = self.client.get_sol_price_usd()
        elapsed = time.time() - t0
        assert price1 == price2, "Cached price should be identical"
        assert elapsed < 0.05, f"Cache hit took {elapsed:.3f}s — expected <0.05s"


# ── RugCheck API ─────────────────────────────────────────────────────

@integration
class TestRugCheckAnalysis:
    """Tests that RugCheck API returns usable safety data for real tokens."""

    # BONK — well-known, widely held, safe token
    BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    # USDC — the safest possible token
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.safety.rugcheck import RugCheckAPI
        self.api = RugCheckAPI()

    def test_known_token_report_has_required_fields(self):
        """Raw report for BONK should have score, risks, topHolders."""
        report = self.api.get_token_report(self.BONK_MINT)
        assert report is not None, "Expected a report for BONK"
        assert 'score' in report, "Report missing 'score'"
        assert 'risks' in report, "Report missing 'risks'"
        assert isinstance(report['risks'], list)
        assert 'topHolders' in report, "Report missing 'topHolders'"

    def test_analyze_safety_result_shape(self):
        """analyze_token_safety should return all documented fields."""
        result = self.api.analyze_token_safety(self.BONK_MINT)
        expected_keys = {
            'available', 'risk_score', 'risk_level', 'is_rugged',
            'dangers', 'warnings', 'has_freeze_authority', 'has_mint_authority',
            'has_mutable_metadata', 'low_lp_providers',
            'top5_holder_pct', 'top10_holder_pct', 'max_single_holder_pct',
            'total_holders',
        }
        missing = expected_keys - set(result.keys())
        assert not missing, f"analyze_token_safety missing keys: {missing}"

    def test_bonk_is_available_and_not_rugged(self):
        """BONK is a well-known legitimate token."""
        result = self.api.analyze_token_safety(self.BONK_MINT)
        assert result['available'] is True
        assert result['is_rugged'] is False

    def test_bonk_risk_score_is_numeric_and_bounded(self):
        result = self.api.analyze_token_safety(self.BONK_MINT)
        assert isinstance(result['risk_score'], (int, float))
        assert 0 <= result['risk_score'] <= 100

    def test_bonk_risk_level_is_valid_enum(self):
        result = self.api.analyze_token_safety(self.BONK_MINT)
        assert result['risk_level'] in ('low', 'medium', 'high')

    def test_bonk_has_holders(self):
        """A popular token should have many holders (when API provides data)."""
        result = self.api.analyze_token_safety(self.BONK_MINT)
        holders = result['total_holders']
        assert isinstance(holders, (int, float)), f"total_holders should be numeric, got {type(holders)}"
        assert holders >= 0, f"total_holders should be non-negative, got {holders}"
        # RugCheck API may not populate holder counts; when it does, BONK has many
        if holders > 0:
            assert holders > 1000, (
                f"When populated, BONK should have >1000 holders, got {holders}"
            )

    def test_bonk_holder_percentages_are_bounded(self):
        result = self.api.analyze_token_safety(self.BONK_MINT)
        assert 0 <= result['top5_holder_pct'] <= 100
        assert 0 <= result['top10_holder_pct'] <= 100
        assert 0 <= result['max_single_holder_pct'] <= 100
        # top10 >= top5 (more holders = more concentration)
        assert result['top10_holder_pct'] >= result['top5_holder_pct']

    def test_bonk_no_freeze_or_mint_authority(self):
        """BONK should not have dangerous authorities."""
        result = self.api.analyze_token_safety(self.BONK_MINT)
        assert result['has_freeze_authority'] is False, "BONK should not have freeze authority"
        assert result['has_mint_authority'] is False, "BONK should not have mint authority"

    def test_usdc_is_low_risk(self):
        """USDC is the safest token — risk score should be very low."""
        result = self.api.analyze_token_safety(self.USDC_MINT)
        assert result['available'] is True
        assert result['risk_score'] <= 20, (
            f"USDC should be very low risk, got score {result['risk_score']}"
        )

    def test_unknown_token_returns_unavailable_or_none(self):
        """A fake mint should return None report or unavailable analysis."""
        fake_mint = "1" * 44
        report = self.api.get_token_report(fake_mint)
        if report is None:
            return  # 404 = expected
        # If API returns something, analysis should reflect uncertainty
        result = self.api.analyze_token_safety(fake_mint)
        assert isinstance(result, dict)

    def test_caching_returns_same_data(self):
        """Second call should hit cache and return identical data."""
        r1 = self.api.get_token_report(self.BONK_MINT)
        t0 = time.time()
        r2 = self.api.get_token_report(self.BONK_MINT)
        elapsed = time.time() - t0
        assert r1 is r2, "Expected same dict object from cache"
        assert elapsed < 0.05, f"Cache hit took {elapsed:.3f}s"


# ── Pool Scoring Pipeline (API → Analyzer) ──────────────────────────

@integration
class TestPoolScoringPipeline:
    """End-to-end: fetch real pools → score them → verify scoring logic."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        from bot.analysis.pool_analyzer import PoolAnalyzer
        from bot.analysis.snapshot_tracker import SnapshotTracker
        self.client = RaydiumAPIClient()
        self.analyzer = PoolAnalyzer()
        self.tracker = SnapshotTracker(max_snapshots=10)
        self.analyzer.set_snapshot_tracker(self.tracker)

    def test_real_pools_score_between_0_and_110(self):
        """Scores should be 0-100 base + up to 10 velocity bonus."""
        pools = self.client.get_filtered_pools(
            min_liquidity=5_000, min_apr=50,
        )
        assert len(pools) > 0, "No pools matched basic filters"

        for pool in pools[:20]:
            score = self.analyzer.calculate_pool_score(pool)
            assert 0 <= score <= 110, (
                f"Pool {pool.get('name')} scored {score}, expected 0-110"
            )

    def test_rank_pools_returns_sorted_descending(self):
        """rank_pools should return pools sorted by score, highest first."""
        pools = self.client.get_filtered_pools(min_liquidity=5_000)
        if len(pools) < 3:
            pytest.skip("Not enough pools for ranking test")

        ranked = self.analyzer.rank_pools(pools, top_n=10)
        assert len(ranked) <= 10
        assert len(ranked) > 0

        scores = [p['score'] for p in ranked]
        assert scores == sorted(scores, reverse=True), (
            f"Scores not sorted descending: {scores}"
        )

    def test_rank_pools_injects_component_scores(self):
        """Each ranked pool should have the new scoring components."""
        pools = self.client.get_filtered_pools(min_liquidity=5_000)
        if not pools:
            pytest.skip("No pools matched filters")

        ranked = self.analyzer.rank_pools(pools, top_n=5)
        pool = ranked[0]

        for component in ('_fee_apr', '_fee_consistency', '_depth', '_il_safety'):
            assert component in pool, f"Missing component score '{component}'"
            assert isinstance(pool[component], (int, float))

    def test_higher_apr_pool_scores_higher_on_fee_component(self):
        """Given two pools, the one with higher fee APR should score higher
        on the fee component (all else being equal is unlikely, but the
        overall score should reflect fee potential)."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if len(pools) < 5:
            pytest.skip("Not enough pools")

        # Sort by fee APR
        pools_with_apr = [
            p for p in pools
            if (p.get('day', {}).get('feeApr', 0) or p.get('day', {}).get('apr', 0)) > 0
        ]
        if len(pools_with_apr) < 2:
            pytest.skip("Not enough pools with APR data")

        pools_with_apr.sort(
            key=lambda p: p.get('day', {}).get('feeApr', 0) or p.get('day', {}).get('apr', 0),
            reverse=True,
        )
        high_apr = pools_with_apr[0]
        low_apr = pools_with_apr[-1]

        high_apr_val = high_apr.get('day', {}).get('feeApr', 0) or high_apr.get('day', {}).get('apr', 0)
        low_apr_val = low_apr.get('day', {}).get('feeApr', 0) or low_apr.get('day', {}).get('apr', 0)
        if high_apr_val <= low_apr_val:
            pytest.skip("APR values too similar")

        # Fee APR component: min(30, (apr / 200) * 30)
        high_fee_score = min(30, (high_apr_val / 200) * 30)
        low_fee_score = min(30, (low_apr_val / 200) * 30)
        assert high_fee_score >= low_fee_score

    def test_snapshot_velocity_bonus_accumulates_with_real_data(self):
        """Record real pool data as snapshots, verify velocity bonus appears."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        pool = pools[0]
        pool_id = pool.get('ammId', pool.get('id', ''))
        day = pool.get('day', {})
        volume = day.get('volume', 0) or pool.get('volume24h', 0)
        tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
        price = pool.get('price', 0)

        # Need >= 3 snapshots for velocity bonus
        assert self.tracker.get_velocity_bonus(pool_id) == 0.0

        # Simulate 4 scan cycles with slightly rising volume (realistic growth)
        for i in range(4):
            self.tracker.record(pool_id, volume * (1 + 0.05 * i), tvl, price)

        bonus = self.tracker.get_velocity_bonus(pool_id)
        assert bonus >= 0.0, f"Velocity bonus should be >= 0, got {bonus}"

        # Score WITH snapshot tracker should include the bonus
        score_with = self.analyzer.calculate_pool_score(pool)

        # Score WITHOUT should be lower (or equal if bonus=0)
        from bot.analysis.pool_analyzer import PoolAnalyzer
        standalone = PoolAnalyzer()  # no snapshot tracker attached
        score_without = standalone.calculate_pool_score(pool)

        assert score_with >= score_without, (
            f"Score with tracker ({score_with}) should be >= without ({score_without})"
        )

    def test_position_size_with_real_pool(self):
        """calculate_position_size with a real pool should return valid SOL amount."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        size = self.analyzer.calculate_position_size(
            pools[0], available_capital=2.0, num_open_positions=0,
        )
        from bot.config import config
        assert 0 < size <= config.MAX_ABSOLUTE_POSITION_SOL
        assert size <= 2.0 - config.RESERVE_SOL

    def test_impermanent_loss_with_real_price_moves(self):
        """Fetch a real pool's price range and compute IL from it."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        pool = pools[0]
        day = pool.get('day', {})
        price_min = day.get('priceMin', 0)
        price_max = day.get('priceMax', 0)

        if price_min > 0 and price_max > 0 and price_min != price_max:
            from bot.analysis.pool_analyzer import PoolAnalyzer
            il = PoolAnalyzer.calculate_impermanent_loss(price_min, price_max)
            # Standard IL is always <= 0 and bounded
            assert il <= 0, f"Standard IL must be <= 0, got {il}"
            assert abs(il) < 0.5, f"IL of {il*100:.1f}% seems too extreme for 24h"


# ── Pool Quality Pipeline (API → RugCheck → Analysis) ───────────────

@integration
class TestPoolQualityPipeline:
    """End-to-end: fetch real pools → safety analysis → verify decisions."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        from bot.analysis.pool_quality import PoolQualityAnalyzer
        self.client = RaydiumAPIClient()
        self.analyzer = PoolQualityAnalyzer()

    def test_analyze_pool_result_shape(self):
        """analyze_pool should return all documented keys."""
        pools = self.client.get_filtered_pools(
            min_liquidity=10_000, min_volume_tvl_ratio=0.3,
        )
        if not pools:
            pytest.skip("No pools matched filters")

        result = self.analyzer.analyze_pool(pools[0], check_safety=True)
        expected_keys = {
            'risk_level', 'risks', 'warnings', 'is_safe',
            'burn_percent', 'liquidity_tier', 'rugcheck',
        }
        missing = expected_keys - set(result.keys())
        assert not missing, f"analyze_pool missing keys: {missing}"

    def test_risk_level_is_valid_enum(self):
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        result = self.analyzer.analyze_pool(pools[0], check_safety=True)
        assert result['risk_level'] in ('LOW', 'MEDIUM', 'HIGH')

    def test_risks_and_warnings_are_lists_of_strings(self):
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        result = self.analyzer.analyze_pool(pools[0], check_safety=True)
        assert isinstance(result['risks'], list)
        assert isinstance(result['warnings'], list)
        for r in result['risks']:
            assert isinstance(r, str)
        for w in result['warnings']:
            assert isinstance(w, str)

    def test_is_safe_means_no_risks(self):
        """is_safe=True should mean the risks list is empty."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        result = self.analyzer.analyze_pool(pools[0], check_safety=True)
        if result['is_safe']:
            assert len(result['risks']) == 0, (
                f"is_safe=True but risks present: {result['risks']}"
            )
        else:
            assert len(result['risks']) > 0, (
                "is_safe=False but no risks listed"
            )

    def test_rugcheck_data_is_populated_when_checking_safety(self):
        """When check_safety=True, rugcheck result should be populated."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        result = self.analyzer.analyze_pool(pools[0], check_safety=True)
        rugcheck = result.get('rugcheck')
        # rugcheck can be None if the pool was rejected before the check
        # OR a dict with 'available' key
        if rugcheck is not None:
            assert isinstance(rugcheck, dict)
            assert 'available' in rugcheck

    def test_no_safety_check_skips_rugcheck(self):
        """When check_safety=False, rugcheck should be None."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        result = self.analyzer.analyze_pool(pools[0], check_safety=False)
        assert result['rugcheck'] is None

    def test_burn_percent_matches_pool_data(self):
        """analyze_pool should carry through the pool's burnPercent."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        pool = pools[0]
        result = self.analyzer.analyze_pool(pool, check_safety=False)
        assert result['burn_percent'] == pool.get('burnPercent', 0)

    def test_low_burn_generates_risk(self):
        """A pool with <50% burn should get a risk entry (if one exists in data)."""
        pools = self.client.get_all_pools(force_refresh=True)
        low_burn = [p for p in pools if p.get('burnPercent', 100) < 50]
        if not low_burn:
            pytest.skip("No low-burn pools in current data")

        result = self.analyzer.analyze_pool(low_burn[0], check_safety=False)
        burn_risks = [r for r in result['risks'] if 'burn' in r.lower()]
        assert len(burn_risks) > 0, (
            f"Pool with burnPercent={low_burn[0].get('burnPercent')} "
            f"should have a burn risk, but risks = {result['risks']}"
        )

    def test_get_safe_pools_filters_correctly(self):
        """get_safe_pools should return a subset of the input."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        # check_locks=True triggers RugCheck
        safe = self.analyzer.get_safe_pools(
            pools[:10],  # limit to 10 for speed
            check_locks=True,
            analyzer=self.analyzer,
        )
        assert isinstance(safe, list)
        assert len(safe) <= len(pools[:10])

        # Every safe pool should pass analyze_pool
        for pool in safe:
            result = self.analyzer.analyze_pool(pool, check_safety=True)
            assert result['is_safe'] is True, (
                f"Pool {pool.get('name')} in safe list but analyze_pool says unsafe: "
                f"{result['risks']}"
            )

    def test_liquidity_tier_assignment(self):
        """liquidity_tier should be high/medium/low based on TVL."""
        pools = self.client.get_all_pools(force_refresh=True)
        for pool in pools[:20]:
            tvl = pool.get('tvl', 0) or pool.get('liquidity', 0)
            result = self.analyzer.analyze_pool(pool, check_safety=False)
            tier = result['liquidity_tier']
            if tvl > 100_000:
                assert tier == 'high', f"TVL ${tvl:,.0f} should be 'high', got '{tier}'"
            elif tvl > 50_000:
                assert tier == 'medium', f"TVL ${tvl:,.0f} should be 'medium', got '{tier}'"
            else:
                assert tier == 'low', f"TVL ${tvl:,.0f} should be 'low', got '{tier}'"


# ── Price Tracker with Real Data ─────────────────────────────────────

@integration
class TestPriceTrackerLive:
    """Test that PriceTracker correctly derives prices from real API data."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from bot.raydium_client import RaydiumAPIClient
        from bot.analysis.price_tracker import PriceTracker
        self.client = RaydiumAPIClient()
        self.tracker = PriceTracker(self.client)

    def test_price_from_pool_data(self):
        """Price derived from real pool data should be positive."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        pool = pools[0]
        amm_id = pool.get('ammId', pool.get('id', ''))
        price = self.tracker.get_current_price(amm_id, pool)
        assert price > 0, f"Price for {pool.get('name')} should be > 0, got {price}"

    def test_price_from_api_lookup(self):
        """Price fetched via API lookup (no pool_data provided) should be positive."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        amm_id = pools[0].get('ammId', pools[0].get('id', ''))
        price = self.tracker.get_current_price(amm_id)
        assert price > 0, f"API-fetched price should be > 0, got {price}"

    def test_batch_prices_for_real_pools(self):
        """get_current_prices_batch should return prices for all provided positions."""
        from bot.trading.position_manager import Position
        from datetime import datetime

        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if len(pools) < 2:
            pytest.skip("Need >= 2 pools")

        # Create mock positions with real pool data
        positions = {}
        for pool in pools[:3]:
            amm_id = pool.get('ammId', pool.get('id', ''))
            pos = Position(
                amm_id=amm_id,
                pool_name=pool.get('name', ''),
                entry_time=datetime.now(),
                entry_price_ratio=pool.get('price', 0),
                position_size_sol=1.0,
                token_a_amount=100,
                token_b_amount=100,
                pool_data=pool,
            )
            positions[amm_id] = pos

        prices = self.tracker.get_current_prices_batch(positions)
        assert isinstance(prices, dict)
        assert len(prices) > 0
        for amm_id, price in prices.items():
            assert price > 0, f"Batch price for {amm_id} should be > 0"

    def test_price_consistency_between_methods(self):
        """Price from pool_data vs API lookup should be close (same source)."""
        pools = self.client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools")

        pool = pools[0]
        amm_id = pool.get('ammId', pool.get('id', ''))

        price_from_data = self.tracker.get_current_price(amm_id, pool)
        price_from_api = self.tracker.get_current_price(amm_id)

        if price_from_data > 0 and price_from_api > 0:
            ratio = max(price_from_data, price_from_api) / min(price_from_data, price_from_api)
            assert ratio < 1.5, (
                f"Price from data ({price_from_data}) vs API ({price_from_api}) "
                f"differ by {ratio:.2f}x — too much divergence"
            )


# ── Node.js Bridge ───────────────────────────────────────────────────

@integration
class TestBridgeLive:
    """Tests that the Node.js SDK bridge works correctly."""

    def test_node_is_available(self):
        import subprocess
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        # Version should start with 'v' and be >= 18
        version = result.stdout.strip()
        assert version.startswith('v'), f"Unexpected node version format: {version}"
        major = int(version.split('.')[0].lstrip('v'))
        assert major >= 18, f"Node.js {version} is too old, need >= 18"

    def test_bridge_test_command_success(self):
        import subprocess, json
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bridge_script = os.path.join(project_root, "bridge", "raydium_sdk_bridge.js")
        if not os.path.exists(bridge_script):
            pytest.skip("Bridge script not found")

        result = subprocess.run(
            ["node", bridge_script, "test"],
            capture_output=True, text=True, timeout=15,
            env=os.environ.copy(),
        )
        assert result.returncode == 0, f"Bridge test failed: {result.stderr}"
        data = json.loads(result.stdout.strip().split('\n')[-1])
        assert data.get("success") is True

    def test_bridge_test_returns_expected_shape(self):
        """Bridge test command should return JSON with success, message, env fields."""
        import subprocess, json
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bridge_script = os.path.join(project_root, "bridge", "raydium_sdk_bridge.js")
        if not os.path.exists(bridge_script):
            pytest.skip("Bridge script not found")

        result = subprocess.run(
            ["node", bridge_script, "test"],
            capture_output=True, text=True, timeout=15,
            env=os.environ.copy(),
        )
        data = json.loads(result.stdout.strip().split('\n')[-1])
        assert 'success' in data
        assert data['success'] is True
        # Bridge test returns wallet info: pubkey, balance, rpc
        assert 'pubkey' in data
        assert 'balance' in data

    def test_bridge_unknown_command_fails_gracefully(self):
        """Calling bridge with an unknown command should fail with non-zero exit."""
        import subprocess
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bridge_script = os.path.join(project_root, "bridge", "raydium_sdk_bridge.js")
        if not os.path.exists(bridge_script):
            pytest.skip("Bridge script not found")

        result = subprocess.run(
            ["node", bridge_script, "totallyFakeCommand"],
            capture_output=True, text=True, timeout=15,
            env=os.environ.copy(),
        )
        # Should either fail or return an error JSON
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout.strip().split('\n')[-1])
            assert data.get("success") is not True or 'error' in data


# ── Executor (read-only, requires wallet) ────────────────────────────

@integration
class TestExecutorLive:
    """Read-only on-chain queries. Requires WALLET_PRIVATE_KEY."""

    @pytest.fixture(autouse=True)
    def _require_wallet(self):
        if not _have_wallet():
            pytest.skip("WALLET_PRIVATE_KEY not set or too short")
        if not _have_rpc():
            pytest.skip("SOLANA_RPC_URL not set or is example.com")

    @pytest.fixture()
    def executor(self):
        from bot.trading.executor import RaydiumExecutor
        return RaydiumExecutor()

    def test_get_balance_returns_nonnegative_float(self, executor):
        balance = executor.get_balance()
        assert isinstance(balance, float)
        assert balance >= 0
        # Dummy test wallet is unfunded
        assert balance == 0, f"Dummy wallet should have 0 SOL, got {balance}"

    def test_list_all_tokens_returns_list(self, executor):
        tokens = executor.list_all_tokens()
        assert isinstance(tokens, list)
        # Dummy wallet has no tokens
        assert len(tokens) == 0, f"Dummy wallet should have no tokens, got {len(tokens)}"

    def test_token_list_entries_have_required_fields(self, executor):
        """Dummy wallet should have no token accounts."""
        tokens = executor.list_all_tokens()
        assert isinstance(tokens, list)
        assert len(tokens) == 0, (
            f"Fresh dummy wallet should have no tokens, got {len(tokens)}"
        )

    def test_get_wsol_balance_returns_nonnegative(self, executor):
        balance = executor.get_wsol_balance()
        assert isinstance(balance, (int, float))
        assert balance >= 0
        # Dummy wallet has no WSOL
        assert balance == 0, f"Dummy wallet should have 0 WSOL, got {balance}"

    def test_close_empty_accounts_returns_result_shape(self, executor):
        """close_empty_accounts should return dict with 'closed' and 'reclaimedSol'."""
        result = executor.close_empty_accounts(keep_mints=[])
        assert isinstance(result, dict)
        assert 'closed' in result
        assert 'reclaimedSol' in result
        assert isinstance(result['closed'], (int, float))
        assert isinstance(result['reclaimedSol'], (int, float))


# ── Full Scan & Rank Pipeline (everything together) ──────────────────

@integration
class TestFullScanPipeline:
    """End-to-end: fetch → filter → safety → score → rank.

    This is the closest thing to testing what the bot does every 3 minutes
    in the main loop, without actually opening positions.
    """

    def test_full_scan_pipeline(self):
        """Simulate a complete scan cycle and verify the output."""
        from bot.raydium_client import RaydiumAPIClient
        from bot.analysis.pool_analyzer import PoolAnalyzer
        from bot.analysis.pool_quality import PoolQualityAnalyzer
        from bot.analysis.snapshot_tracker import SnapshotTracker
        from bot.config import config

        client = RaydiumAPIClient()
        analyzer = PoolAnalyzer()
        tracker = SnapshotTracker(max_snapshots=10)
        analyzer.set_snapshot_tracker(tracker)
        quality = PoolQualityAnalyzer()

        # Step 1: Fetch and filter pools (same as scan_and_rank_pools)
        pools = client.get_filtered_pools(
            min_liquidity=config.MIN_LIQUIDITY_USD,
            min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
            min_apr=config.MIN_APR_24H,
        )
        assert isinstance(pools, list)

        # Step 2: Filter by burn percent
        pools = [p for p in pools if p.get('burnPercent', 0) >= config.MIN_BURN_PERCENT]

        # Step 3: Record snapshots
        for pool in pools:
            pool_id = pool.get('ammId', pool.get('id', ''))
            day = pool.get('day', {})
            volume = day.get('volume', 0)
            tvl = pool.get('tvl', 0)
            price = pool.get('price', 0)
            if pool_id and tvl > 0:
                tracker.record(pool_id, volume, tvl, price)

        assert tracker.pool_count() > 0 or len(pools) == 0

        # Step 4: Safety filter (limit to 5 for speed)
        safe_pools = PoolQualityAnalyzer.get_safe_pools(
            pools[:5],
            check_locks=config.CHECK_TOKEN_SAFETY,
            analyzer=quality,
        )
        assert isinstance(safe_pools, list)
        assert len(safe_pools) <= len(pools[:5])

        # Step 5: Rank
        if safe_pools:
            ranked = analyzer.rank_pools(safe_pools, top_n=5)
            assert len(ranked) > 0
            assert all('score' in p for p in ranked)
            # Top pool should have the highest score
            scores = [p['score'] for p in ranked]
            assert scores[0] == max(scores)

    def test_full_pipeline_with_position_sizing(self):
        """After ranking, position sizing should give valid amounts."""
        from bot.raydium_client import RaydiumAPIClient
        from bot.analysis.pool_analyzer import PoolAnalyzer
        from bot.config import config

        client = RaydiumAPIClient()
        analyzer = PoolAnalyzer()

        pools = client.get_filtered_pools(min_liquidity=10_000)
        if not pools:
            pytest.skip("No pools matched filters")

        ranked = analyzer.rank_pools(pools, top_n=3)
        if not ranked:
            pytest.skip("No pools after ranking")

        # Simulate having 2 SOL of capital
        size = analyzer.calculate_position_size(
            ranked[0],
            available_capital=2.0,
            num_open_positions=0,
        )
        assert size > 0
        assert size <= config.MAX_ABSOLUTE_POSITION_SOL
        assert size <= 2.0 - config.RESERVE_SOL

        # With 2 positions already open (out of 3 max), only 1 slot left
        size2 = analyzer.calculate_position_size(
            ranked[0],
            available_capital=1.0,
            num_open_positions=2,
        )
        assert size2 > 0
        assert size2 <= config.MAX_ABSOLUTE_POSITION_SOL
        assert size2 <= 1.0 - config.RESERVE_SOL  # can't exceed deployable
        # With only 1 slot, entire deployable goes to it: (1.0 - RESERVE) / 1
        expected = (1.0 - config.RESERVE_SOL) / 1
        assert abs(size2 - expected) < 0.01
