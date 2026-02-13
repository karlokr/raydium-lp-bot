#!/usr/bin/env python3
"""
Quick test script for wallet setup and WSOL filtering.
Run this to verify your environment is configured correctly.
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.trading.executor import RaydiumExecutor
from bot.raydium_client import RaydiumAPIClient
from bot.config import config

print("=" * 60)
print("LIQUIDITY BOT - SETUP TEST")
print("=" * 60)

# Test 1: Configuration
print("\n1. Configuration:")
print(f"   WSOL filtering: {config.REQUIRE_WSOL_PAIRS}")
print(f"   Trading enabled: {config.TRADING_ENABLED}")
print(f"   Dry run mode: {config.DRY_RUN}")
print(f"   Token safety checks: {config.CHECK_TOKEN_SAFETY}")

# Test 2: Wallet (if configured)
print("\n2. Wallet Connection:")
try:
    executor = RaydiumExecutor()
    balance = executor.get_balance()
    print(f"   ✓ Wallet: {executor.wallet.pubkey()}")
    print(f"   ✓ Balance: {balance:.4f} SOL")
    if balance > 0:
        print("   ✓ Wallet is funded")
    else:
        print("   ⚠️  Wallet has no SOL")
except Exception as e:
    print(f"   ✗ Wallet not configured: {e}")
    print("   → Add WALLET_PRIVATE_KEY to .env file")

# Test 3: Pool Discovery
print("\n3. WSOL Pool Discovery:")
try:
    client = RaydiumAPIClient()
    pools = client.get_filtered_pools(
        min_liquidity=config.MIN_LIQUIDITY_USD,
        min_volume_tvl_ratio=config.MIN_VOLUME_TVL_RATIO,
        min_apr=config.MIN_APR_24H,
    )
    print(f"   ✓ Found {len(pools)} qualifying WSOL pools")
    
    if pools:
        top = sorted(pools, key=lambda x: x.get('apr24h', 0), reverse=True)[0]
        print(f"\n   Top pool: {top['name']}")
        print(f"   - TVL: ${top.get('tvl', 0):,.2f}")
        print(f"   - APR: {top.get('apr24h', 0):.1f}%")
        tvl = top.get('tvl', 1) or 1
        print(f"   - Volume/TVL: {top.get('volume24h', 0) / tvl:.2f}x")
        print(f"   - LP Burn: {top.get('burnPercent', 0):.1f}%")
    else:
        print("   ⚠️  No pools match criteria")
        
except Exception as e:
    print(f"   ✗ Error: {e}")

# Summary
print("\n" + "=" * 60)
print("NEXT STEPS:")
print("=" * 60)

if config.TRADING_ENABLED:
    print("⚠️  WARNING: Trading is ENABLED!")
    print("   The bot will execute REAL transactions.")
    print("   Make sure you've:")
    print("   - Tested on devnet first")
    print("   - Set appropriate position limits")
    print("   - Have emergency exit plan")
else:
    print("✓ Trading is disabled (paper trading mode)")
    print("  To enable real trading:")
    print("  1. Read docs/TRADING_SETUP.md carefully")
    print("  2. Set TRADING_ENABLED=True in bot/config.py")
    print("  3. Set DRY_RUN=False in bot/config.py")

print("\nRun bot: python run.py")
print("=" * 60)
