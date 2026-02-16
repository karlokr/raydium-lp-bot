"""
Raydium Transaction Executor

Handles liquidity transactions via the Node.js SDK bridge.
All transaction building/signing/sending is done by the bridge script.
"""
import os
import subprocess
import json
from typing import Dict, Optional

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58

from bot.config import config


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

    # ── Bridge helpers ────────────────────────────────────────────────

    def _call_bridge(self, *args, timeout=15):
        """Call the Node.js bridge and return parsed JSON response, or None."""
        try:
            proc = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, *args],
                capture_output=True, text=True, timeout=timeout,
                env=os.environ.copy(),
            )
            if not proc.stdout or not proc.stdout.strip():
                return None
            resp = json.loads(proc.stdout.strip().split('\n')[-1])
            return resp if proc.returncode == 0 else None
        except Exception:
            return None

    def _bridge_tx(self, label, *args, timeout=60):
        """Execute a bridge transaction. Returns response dict or None."""
        try:
            proc = subprocess.run(
                ['node', config.BRIDGE_SCRIPT, *args],
                capture_output=True, text=True, timeout=timeout,
                env=os.environ.copy(),
            )
            resp = None
            if proc.stdout and proc.stdout.strip():
                try:
                    resp = json.loads(proc.stdout.strip().split('\n')[-1])
                except json.JSONDecodeError:
                    pass
            if proc.returncode != 0:
                err = (resp.get('error', proc.stderr) if resp else proc.stderr)
                print(f"\u2717 {label} failed: {err}")
                return None
            if resp and resp.get('success'):
                return resp
            err = (resp.get('error', 'Unknown error') if resp else 'No response')
            print(f"\u2717 {label} failed: {err}")
            return None
        except subprocess.TimeoutExpired:
            print(f"\u2717 {label} timeout")
            return None
        except Exception as e:
            print(f"\u2717 {label} error: {e}")
            return None

    # ── Read-only queries ─────────────────────────────────────────────

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
        resp = self._call_bridge('balance', 'So11111111111111111111111111111111111111112')
        if not resp:
            return 0.0
        return int(resp.get('balance', 0)) / 1e9

    def unwrap_wsol(self) -> float:
        """Unwrap all WSOL back to native SOL. Returns amount unwrapped."""
        resp = self._call_bridge('unwrap', timeout=30)
        if resp and resp.get('success'):
            return float(resp.get('unwrapped', 0))
        return 0.0

    def get_token_balance(self, token_mint: str) -> float:
        """Get token balance (raw) for a given mint via bridge."""
        resp = self._call_bridge('balance', token_mint)
        return float(resp.get('balance', 0)) if resp else 0.0

    def close_empty_accounts(self, keep_mints: list = None) -> dict:
        """Close empty token accounts to reclaim rent SOL."""
        args = ['closeaccounts']
        if keep_mints:
            args.append(','.join(keep_mints))
        resp = self._call_bridge(*args, timeout=60)
        if not resp:
            return {'closed': 0, 'reclaimedSol': 0}
        return {
            'closed': int(resp.get('closed', 0)),
            'reclaimedSol': float(resp.get('reclaimedSol', 0)),
        }

    def get_lp_value_sol(self, pool_id: str, lp_mint: str) -> Dict:
        """Get on-chain LP token value. Returns {} on failure."""
        resp = self._call_bridge('lpvalue', pool_id, lp_mint)
        if not resp:
            return {}
        return {
            'valueSol': float(resp.get('valueSol', 0)),
            'priceRatio': float(resp.get('priceRatio', 0)),
            'lpBalance': int(resp.get('lpBalance', 0)),
        }

    def batch_get_lp_values(self, positions: list) -> Dict[str, Dict]:
        """Batch LP value lookup — 2 RPC calls instead of 6 per position."""
        if not positions:
            return {}
        entries = [{'poolId': p['pool_id'], 'lpMint': p['lp_mint']} for p in positions]
        resp = self._call_bridge('batchlpvalue', json.dumps(entries), timeout=20)
        if not resp:
            return {}
        return {
            pid: {
                'valueSol': float(d.get('valueSol', 0)),
                'priceRatio': float(d.get('priceRatio', 0)),
                'lpBalance': int(d.get('lpBalance', 0)),
            }
            for pid, d in resp.get('results', {}).items()
        }

    # ── Transactions ─────────────────────────────────────────────────

    def swap_tokens(
        self,
        pool_id: str,
        amount_in: float,
        direction: str = 'buy',
        slippage: float = None,
    ) -> Optional[str]:
        """Swap tokens via a Raydium AMM pool."""
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None
        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0
        if config.DRY_RUN:
            print(f"⚠️  Dry run: would swap {amount_in:.6f} ({direction}) via {pool_id[:8]}...")
            return f"DRY_RUN_SWAP_{pool_id[:8]}"
        resp = self._bridge_tx('Swap', 'swap', pool_id, str(amount_in),
                               str(slippage * 100), direction)
        if resp:
            sig = (resp.get('signatures') or ['unknown'])[0]
            print(f"✓ Swap executed: {sig}")
            return sig
        return None

    def add_liquidity(
        self,
        pool_id: str,
        token_a_amount: float,
        token_b_amount: float,
        slippage: float = None,
    ) -> Optional[Dict]:
        """Add liquidity to a Raydium pool. Returns {signature, lpMint} or None."""
        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None
        if config.DRY_RUN:
            print(f"⚠️  Dry run: would add {token_a_amount:.6f} A + {token_b_amount:.6f} WSOL to {pool_id[:8]}...")
            return {'signature': f"DRY_RUN_{pool_id[:8]}", 'lpMint': ''}
        resp = self._bridge_tx('Add liquidity', 'add', pool_id,
                               str(token_a_amount), str(token_b_amount),
                               str(slippage * 100))
        if resp:
            sig = (resp.get('signatures') or ['unknown'])[0]
            print(f"✓ Liquidity added: {sig}")
            return {'signature': sig, 'lpMint': resp.get('lpMint', '')}
        return None

    def remove_liquidity(
        self,
        pool_id: str,
        lp_token_amount: float,
        slippage: float = None,
    ) -> Optional[str]:
        """Remove liquidity from a Raydium pool."""
        if slippage is None:
            slippage = config.SLIPPAGE_PERCENT / 100.0
        if not config.TRADING_ENABLED:
            print("⚠️  Trading disabled (TRADING_ENABLED=False)")
            return None
        if config.DRY_RUN:
            print(f"⚠️  Dry run: would remove {lp_token_amount:.6f} LP from {pool_id[:8]}...")
            return f"DRY_RUN_{pool_id[:8]}"
        resp = self._bridge_tx('Remove liquidity', 'remove', pool_id,
                               str(lp_token_amount), str(slippage * 100))
        if resp:
            sig = (resp.get('signatures') or ['unknown'])[0]
            print(f"✓ Liquidity removed: {sig}")
            return sig
        return None

    def list_all_tokens(self) -> list:
        """List all non-zero token accounts in the wallet."""
        resp = self._call_bridge('listtokens', timeout=30)
        if resp and resp.get('success'):
            return resp.get('tokens', [])
        return []
