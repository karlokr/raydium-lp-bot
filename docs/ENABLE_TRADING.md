# ✅ Enable Live Trading

The bot has **full transaction execution** via the Node.js Raydium SDK bridge.

---

## Current Status

| Component | Status |
|-----------|--------|
| Raydium V3 API (pool discovery) | ✅ Working |
| LP burn verification (burnPercent) | ✅ Working |
| RugCheck token safety | ✅ Working |
| Pool scoring (5-factor) | ✅ Working |
| Node.js + Raydium SDK bridge | ✅ Working |
| Wallet connection | ✅ Working |
| Paper trading mode | ✅ Working |
| Live trading | 🟡 Ready (enable the flags) |

---

## Step 1: Safety Check

Before going live:
- [ ] Paper traded for 24+ hours
- [ ] Understand how the bot enters/exits positions
- [ ] Wallet has sufficient SOL for fees + trading
- [ ] You're willing to risk the capital
- [ ] You have an emergency exit plan

## Step 2: Configure

Edit `bot/config.py`:
```python
TRADING_ENABLED: bool = True   # Enable real transactions
DRY_RUN: bool = False          # Disable paper trading
```

Start conservative:
```python
MAX_ABSOLUTE_POSITION_SOL: float = 0.5   # ~$75 max per position
MAX_CONCURRENT_POSITIONS: int = 1         # One position at a time
```

## Step 3: Run

```bash
./venv/bin/python run.py
```

## Step 4: Scale Gradually

If the small test works over 24-48 hours:
- Week 1: 1 SOL positions, 1-2 concurrent
- Week 2: 2 SOL positions, 2-3 concurrent
- Week 3: 5 SOL positions, 3-5 concurrent
- Monitor profitability at each stage

---

## Configuration Reference

### Position Sizing
| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_ABSOLUTE_POSITION_SOL` | 5.0 | Hard cap per position (SOL) |
| `MAX_CONCURRENT_POSITIONS` | 5 | Max simultaneous positions |
| `RESERVE_PERCENT` | 0.20 | Keep 20% capital in reserve |

### Risk Management
| Setting | Default | Description |
|---------|---------|-------------|
| `STOP_LOSS_PERCENT` | -2.0 | Exit at -2% loss |
| `TAKE_PROFIT_PERCENT` | 5.0 | Exit at +5% profit |
| `MAX_HOLD_TIME_HOURS` | 24 | Force exit after 24h |
| `MAX_IMPERMANENT_LOSS` | -3.0 | Exit if IL > 3% |

### Pool Filtering
| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_LIQUIDITY_USD` | 10,000 | Min pool TVL |
| `MIN_VOLUME_TVL_RATIO` | 0.5 | Volume ≥ 50% of TVL |
| `MIN_APR_24H` | 5.0 | Min 24h APR (%) |
| `MIN_BURN_PERCENT` | 50.0 | Min LP tokens burned (%) |
| `CHECK_TOKEN_SAFETY` | False | Enable RugCheck analysis |

---

## Monitoring

**Good signs:**
- Positions opening on high-score pools (> 70)
- Take-profit exits (+5%)
- Consistent positive P&L

**Bad signs:**
- Frequent stop-loss exits
- Transaction failures
- Excessive IL

**Emergency stop:**
```bash
pkill -f "python run.py"   # Or Ctrl+C
```

Then manually remove liquidity at [raydium.io/liquidity](https://raydium.io/liquidity/).

---

## Testing Commands

```bash
./venv/bin/python tests/test_wallet.py     # Wallet + pool scan
./venv/bin/python tests/test_trading.py    # Trading pipeline
./venv/bin/python tests/test_bridge.py     # Node.js bridge
```

---

## ⚠️ Warning

This bot trades real money on Solana mainnet. High-APR pools are mostly high-risk meme tokens. You can lose everything. This is not financial advice.
