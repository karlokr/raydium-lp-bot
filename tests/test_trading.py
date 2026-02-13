#!/usr/bin/env python3
"""
Trading functionality test script.
Tests the full trading pipeline from pool analysis to transaction execution.
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.trading.executor import RaydiumExecutor
from bot.raydium_client import RaydiumAPIClient
from bot.config import config

print("=" * 70)
print("TRADING FUNCTIONALITY TEST")
print("=" * 70)

# Test 1: Node.js SDK Bridge
print("\n1. Testing Node.js SDK Bridge...")
try:
    import subprocess
    result = subprocess.run(
        ['node', '--version'],
        capture_output=True,
        text=True,
        timeout=5
    )
    if result.returncode == 0:
        print(f"   ✓ Node.js installed: {result.stdout.strip()}")
    else:
        print("   ✗ Node.js not found")
        sys.exit(1)
except Exception as e:
    print(f"   ✗ Error: {e}")
    sys.exit(1)

# Test 2: Raydium SDK
print("\n2. Testing Raydium SDK installation...")
try:
    result = subprocess.run(
        ['node', '-e', 'require("@raydium-io/raydium-sdk"); console.log("OK")'],
        capture_output=True,
        text=True,
        timeout=5
    )
    if "OK" in result.stdout:
        print("   ✓ Raydium SDK installed")
    else:
        print("   ✗ Raydium SDK not found")
        print("   Run: npm install")
        sys.exit(1)
except Exception as e:
    print(f"   ✗ Error: {e}")
    sys.exit(1)

# Test 3: Configuration
print("\n3. Configuration Check...")
print(f"   Trading enabled: {config.TRADING_ENABLED}")
print(f"   Dry run mode: {config.DRY_RUN}")
print(f"   WSOL pairs only: {config.REQUIRE_WSOL_PAIRS}")
print(f"   Reserve percent: {config.RESERVE_PERCENT * 100}%")

if config.TRADING_ENABLED and not config.DRY_RUN:
    print("\n   ⚠️  WARNING: LIVE TRADING MODE ENABLED!")
    response = input("   Continue with live trading test? (yes/no): ")
    if response.lower() != 'yes':
        print("   Aborted.")
        sys.exit(0)

# Test 4: Wallet Connection
print("\n4. Wallet Connection...")
executor = None
try:
    executor = RaydiumExecutor()
    print(f"   ✓ Wallet: {executor.wallet.pubkey()}")
    
    balance = executor.get_balance()
    print(f"   ✓ Balance: {balance:.4f} SOL")
    
    if balance < 0.01:
        print("   ⚠️  Low SOL balance - may not be able to pay transaction fees")
    
except Exception as e:
    print(f"   ✗ Wallet error: {e}")
    print("   Make sure WALLET_PRIVATE_KEY is set in .env")
    if not config.DRY_RUN:
        sys.exit(1)

# Test 5: Pool Discovery
print("\n5. Pool Discovery...")
pools = []
top = None
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
        print(f"\n   Top pool for testing:")
        print(f"   - Name: {top['name']}")
        print(f"   - AMM ID: {top['ammId']}")
        print(f"   - TVL: ${top.get('tvl', 0):,.2f}")
        print(f"   - APR: {top.get('apr24h', 0):.1f}%")
        print(f"   - LP Burn: {top.get('burnPercent', 0):.1f}%")
        
except Exception as e:
    print(f"   ✗ Error: {e}")
    sys.exit(1)

# Test 6: Transaction Simulation (Dry Run)
print("\n6. Transaction Simulation...")
if config.DRY_RUN and executor and top:
    print("   Testing add_liquidity in dry-run mode...")
    try:
        signature = executor.add_liquidity(
            pool_id=top['ammId'],
            token_a_amount=0.01,
            token_b_amount=0.01,
            slippage=0.01
        )
        if signature:
            print(f"   ✓ Simulation successful: {signature}")
        else:
            print("   ✗ Simulation failed")
    except Exception as e:
        print(f"   ✗ Error: {e}")
else:
    print("   ⚠️  SKIPPING - Live trading mode enabled")
    print("   Would execute REAL transactions on mainnet!")

# Summary
print("\n" + "=" * 70)
print("TEST SUMMARY")
print("=" * 70)

checks = [
    ("Node.js installed", True),
    ("Raydium SDK installed", True),
    ("Wallet configured", executor is not None),
    ("Pools discoverable", len(pools) > 0),
]

all_good = True
for check, status in checks:
    symbol = "✓" if status else "✗"
    print(f"{symbol} {check}")
    if not status:
        all_good = False

print("\n" + "=" * 70)
if all_good:
    print("✓ ALL TESTS PASSED")
    print("\nYou can now:")
    if config.DRY_RUN:
        print("  1. Run paper trading: python run.py")
        print("  2. To enable live trading:")
        print("     - Set TRADING_ENABLED=True in bot/config.py")
        print("     - Set DRY_RUN=False in bot/config.py")
        print("     - Fund wallet with SOL and tokens")
        print("     - Test on devnet first!")
    else:
        print("  ⚠️  LIVE TRADING ENABLED - Bot will execute real transactions!")
        print("  Run: python run.py")
else:
    print("✗ SOME TESTS FAILED")
    print("\nFix the issues above before running the bot.")

print("=" * 70)
