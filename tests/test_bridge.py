#!/usr/bin/env python3
"""
Quick test of the Node.js SDK bridge connection
"""
import subprocess
import json
import os
from dotenv import load_dotenv

load_dotenv()

# Resolve bridge script path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_SCRIPT = os.path.join(PROJECT_ROOT, 'bridge', 'raydium_sdk_bridge.js')

print("Testing Node.js SDK Bridge...\n")

env = os.environ.copy()

try:
    result = subprocess.run(
        ['node', BRIDGE_SCRIPT, 'test'],
        capture_output=True,
        text=True,
        timeout=10,
        env=env
    )
    
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)
    print("Return code:", result.returncode)
    
    if result.returncode == 0:
        response = json.loads(result.stdout.strip())
        if response.get('success'):
            print(f"\n✓ Bridge working!")
            print(f"  Wallet: {response['pubkey']}")
            print(f"  Balance: {response['balance']:.4f} SOL")
        else:
            print(f"\n✗ Bridge error: {response.get('error')}")
    else:
        print("\n✗ Bridge failed")
        
except Exception as e:
    print(f"\n✗ Error: {e}")
