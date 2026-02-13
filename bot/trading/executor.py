"""
Raydium Transaction Executor

Handles liquidity transactions via the Node.js SDK bridge.
All transaction building/signing/sending is done by the bridge script.
"""
import os
import subprocess
import json
from typing import Optional
from dotenv import load_dotenv

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58

from bot.config import config


load_dotenv()


class RaydiumExecutor:
    """
    Executes liquidity provision transactions on Raydium.
    Uses the Node.js bridge for transaction building/signing.

    WARNING: This handles REAL MONEY. Use with extreme caution!
    """

    RAYDIUM_AMM_PROGRAM = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")

    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or config.RPC_ENDPOINT
        self.client = Client(self.rpc_url)

        # Load wallet from environment
        private_key = os.getenv('WALLET_PRIVATE_KEY')
        if not private_key:
            raise ValueError("WALLET_PRIVATE_KEY not found in .env file!")

        try:
            if ',' in private_key:
                key_bytes = bytes([int(x.strip()) for x in private_key.strip('[]').split(',')])
            else:
                key_bytes = base58.b58decode(private_key)

            self.wallet = Keypair.from_bytes(key_bytes)
            print(f"✓ Wallet loaded: {self.wallet.pubkey()}")

        except Exception as e:
            raise ValueError(f"Failed to load wallet: {e}")

    def get_balance(self) -> float:
        """Get native SOL balance in wallet."""
        try:
            balance_lamports = self.client.get_balance(self.wallet.pubkey()).value
            return balance_lamports / 1e9
        except Exception as e:
            print(f"Error getting SOL balance: {e}")
            return 0.0

    def get_wsol_balance(self) -> float:
        """Get WSOL (wrapped SOL) balance via Node.js bridge."""
        try:
            wsol_mint = "So11111111111111111111111111111111111111112"
            result = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, 'balance', wsol_mint],
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                print(f"Error getting WSOL balance: {result.stderr}")
                return 0.0

            response = json.loads(result.stdout.strip().split('\n')[-1])
            raw_balance = int(response.get('balance', 0))
            return raw_balance / 1e9

        except Exception as e:
            print(f"Error getting WSOL balance: {e}")
            return 0.0

    def get_wallet_balances(self) -> dict:
        """Get all wallet balances (SOL + WSOL)."""
        sol = self.get_balance()
        wsol = self.get_wsol_balance()
        return {
            'sol': sol,
            'wsol': wsol,
            'total_sol': sol + wsol,
        }

    def add_liquidity(
        self,
        pool_id: str,
        token_a_amount: float,
        token_b_amount: float,
        slippage: float = 0.01,
    ) -> Optional[str]:
        """Add liquidity to a Raydium pool via the SDK bridge."""
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None

        if config.DRY_RUN:
            print(f"⚠️  Dry run: would add {token_a_amount:.6f} A + {token_b_amount:.6f} WSOL to {pool_id[:8]}...")
            return f"DRY_RUN_{pool_id[:8]}"

        try:
            result = subprocess.run(
                [
                    'node', config.BRIDGE_SCRIPT, 'add',
                    pool_id,
                    str(token_a_amount),
                    str(token_b_amount),
                    str(slippage * 100),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                print(f"✗ Transaction failed: {result.stderr}")
                return None

            response = json.loads(result.stdout.strip().split('\n')[-1])

            if response.get('success'):
                signatures = response.get('signatures', [])
                main_sig = signatures[0] if signatures else 'unknown'
                print(f"✓ Liquidity added: {main_sig}")
                return main_sig
            else:
                print(f"✗ Transaction failed: {response.get('error')}")
                return None

        except subprocess.TimeoutExpired:
            print("✗ Transaction timeout")
            return None
        except Exception as e:
            print(f"✗ Error: {e}")
            return None

    def remove_liquidity(
        self,
        pool_id: str,
        lp_token_amount: float,
        slippage: float = 0.01,
    ) -> Optional[str]:
        """Remove liquidity from a Raydium pool via the SDK bridge."""
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None

        if config.DRY_RUN:
            print(f"⚠️  Dry run: would remove {lp_token_amount:.6f} LP from {pool_id[:8]}...")
            return f"DRY_RUN_{pool_id[:8]}"

        try:
            result = subprocess.run(
                [
                    'node', config.BRIDGE_SCRIPT, 'remove',
                    pool_id,
                    str(lp_token_amount),
                    str(slippage * 100),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                print(f"✗ Transaction failed: {result.stderr}")
                return None

            response = json.loads(result.stdout.strip().split('\n')[-1])

            if response.get('success'):
                signatures = response.get('signatures', [])
                main_sig = signatures[0] if signatures else 'unknown'
                print(f"✓ Liquidity removed: {main_sig}")
                return main_sig
            else:
                print(f"✗ Transaction failed: {response.get('error')}")
                return None

        except subprocess.TimeoutExpired:
            print("✗ Transaction timeout")
            return None
        except Exception as e:
            print(f"✗ Error: {e}")
            return None
