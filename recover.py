"""
Recovery Script — Exit ALL LP positions and convert everything back to SOL.

This script:
1. Removes liquidity from all 5 pools where we hold LP tokens
2. Swaps all remaining non-SOL tokens back to SOL
3. Unwraps any WSOL back to native SOL

Run: python recover.py
"""
import subprocess
import json
import os
import time
import sys
from dotenv import load_dotenv

# Load .env so bridge subprocess gets the wallet key + RPC URL
load_dotenv()

# Project root for bridge path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(PROJECT_ROOT, 'bridge', 'raydium_sdk_bridge.js')

# LP positions to exit: (pool_id, lp_amount, lp_decimals, name)
POSITIONS = [
    ("9ETGHgMK35JxPgyk8j5utvtpa12y3QJWnEYNifcVcWFU", 0.907920, 9, "WSOL/Slinks"),
    ("3kApH42dyJRtk3DUDyfJJx3TGkPAK23ojMbJiBhrEQrX", 0.254326, 9, "WSOL/TULSA"),
    ("2QtkjW4vLYFx7ffTDaXd7ZvZ8Dx8ewhAxs8jfQ4y9wt5", 1.066469, 9, "WSOL/Goku"),
    ("H2uJCdsvuKdYbme5DneebrxNAVZcyCN5J9gmBmNWWP8b", 286.656668, 6, "ELON/WSOL"),
    ("81AHsVyKA89mmp3aQnQtUnfxGvgM6ZnFCdtnaT9Fs5tc", 211.177989, 6, "CORAL/WSOL"),
]

# Non-SOL token mints we might have after removing liquidity (to swap back)
# These will be detected automatically after each removeLiquidity


def run_bridge(args, timeout=90):
    """Run a bridge command, print stderr debug lines, return parsed JSON response."""
    cmd = ['node', BRIDGE] + args
    print(f"  $ node bridge {' '.join(args[:3])}...")
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    
    # Show bridge debug output
    if result.stderr and result.stderr.strip():
        for line in result.stderr.strip().split('\n'):
            if '[DEP0040]' not in line and 'trace-deprecation' not in line:
                print(f"  [bridge] {line}")
    
    response = None
    if result.stdout and result.stdout.strip():
        try:
            response = json.loads(result.stdout.strip().split('\n')[-1])
        except json.JSONDecodeError:
            pass
    
    return response, result.returncode


def remove_liquidity(pool_id, lp_amount, name):
    """Remove liquidity from a pool."""
    print(f"\n{'─' * 50}")
    print(f"Removing liquidity: {name} ({lp_amount} LP tokens)")
    print(f"Pool: {pool_id}")
    
    slippage = 10  # 10% slippage for recovery
    response, rc = run_bridge(['remove', pool_id, str(lp_amount), str(slippage)])
    
    if response and response.get('success'):
        sigs = response.get('signatures', [])
        print(f"  ✓ Liquidity removed: {sigs[0] if sigs else 'ok'}")
        return True
    else:
        error = response.get('error', 'Unknown error') if response else 'No response'
        print(f"  ✗ Failed: {error}")
        return False


def swap_all_to_sol(pool_id, name):
    """Swap all non-SOL tokens back to SOL (sell-all mode with amount=0)."""
    print(f"  Swapping remaining tokens → SOL...")
    
    slippage = 10  # 10% slippage for recovery
    response, rc = run_bridge(['swap', pool_id, '0', str(slippage), 'sell'])
    
    if response and response.get('success'):
        sigs = response.get('signatures', [])
        note = response.get('note', '')
        if note:
            print(f"  ✓ {note}")
        elif sigs:
            print(f"  ✓ Swap executed: {sigs[0]}")
        else:
            print(f"  ✓ Done")
        return True
    else:
        error = response.get('error', 'Unknown error') if response else 'No response'
        print(f"  ✗ Swap failed: {error}")
        return False


def unwrap_wsol():
    """Unwrap any remaining WSOL to native SOL."""
    print(f"\nUnwrapping WSOL → native SOL...")
    response, rc = run_bridge(['unwrap'])
    
    if response and response.get('success'):
        unwrapped = response.get('unwrapped', 0)
        if unwrapped > 0:
            print(f"  ✓ Unwrapped {unwrapped:.4f} WSOL")
        else:
            print(f"  ✓ No WSOL to unwrap")
        return True
    else:
        error = response.get('error', 'Unknown error') if response else 'No response'
        print(f"  ✗ {error}")
        return False


def get_sol_balance():
    """Get current native SOL balance."""
    response, rc = run_bridge(['test'])
    if response and response.get('success'):
        return response.get('balance', 0)
    return 0


def main():
    print("=" * 50)
    print("RECOVERY: Exiting all LP positions")
    print("=" * 50)
    
    starting_sol = get_sol_balance()
    print(f"Starting SOL balance: {starting_sol:.6f}")
    print(f"Positions to exit: {len(POSITIONS)}")
    
    input("\nPress Enter to start recovery (Ctrl+C to abort)... ")
    
    successes = 0
    failures = 0
    
    for pool_id, lp_amount, lp_decimals, name in POSITIONS:
        try:
            # Step 1: Remove liquidity
            ok = remove_liquidity(pool_id, lp_amount, name)
            if not ok:
                failures += 1
                continue
            
            time.sleep(3)  # Wait for on-chain state
            
            # Step 2: Swap remaining tokens back to SOL
            swap_all_to_sol(pool_id, name)
            
            time.sleep(2)
            successes += 1
            
        except subprocess.TimeoutExpired:
            print(f"  ✗ Timeout on {name}")
            failures += 1
        except Exception as e:
            print(f"  ✗ Error on {name}: {e}")
            failures += 1
    
    # Step 3: Unwrap any WSOL
    time.sleep(2)
    unwrap_wsol()
    
    # Final balance
    time.sleep(2)
    final_sol = get_sol_balance()
    
    print(f"\n{'=' * 50}")
    print(f"RECOVERY COMPLETE")
    print(f"{'=' * 50}")
    print(f"Positions exited: {successes}/{len(POSITIONS)}")
    if failures > 0:
        print(f"Failures: {failures} (may need manual intervention)")
    print(f"Starting SOL: {starting_sol:.6f}")
    print(f"Final SOL:    {final_sol:.6f}")
    recovered = final_sol - starting_sol
    print(f"Recovered:    {recovered:.6f} SOL")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
