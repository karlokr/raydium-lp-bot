"""
Raydium Transaction Executor
Handles actual liquidity provision transactions
"""
import os
import time
import subprocess
import json
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv

from solana.rpc.api import Client
from solders.transaction import Transaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
import base58

from bot.config import config


load_dotenv()


class RaydiumExecutor:
    """
    Executes liquidity provision transactions on Raydium.
    
    WARNING: This handles REAL MONEY. Use with extreme caution!
    """
    
    # Raydium Program IDs
    RAYDIUM_AMM_PROGRAM = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
    
    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or config.RPC_ENDPOINT
        self.client = Client(self.rpc_url)
        
        # Load wallet from environment
        private_key = os.getenv('WALLET_PRIVATE_KEY')
        if not private_key:
            raise ValueError("WALLET_PRIVATE_KEY not found in .env file!")
        
        try:
            if isinstance(private_key, str):
                if ',' in private_key:
                    key_bytes = bytes([int(x.strip()) for x in private_key.strip('[]').split(',')])
                else:
                    key_bytes = base58.b58decode(private_key)
            else:
                key_bytes = private_key
            
            self.wallet = Keypair.from_bytes(key_bytes)
            print(f"✓ Wallet loaded: {self.wallet.pubkey()}")
            
        except Exception as e:
            raise ValueError(f"Failed to load wallet: {e}")
    
    def _bridge_script(self) -> str:
        """Get the path to the Node.js bridge script"""
        return config.BRIDGE_SCRIPT

    def get_balance(self) -> float:
        """Get native SOL balance in wallet"""
        try:
            balance_lamports = self.client.get_balance(self.wallet.pubkey()).value
            return balance_lamports / 1e9
        except Exception as e:
            print(f"Error getting SOL balance: {e}")
            return 0.0

    def get_wsol_balance(self) -> float:
        """Get WSOL (wrapped SOL) balance in wallet via Node.js bridge"""
        try:
            wsol_mint = "So11111111111111111111111111111111111111112"
            env = os.environ.copy()

            result = subprocess.run(
                ['node', self._bridge_script(), 'balance', wsol_mint],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
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
        """Get all wallet balances (SOL + WSOL)"""
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
        slippage: float = 0.01
    ) -> Optional[str]:
        """
        Add liquidity to a Raydium pool using the TypeScript SDK bridge.
        """
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None
        
        if config.DRY_RUN:
            print("⚠️  Dry run mode - simulating transaction")
            print(f"   Would add: {token_a_amount:.6f} token A, {token_b_amount:.6f} WSOL")
            print(f"   Pool: {pool_id}")
            print(f"   Slippage: {slippage * 100}%")
            return f"DRY_RUN_{pool_id[:8]}"
        
        try:
            env = os.environ.copy()
            
            result = subprocess.run(
                [
                    'node',
                    self._bridge_script(),
                    'add',
                    pool_id,
                    str(token_a_amount),
                    str(token_b_amount),
                    str(slippage * 100)
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env
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
        slippage: float = 0.01
    ) -> Optional[str]:
        """
        Remove liquidity from a Raydium pool using the TypeScript SDK bridge.
        """
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None
        
        if config.DRY_RUN:
            print("⚠️  Dry run mode - simulating transaction")
            print(f"   Would remove: {lp_token_amount:.6f} LP tokens")
            print(f"   Pool: {pool_id}")
            print(f"   Slippage: {slippage * 100}%")
            return f"DRY_RUN_{pool_id[:8]}"
        
        try:
            env = os.environ.copy()
            
            result = subprocess.run(
                [
                    'node',
                    self._bridge_script(),
                    'remove',
                    pool_id,
                    str(lp_token_amount),
                    str(slippage * 100)
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env
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
    
    def simulate_transaction(self, transaction: Transaction) -> bool:
        """Simulate a transaction before sending."""
        try:
            result = self.client.simulate_transaction(transaction)
            if result.value.err:
                print(f"Transaction simulation failed: {result.value.err}")
                return False
            return True
        except Exception as e:
            print(f"Simulation error: {e}")
            return False
    
    def send_transaction_with_retry(
        self,
        transaction: Transaction,
        max_retries: int = 3
    ) -> Optional[str]:
        """Send transaction with retry logic."""
        for attempt in range(max_retries):
            try:
                if not self.simulate_transaction(transaction):
                    print(f"Simulation failed on attempt {attempt + 1}")
                    continue
                
                result = self.client.send_transaction(transaction, self.wallet)
                signature = str(result.value)
                
                print(f"✓ Transaction sent: {signature}")
                
                confirmed = self._wait_for_confirmation(signature)
                if confirmed:
                    return signature
                else:
                    print(f"Transaction not confirmed on attempt {attempt + 1}")
                    
            except Exception as e:
                print(f"Transaction failed on attempt {attempt + 1}: {e}")
                time.sleep(2)
        
        return None
    
    def _wait_for_confirmation(self, signature: str, timeout: int = 30) -> bool:
        """Wait for transaction confirmation"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                result = self.client.get_signature_statuses([signature])
                if result.value and result.value[0]:
                    status = result.value[0]
                    if status.confirmation_status:
                        print(f"✓ Transaction confirmed: {status.confirmation_status}")
                        return True
            except Exception as e:
                print(f"Error checking confirmation: {e}")
            
            time.sleep(2)
        
        print("⚠️  Transaction confirmation timeout")
        return False


if __name__ == "__main__":
    try:
        executor = RaydiumExecutor()
        balance = executor.get_balance()
        print(f"\nWallet Balance: {balance:.4f} SOL")
        
        if balance > 0:
            print("✓ Wallet is funded and ready")
        else:
            print("⚠️  Wallet has no SOL - add funds before trading")
            
    except Exception as e:
        print(f"✗ Failed to initialize executor: {e}")
        print("\nMake sure to:")
        print("1. Copy .env.example to .env")
        print("2. Add your WALLET_PRIVATE_KEY to .env")
        print("3. Fund the wallet with SOL")
