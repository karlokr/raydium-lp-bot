"""
Raydium Transaction Executor

Handles liquidity transactions via the Node.js SDK bridge.
All transaction building/signing/sending is done by the bridge script.
"""
import os
import subprocess
import json
from typing import Dict, Optional
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

    def unwrap_wsol(self) -> float:
        """Unwrap all WSOL in the wallet back to native SOL.
        Returns the amount unwrapped, or 0 if nothing to unwrap."""
        try:
            result = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, 'unwrap'],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ.copy(),
            )
            response = None
            if result.stdout and result.stdout.strip():
                try:
                    response = json.loads(result.stdout.strip().split('\n')[-1])
                except json.JSONDecodeError:
                    pass

            if response and response.get('success'):
                return float(response.get('unwrapped', 0))
            return 0.0
        except Exception as e:
            print(f"⚠ Error unwrapping WSOL: {e}")
            return 0.0

    def get_token_balance(self, token_mint: str) -> float:
        """Get token balance (raw) for a given mint via bridge."""
        try:
            result = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, 'balance', token_mint],
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                return 0.0
            response = json.loads(result.stdout.strip().split('\n')[-1])
            return float(response.get('balance', 0))
        except Exception as e:
            print(f"Error getting token balance: {e}")
            return 0.0

    def get_lp_value_sol(self, pool_id: str, lp_mint: str) -> Dict:
        """Get on-chain LP token value and current price ratio.
        
        Returns dict with:
          - valueSol: total SOL value of our LP tokens
          - priceRatio: current on-chain price (quoteReserve/baseReserve, human units)
        Returns empty dict on failure.
        """
        try:
            result = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, 'lpvalue', pool_id, lp_mint],
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                return {}
            response = json.loads(result.stdout.strip().split('\n')[-1])
            value_sol = float(response.get('valueSol', 0))
            price_ratio = float(response.get('priceRatio', 0))
            if value_sol > 0:
                return {'valueSol': value_sol, 'priceRatio': price_ratio}
            return {}
        except Exception:
            return {}

    def swap_tokens(
        self,
        pool_id: str,
        amount_in: float,
        direction: str = 'buy',
        slippage: float = None,
    ) -> Optional[str]:
        """
        Swap tokens via a Raydium AMM pool.
        direction='buy': swap WSOL -> base token
        direction='sell': swap base token -> WSOL
        """
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None

        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0

        if config.DRY_RUN:
            print(f"⚠️  Dry run: would swap {amount_in:.6f} ({direction}) via {pool_id[:8]}...")
            return f"DRY_RUN_SWAP_{pool_id[:8]}"

        try:
            result = subprocess.run(
                [
                    'node', config.BRIDGE_SCRIPT, 'swap',
                    pool_id,
                    str(amount_in),
                    str(slippage * 100),
                    direction,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=os.environ.copy(),
            )

            # Show bridge debug output
            if result.stderr and result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  [bridge] {line}")

            response = None
            if result.stdout and result.stdout.strip():
                try:
                    response = json.loads(result.stdout.strip().split('\n')[-1])
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                error_msg = response.get('error', result.stderr) if response else result.stderr
                print(f"✗ Swap failed: {error_msg}")
                return None

            if response and response.get('success'):
                signatures = response.get('signatures', [])
                main_sig = signatures[0] if signatures else 'unknown'
                print(f"✓ Swap executed: {main_sig}")
                return main_sig
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response from bridge'
                print(f"✗ Swap failed: {error_msg}")
                return None

        except subprocess.TimeoutExpired:
            print("✗ Swap timeout")
            return None
        except Exception as e:
            print(f"✗ Swap error: {e}")
            return None

    def add_liquidity(
        self,
        pool_id: str,
        token_a_amount: float,
        token_b_amount: float,
        slippage: float = None,
    ) -> Optional[Dict]:
        """Add liquidity to a Raydium pool via the SDK bridge.
        
        Returns dict with 'signature' and 'lpMint' on success, or None on failure.
        """
        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None

        if config.DRY_RUN:
            print(f"⚠️  Dry run: would add {token_a_amount:.6f} A + {token_b_amount:.6f} WSOL to {pool_id[:8]}...")
            return {'signature': f"DRY_RUN_{pool_id[:8]}", 'lpMint': ''}

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

            # Show bridge debug output
            if result.stderr and result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  [bridge] {line}")

            response = None
            if result.stdout and result.stdout.strip():
                try:
                    response = json.loads(result.stdout.strip().split('\n')[-1])
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                error_msg = response.get('error', result.stderr) if response else result.stderr
                print(f"✗ Transaction failed: {error_msg}")
                return None

            if response and response.get('success'):
                signatures = response.get('signatures', [])
                main_sig = signatures[0] if signatures else 'unknown'
                lp_mint = response.get('lpMint', '')
                print(f"✓ Liquidity added: {main_sig}")
                return {'signature': main_sig, 'lpMint': lp_mint}
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response from bridge'
                print(f"✗ Transaction failed: {error_msg}")
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
        slippage: float = None,
    ) -> Optional[str]:
        """Remove liquidity from a Raydium pool via the SDK bridge."""
        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0
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

            # Show bridge debug output
            if result.stderr and result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  [bridge] {line}")

            response = None
            if result.stdout and result.stdout.strip():
                try:
                    response = json.loads(result.stdout.strip().split('\n')[-1])
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                error_msg = response.get('error', result.stderr) if response else result.stderr
                print(f"✗ Transaction failed: {error_msg}")
                return None

            if response and response.get('success'):
                signatures = response.get('signatures', [])
                main_sig = signatures[0] if signatures else 'unknown'
                print(f"✓ Liquidity removed: {main_sig}")
                return main_sig
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response from bridge'
                print(f"✗ Transaction failed: {error_msg}")
                return None

        except subprocess.TimeoutExpired:
            print("✗ Transaction timeout")
            return None
        except Exception as e:
            print(f"✗ Error: {e}")
            return None

    def list_all_tokens(self) -> list:
        """List all non-zero token accounts in the wallet via bridge.
        Returns list of dicts with 'mint' and 'balance' (raw string)."""
        try:
            result = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, 'listtokens'],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                return []
            response = json.loads(result.stdout.strip().split('\n')[-1])
            if response and response.get('success'):
                return response.get('tokens', [])
            return []
        except Exception as e:
            print(f"⚠ Error listing tokens: {e}")
            return []
