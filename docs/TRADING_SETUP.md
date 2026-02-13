# Trading Setup Guide

## ⚠️ WARNING: REAL MONEY

This bot executes **real transactions** on Solana mainnet via the Raydium SDK. Read this guide before enabling live trading.

---

## Prerequisites

1. **Funded Solana Wallet**
   - Create a new wallet (Phantom, Solflare, or CLI)
   - Fund with SOL for trading capital + transaction fees (~0.000005 SOL per tx)

2. **Reliable RPC Endpoint**
   - Free RPC (`api.mainnet-beta.solana.com`) has strict rate limits
   - Recommended paid RPCs: [Helius](https://helius.xyz), [QuickNode](https://quicknode.com)

3. **Private Key**
   - Export as Base58 string from your wallet
   - Store in `.env` file (never commit to git)

---

## Configuration

### 1. Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:
```env
WALLET_PRIVATE_KEY=your_base58_private_key_here
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### 2. Trading Parameters

Edit `bot/config.py`:

```python
# Enable real trading
TRADING_ENABLED: bool = True
DRY_RUN: bool = False

# Position sizing
MAX_ABSOLUTE_POSITION_SOL: float = 5.0   # Hard cap per position (SOL)
MAX_CONCURRENT_POSITIONS: int = 5         # Max active positions
RESERVE_PERCENT: float = 0.20            # Keep 20% capital in reserve

# Risk management
STOP_LOSS_PERCENT: float = -2.0          # Exit at -2% loss
TAKE_PROFIT_PERCENT: float = 5.0         # Exit at +5% profit
MAX_HOLD_TIME_HOURS: int = 24            # Force exit after 24h
MAX_IMPERMANENT_LOSS: float = -3.0       # Exit if IL > 3%

# Pool filtering
MIN_LIQUIDITY_USD: float = 10_000        # Min $10k TVL
MIN_BURN_PERCENT: float = 50.0           # Min 50% LP tokens burned
MIN_APR_24H: float = 5.0                 # Min 5% APR
MIN_VOLUME_TVL_RATIO: float = 0.5        # Volume > 50% of TVL
```

---

## Testing Procedure

### 1. Test Wallet Connection

```bash
./venv/bin/python tests/test_wallet.py
```

Expected: wallet address, SOL balance, pool scanning results.

### 2. Test Pool Discovery

```bash
./venv/bin/python tests/test_trading.py
```

Expected: V3 API fetches WSOL pools, filters by TVL/APR/burn, scores top pools.

### 3. Test Node.js Bridge

```bash
./venv/bin/python tests/test_bridge.py
```

Expected: Bridge script accessible, Node.js + Raydium SDK installed.

### 4. Paper Trading

Run with `DRY_RUN: bool = True`:

```bash
./venv/bin/python run.py
```

Monitor for 24+ hours. Verify:
- Pools discovered and scored correctly
- Simulated positions open/close as expected
- Stop-loss and take-profit triggers work

### 5. Small Live Test

```python
# Start tiny
MAX_ABSOLUTE_POSITION_SOL: float = 0.5   # ~$75
MAX_CONCURRENT_POSITIONS: int = 1         # One position only
```

Run for 24 hours and verify positions match on-chain state.

---

## How Transactions Work

```
Python Bot → Node.js Bridge → Raydium SDK → Solana
```

1. Bot finds pool with score > 70
2. Calculates position size (score-based, capped)
3. Calls `node bridge/raydium_sdk_bridge.js add <pool_id> <amount> <wallet_key>`
4. SDK builds, signs, and submits transaction
5. Bot tracks position and monitors for exit conditions
6. On exit: `node bridge/raydium_sdk_bridge.js remove <pool_id> <amount> <wallet_key>`

---

## Safety Checklist

Before enabling live trading:

- [ ] Private key in `.env`, `.env` in `.gitignore`
- [ ] Using paid RPC endpoint
- [ ] Wallet funded with SOL
- [ ] Position size limits set conservatively
- [ ] Paper traded for 24+ hours
- [ ] Stop-loss and take-profit configured
- [ ] `MIN_BURN_PERCENT` ≥ 50 (LP burn verification)
- [ ] Understand you can lose everything

---

## Emergency Exit

```bash
# Kill the bot
pkill -f "python run.py"
# Or Ctrl+C in terminal
```

Manual liquidity removal: go to [raydium.io/liquidity](https://raydium.io/liquidity/), connect wallet, remove manually.

---

## ⚠️ Disclaimer

High-APR WSOL pools are mostly meme tokens with extreme volatility. This is not financial advice. Never invest more than you can afford to lose.
