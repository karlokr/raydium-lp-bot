"""
On-chain LP Lock Analysis

Queries Solana RPC to determine what percentage of a pool's LP tokens are
safely locked (burned, protocol-held, or in a time-lock contract) versus
sitting in regular wallets where holders can rug-pull.

Flow:
  1. getTokenSupply(lpMint)               → total LP supply
  2. getTokenLargestAccounts(lpMint)       → top ~20 LP holders
  3. getMultipleAccounts(holder accounts)  → owner program IDs
  4. Classify each holder as burned/protocol-locked/contract-locked/unlocked
  5. Return breakdown + safety verdict
"""
import time
import requests
from typing import Dict, Optional, List
from bot.config import config


# --- Well-known addresses for LP safety classification ---

# Burned / dead addresses — LP tokens sent here are gone forever
BURN_ADDRESSES = {
    "1111111111111111111111111111111111111111111",   # Solana null address
    "1nc1nerator11111111111111111111111111111111",   # Common incinerator
}

# Raydium protocol authority — holds initial LP that cannot be withdrawn
RAYDIUM_LP_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"

# Known time-lock / vesting programs on Solana.
# LP tokens owned by PDAs of these programs are contract-locked.
KNOWN_LOCKER_PROGRAMS = {
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m",   # Streamflow
    "LocpQgucEQHbqNABEYvBMrzJKjWcjEPPwd6i215cQ9a",    # Uncx / Liquidify (old)
    "2r5VekMNiWPzi1pWwvJczrdPaZnJG59u91unSrTunwJg",   # Jupiter Lock
    "FLockTopXvM3MRs5ThJTsSQDQNmzWfnj5s7xUQXKTc1v",   # Fluxbeam Locker
    "GJa1VEhNhjMEJoeqYyPvH5Ts9XadZAdFmRSi8ijrSU7G",   # Raydium LP Lock
}

# The System Program (owner of normal wallets / user accounts)
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Token Program (owner of SPL token accounts)
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class LiquidityLockAnalyzer:
    """Analyze LP token distribution on-chain to assess rug-pull risk."""

    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or config.RPC_ENDPOINT
        self._cache: Dict[str, tuple] = {}  # lp_mint -> (result, timestamp)
        self._cache_ttl = 300  # 5 minutes

    def _rpc_call(self, method: str, params: list) -> Optional[Dict]:
        """Make a single Solana JSON-RPC call."""
        try:
            resp = requests.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                print(f"  ⚠ RPC error ({method}): {data['error']}")
                return None
            return data.get("result")
        except Exception as e:
            print(f"  ⚠ RPC call failed ({method}): {e}")
            return None

    def analyze_lp_lock(self, lp_mint: str) -> Dict:
        """
        Analyze LP token distribution for a given LP mint.

        Returns:
            {
                'available': bool,
                'total_supply': int,          # raw lamports
                'burned_pct': float,          # % burned (dead addresses)
                'protocol_locked_pct': float, # % held by Raydium authority
                'contract_locked_pct': float, # % in known locker programs
                'unlocked_pct': float,        # % in regular wallets
                'safe_pct': float,            # burned + protocol + contract
                'max_single_unlocked_pct': float,  # largest unlocked holder
                'top_holders': list,          # classified holder details
                'is_safe': bool,              # passes thresholds
                'risks': list[str],           # reason(s) if not safe
            }
        """
        # Check cache
        if lp_mint in self._cache:
            cached_data, cached_time = self._cache[lp_mint]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data

        result = self._do_analyze(lp_mint)
        self._cache[lp_mint] = (result, time.time())
        return result

    def _do_analyze(self, lp_mint: str) -> Dict:
        """Perform the actual on-chain analysis."""

        unavailable = {
            'available': False,
            'total_supply': 0,
            'burned_pct': 0.0,
            'protocol_locked_pct': 0.0,
            'contract_locked_pct': 0.0,
            'unlocked_pct': 100.0,
            'safe_pct': 0.0,
            'max_single_unlocked_pct': 100.0,
            'top_holders': [],
            'is_safe': False,
            'risks': ['LP lock data unavailable'],
        }

        # Step 1: Get total LP supply
        supply_result = self._rpc_call("getTokenSupply", [lp_mint])
        if not supply_result:
            return unavailable

        supply_value = supply_result.get('value', {})
        total_supply_str = supply_value.get('amount', '0')
        try:
            total_supply = int(total_supply_str)
        except (ValueError, TypeError):
            return unavailable

        if total_supply == 0:
            return unavailable

        # Step 2: Get largest LP token holders (top ~20)
        holders_result = self._rpc_call(
            "getTokenLargestAccounts",
            [lp_mint, {"commitment": "confirmed"}],
        )
        if not holders_result:
            return unavailable

        holder_accounts = holders_result.get('value', [])
        if not holder_accounts:
            return unavailable

        # Step 3: Get account info for each holder (to find owner program)
        holder_addresses = [h['address'] for h in holder_accounts]
        owner_map = self._batch_get_account_owners(holder_addresses)

        # Step 4: Classify each holder
        # We have the token authority (wallet/PDA) for each holder.
        # Now we need a second lookup to see if any authority is a PDA
        # owned by a known locker program.
        authority_addresses = list(set(owner_map.values()))
        authority_addresses = [a for a in authority_addresses
                               if a not in ('unknown', SYSTEM_PROGRAM)
                               and a not in BURN_ADDRESSES
                               and a != RAYDIUM_LP_AUTHORITY]
        authority_owners = self._batch_get_authority_owners(authority_addresses)

        burned_amount = 0
        protocol_amount = 0
        contract_amount = 0
        unlocked_amount = 0
        max_single_unlocked = 0
        classified_holders = []

        for holder in holder_accounts:
            address = holder['address']
            amount_str = holder.get('amount', '0')
            try:
                amount = int(amount_str)
            except (ValueError, TypeError):
                continue

            if amount == 0:
                continue

            owner = owner_map.get(address, 'unknown')
            pct = (amount / total_supply) * 100

            # Classify based on the token authority
            if address in BURN_ADDRESSES or owner in BURN_ADDRESSES:
                category = 'burned'
                burned_amount += amount
            elif owner == SYSTEM_PROGRAM:
                # Token account closed / authority is system program → burned
                category = 'burned'
                burned_amount += amount
            elif owner == RAYDIUM_LP_AUTHORITY:
                category = 'protocol_locked'
                protocol_amount += amount
            elif owner in KNOWN_LOCKER_PROGRAMS:
                # Authority IS a known locker program directly
                category = 'contract_locked'
                contract_amount += amount
            elif authority_owners.get(owner) in KNOWN_LOCKER_PROGRAMS:
                # Authority is a PDA owned by a known locker program
                category = 'contract_locked'
                contract_amount += amount
            else:
                category = 'unlocked'
                unlocked_amount += amount
                if amount > max_single_unlocked:
                    max_single_unlocked = amount

            classified_holders.append({
                'address': address,
                'owner': owner,
                'amount': amount,
                'pct': round(pct, 2),
                'category': category,
            })

        # The top ~20 accounts may not cover 100% of supply.
        # Anything not in the top holders is by definition a small holder
        # (individually low risk). Treat uncovered portion as unlocked
        # but with no single-whale concern.
        covered_amount = burned_amount + protocol_amount + contract_amount + unlocked_amount
        uncovered_amount = total_supply - covered_amount
        if uncovered_amount > 0:
            unlocked_amount += uncovered_amount

        burned_pct = (burned_amount / total_supply) * 100
        protocol_pct = (protocol_amount / total_supply) * 100
        contract_pct = (contract_amount / total_supply) * 100
        unlocked_pct = (unlocked_amount / total_supply) * 100
        safe_pct = burned_pct + protocol_pct + contract_pct
        max_single_unlocked_pct = (max_single_unlocked / total_supply) * 100

        # Evaluate — note: this is the raw on-chain picture for REMAINING
        # circulating LP only. The API's burnPercent should be combined
        # with these numbers by the caller (pool_quality.py) for the
        # full safety picture.
        risks = []
        if safe_pct < config.MIN_SAFE_LP_PERCENT:
            risks.append(
                f"Only {safe_pct:.1f}% of remaining LP is locked "
                f"(min: {config.MIN_SAFE_LP_PERCENT}%)"
            )
        if max_single_unlocked_pct > config.MAX_SINGLE_LP_HOLDER_PERCENT:
            risks.append(
                f"Single wallet holds {max_single_unlocked_pct:.1f}% of remaining LP "
                f"(max: {config.MAX_SINGLE_LP_HOLDER_PERCENT}%)"
            )

        result = {
            'available': True,
            'total_supply': total_supply,
            'burned_pct': round(burned_pct, 2),
            'protocol_locked_pct': round(protocol_pct, 2),
            'contract_locked_pct': round(contract_pct, 2),
            'unlocked_pct': round(unlocked_pct, 2),
            'safe_pct': round(safe_pct, 2),
            'max_single_unlocked_pct': round(max_single_unlocked_pct, 2),
            'top_holders': classified_holders,
            'is_safe': len(risks) == 0,
            'risks': risks,
        }

        return result

    def _batch_get_account_owners(self, addresses: List[str]) -> Dict[str, str]:
        """
        Get the **token authority** (wallet/PDA that controls the tokens)
        for each token account address.

        Token accounts are always owned by the Token Program on-chain.
        The real owner/authority is in the parsed account data under
        `data.parsed.info.owner`.

        Uses getMultipleAccounts with jsonParsed encoding to extract this.
        Returns {token_account_address: authority_pubkey_str}.
        """
        if not addresses:
            return {}

        result = self._rpc_call(
            "getMultipleAccounts",
            [
                addresses,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        if not result:
            return {}

        owner_map = {}
        accounts = result.get('value', [])
        for addr, acct in zip(addresses, accounts):
            if acct is None:
                # Account doesn't exist on-chain — closed, likely burned
                owner_map[addr] = SYSTEM_PROGRAM
            else:
                # For SPL token accounts, the real authority is in
                # data.parsed.info.owner (the wallet/PDA that controls it).
                # The top-level acct['owner'] is always the Token Program.
                data = acct.get('data', {})
                if isinstance(data, dict) and 'parsed' in data:
                    info = data['parsed'].get('info', {})
                    token_authority = info.get('owner', 'unknown')
                    owner_map[addr] = token_authority
                else:
                    # Fallback to program owner if not parsed
                    owner_map[addr] = acct.get('owner', 'unknown')

        return owner_map

    def _batch_get_authority_owners(self, authority_addresses: List[str]) -> Dict[str, str]:
        """
        For each token authority address, determine what program owns it.

        This is the second-level lookup: if a token's authority is a PDA
        (program-derived address), we check whether its on-chain account
        is owned by a known locker program.

        Returns {authority_address: owning_program_pubkey}.
        Regular wallets will show System Program. PDAs will show their
        parent program.
        """
        if not authority_addresses:
            return {}

        result = self._rpc_call(
            "getMultipleAccounts",
            [
                authority_addresses,
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        )
        if not result:
            return {}

        owner_map = {}
        accounts = result.get('value', [])
        for addr, acct in zip(authority_addresses, accounts):
            if acct is None:
                # No on-chain account — regular wallet (unfunded or system)
                owner_map[addr] = SYSTEM_PROGRAM
            else:
                owner_map[addr] = acct.get('owner', 'unknown')

        return owner_map