# Raydium LP Bot

**The first open-source LP fee farming bot for Solana.**

Autonomously discovers, evaluates, and provides liquidity to high-yield Raydium pools. Actively manages positions to farm trading fees while protecting against rug pulls and impermanent loss.

## Features

- **Raydium V3 API** - Paginated, mint-filtered pool discovery (WSOL pairs only)
- **Multi-Threaded Architecture** - Parallel position checks (1s), pool scanning, sequential buys, parallel sells
- **Multi-Layer Safety** - LP burn verification, on-chain lock analysis, and RugCheck token scoring
- **Pool Scoring** - 5-factor weighted scoring: APR, Vol/TVL, Liquidity, IL safety, LP Burn
- **Progressive Cooldowns** - Escalating cooldowns on stop losses (24h → 48h → permanent blacklist)
- **Raydium SDK Bridge** - Full transaction execution via Node.js + `@raydium-io/raydium-sdk`
- **Risk Management** - Stop-loss, take-profit, IL monitoring, time-based forced exit
- **State Persistence** - Positions, cooldowns, blacklists survive restarts
- **Paper Trading** - Dry-run mode for testing without real transactions

---

## Thank you for your support!

Donations are graciously accepted for your continuing support in the development of this software and others.

- **XMR**: 8BwmJHCfeaL9z3f1DwjStW7i1bvwKPL8oXhDnfcXbjRNSQAxVk9PVFv74SoFWVGWEVVQDCfb1bTsa1S53KP18zrwVizUeqe
- **BTC**: bc1q3frlupheaz79v88t4hc8lgzfqwy4nekvc4gtj7
- **ETH**: 0x9038E310D0a6B8E7819A8b7c33E53ebCF6964eF9
- **SOL**: Gzea6q2aBmpUUPMwCcAUUPsxGqSua5k6HT8PaGHD3ewn

---

## Architecture

### Threading Model

The bot uses a multi-threaded architecture to decouple position monitoring from display and trading:

| Thread | Responsibility | Frequency |
|--------|---------------|-----------|
| **Main** | Display status only | Every 4s (`DISPLAY_INTERVAL_SEC`) |
| **Position check** | Update positions, detect & execute exits | Every 1s (`POSITION_CHECK_INTERVAL_SEC`) |
| **Pool scan** | Discover & rank pools, queue buy orders | Every 180s (`POOL_SCAN_INTERVAL_SEC`) |
| **Buy worker** | Execute entries sequentially from queue | On demand |

- **Sells execute in parallel.** If multiple positions trigger exit simultaneously, they all fire via `ThreadPoolExecutor`
- **Buys execute sequentially.** Entries are queued and processed one at a time to avoid race conditions
- **Display is fully decoupled.** The main thread reads shared state without blocking on RPC calls
- All shared state is protected by a `threading.Lock`

### Transaction Flow

```
Python Bot (Analysis, Threading, Logic)
      ↓ IPC JSON
Node.js Bridge (Raydium SDK)
      ↓
Solana Blockchain
```

### Project Structure

```
raydium-lp-bot/
├── run.py                      # Entry point
├── recover.py                  # Manual LP position recovery utility
├── bot/
│   ├── config.py               # Central BotConfig dataclass
│   ├── main.py                 # Threaded orchestration (4 worker threads)
│   ├── raydium_client.py       # Raydium V3 API client
│   ├── state.py                # State persistence (JSON)
│   ├── analysis/
│   │   ├── pool_analyzer.py    # 5-factor pool scoring
│   │   ├── pool_quality.py     # Risk assessment (burn + RugCheck + LP lock)
│   │   ├── price_tracker.py    # Price from pool reserve ratios
│   │   └── snapshot_tracker.py # Historical pool state tracking
│   ├── safety/
│   │   ├── rugcheck.py         # RugCheck API integration
│   │   └── liquidity_lock.py   # On-chain LP lock analysis
│   └── trading/
│       ├── executor.py         # Tx execution via Node.js bridge
│       └── position_manager.py # Position lifecycle & exit logic
├── bridge/
│   └── raydium_sdk_bridge.js   # Node.js Raydium SDK wrapper
├── tests/
├── docs/
├── .env                        # Wallet key + RPC URL (never commit)
├── requirements.txt
└── package.json
```

---

## Quick Start

### 1. Install Dependencies

```bash
cd raydium-lp-bot

# Python
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Node.js (Raydium SDK)
npm install
```

### 2. Configure Wallet & Environment

> ⚠️ **Create a new Solana wallet exclusively for this bot.** Never use your main wallet. Fund with SOL only. Start small (0.5-1 SOL). Any existing tokens or manual operations may interfere with balance tracking.

```bash
# Generate a dedicated keypair
solana-keygen new --outfile ~/raydium-bot-keypair.json
solana-keygen pubkey ~/raydium-bot-keypair.json
# Fund the address above with SOL from your main wallet
```

```bash
cp .env.example .env
```

Edit `.env`:
```env
# Export keypair to base58:
# cat ~/raydium-bot-keypair.json | python -c "import sys,json,base58; print(base58.b58encode(bytes(json.load(sys.stdin))).decode())"
WALLET_PRIVATE_KEY=your_base58_private_key

# Free RPC: https://www.helius.dev/
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### 3. Run

```bash
./venv/bin/python run.py
```

The bot starts in **paper trading mode** by default. It will scan, score, and simulate without executing transactions.

To enable live trading, edit `bot/config.py`:
```python
TRADING_ENABLED: bool = True
DRY_RUN: bool = False
```

See [docs/ENABLE_TRADING.md](docs/ENABLE_TRADING.md) for the full checklist before going live.

---

## How It Works

### Pipeline

```
Discovery → Safety Check → Scoring → Entry → Monitoring (1s) → Smart Exit
```

1. **Pool Discovery** - Fetches WSOL pairs from Raydium V3 API, paginates up to 1000 pools, caches 60s
2. **Filtering** - TVL ≥ $10k, Vol/TVL ≥ 0.5, APR ≥ 5%, LP burn ≥ 50%
3. **Safety Check** - Three independent layers (see below)
4. **Scoring** - 5-factor weighted score (0-100): APR (35%), Vol/TVL (20%), Liquidity (20%), IL (10%), LP Burn (15%)
5. **Entry** - Position sized by score, capped by config, queued to buy worker thread
6. **Monitoring** - Position check thread updates metrics every 1s from on-chain reserves
7. **Exit** - First trigger wins: Stop Loss, Take Profit, Max Hold Time, IL limit, or Ghost Detection

### Multi-Layer Rug-Pull Protection

```
Pool enters evaluation
  ├─→ V3 burnPercent ≥ 50%?        → NO  → REJECT
  ├─→ On-chain LP lock ≥ 50%?      → NO  → REJECT
  ├─→ RugCheck score ≤ 60?         → NO  → REJECT
  ├─→ Any danger-level risks?      → YES → REJECT
  └─→ All pass                     → Proceed to scoring
```

**Layer 1: LP Burn.** The V3 API `burnPercent` field verifies LP tokens were burned at pool creation.

**Layer 2: On-Chain LP Lock.** Queries Solana RPC to classify where remaining LP tokens are held: burned, protocol-locked, contract-locked (Streamflow, Jupiter Lock, Fluxbeam, Raydium LP Lock), or unlocked wallets.

**Layer 3: RugCheck Token Safety.** Token-level analysis via RugCheck API: `score_normalised` (0-100, lower = safer), holder concentration, freeze/mint authority detection. Any "danger" level risk is a hard rejection.

### Progressive Cooldown System

When a pool triggers a stop loss, the bot applies escalating cooldowns before allowing re-entry:

| Consecutive Stop Losses | Action |
|------------------------|--------|
| 1st | 24h cooldown |
| 2nd | 48h cooldown |
| 3rd | **Permanent blacklist** |

- A **Take Profit** exit resets the strike counter for that pool
- Blacklists persist across restarts via `data/bot_state.json`

---

## Configuration

All settings in `bot/config.py`. The tables below cover the most important tuning knobs.

### Trading & Position Sizing

| Setting | Default | Description |
|---------|---------|-------------|
| `TRADING_ENABLED` | `True` | Master switch for blockchain transactions |
| `DRY_RUN` | `False` | Paper trading mode (no transactions) |
| `MAX_ABSOLUTE_POSITION_SOL` | `5.0` | Hard cap per position in SOL |
| `MIN_POSITION_SOL` | `0.05` | Minimum position size |
| `MAX_CONCURRENT_POSITIONS` | `3` | Max simultaneous LP positions |
| `RESERVE_SOL` | `0.05` | SOL reserved for tx fees + ATA rent |
| `SLIPPAGE_PERCENT` | `5.0` | Slippage tolerance for swaps |

### Exit Conditions

| Setting | Default | Description |
|---------|---------|-------------|
| `STOP_LOSS_PERCENT` | `-25.0` | Exit when P&L drops below this % |
| `TAKE_PROFIT_PERCENT` | `+20.0` | Exit when P&L rises above this % |
| `MAX_HOLD_TIME_HOURS` | `24` | Force exit after this many hours |
| `MAX_IMPERMANENT_LOSS` | `-5.0` | Exit if IL exceeds this % |

### Pool Filtering

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_LIQUIDITY_USD` | `10000` | Minimum pool TVL |
| `MIN_VOLUME_TVL_RATIO` | `0.5` | 24h volume must be ≥ this × TVL |
| `MIN_APR_24H` | `100.0` | Minimum 24h APR % |
| `MIN_BURN_PERCENT` | `50.0` | Minimum LP tokens burned |

### Token Safety (RugCheck)

| Setting | Default | Description |
|---------|---------|-------------|
| `CHECK_TOKEN_SAFETY` | `True` | Enable RugCheck API analysis |
| `MAX_RUGCHECK_SCORE` | `50` | Max score (0-100, lower = safer) |
| `MAX_TOP10_HOLDER_PERCENT` | `35.0` | Reject if top 10 holders own more than this % |
| `MAX_SINGLE_HOLDER_PERCENT` | `20.0` | Reject if any single holder owns more than this % |
| `MIN_TOKEN_HOLDERS` | `100` | Reject tokens with fewer holders |

### LP Lock Safety (On-Chain)

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_SAFE_LP_PERCENT` | `50.0` | Min % of LP that must be burned/locked |
| `MAX_SINGLE_LP_HOLDER_PERCENT` | `25.0` | Max % single wallet can hold of unlocked LP |

### Cooldowns & Blacklist

| Setting | Default | Description |
|---------|---------|-------------|
| `STOP_LOSS_COOLDOWNS` | `[86400, 172800]` | Escalating cooldowns in seconds (24h, 48h) |
| `PERMANENT_BLACKLIST_STRIKES` | `3` | Permanent ban after this many consecutive stop losses |

### Intervals

| Setting | Default | Description |
|---------|---------|-------------|
| `POSITION_CHECK_INTERVAL_SEC` | `1` | Position update frequency (threaded) |
| `DISPLAY_INTERVAL_SEC` | `4` | Status display refresh |
| `POOL_SCAN_INTERVAL_SEC` | `180` | Pool rescan frequency |

---

## Data Sources

| Source | Purpose | Auth |
|--------|---------|------|
| **Raydium V3 API** | Pool discovery, TVL, APR, volume, burnPercent | No |
| **RugCheck API** | Token safety scoring + risk analysis | No |
| **Jupiter Price API** | SOL/USD pricing (primary, needs API key) | Optional |
| **CoinGecko API** | SOL/USD pricing (fallback) | No |
| **Solana RPC** | Wallet balance, LP values, tx submission | API key recommended |

---

## State & Persistence

**`data/bot_state.json`** stores active positions, exit cooldowns, stop-loss strike counts, permanent blacklist, and snapshot history. Updated on every state change. Loaded on startup to resume seamlessly.

**`data/trade_history.jsonl`** is an append-only trade log. One JSON object per closed position with entry/exit prices, P&L, fees, IL, hold time, and exit reason.

### Startup Behavior

On every startup the bot automatically:
1. Unwraps leftover WSOL → native SOL
2. Detects & closes ghost positions (LP = 0 on-chain)
3. Recovers orphaned LP tokens from previous runs
4. Sells leftover non-SOL tokens from failed exits
5. Closes empty token accounts to reclaim rent
6. Loads saved state (positions, cooldowns, blacklist)
7. Asks whether to continue or close existing positions

### Shutdown (`Ctrl+C`)

Saves state and stops. **Positions remain open on-chain** and resume on next startup. To force-close all positions, use the startup prompt or `python manage_positions.py`.

---

## Running

```bash
# Start the bot
make start
# — or —
.venv/bin/python run.py
```

Run in paper trading mode (`DRY_RUN = True`) for 30-60 minutes before going live. See [docs/ENABLE_TRADING.md](docs/ENABLE_TRADING.md).

---

## Testing

The test suite uses **pytest** with two tiers:

| Tier | What it tests | Network? |
|------|--------------|----------|
| **Unit** | All modules, fully mocked | No |
| **Integration** | Real Raydium API, RugCheck, Node.js bridge, on-chain reads | Yes |

```bash
# Run everything (unit + integration)
make test

# Unit tests only (fast, no network)
make test-unit

# Integration tests only (requires .env with RPC + wallet)
make test-integration

# Run a specific test file
.venv/bin/python -m pytest tests/test_config.py -v

# Run a single test
.venv/bin/python -m pytest tests/test_pool_analyzer.py::TestCalculatePoolScore::test_high_score -v
```

> **Note:** Integration tests are marked with `@pytest.mark.integration` and require network access plus a valid `.env` file. They are included in `make test` but excluded from `make test-unit`.

---

## ⚠️ Risks & Disclaimer

**This bot trades real money on Solana mainnet.** Key risks:

- **Impermanent Loss** - Price divergence causes loss even with fee income
- **Rug Pulls** - Despite 3-layer safety checks, tokens can still be exploited
- **Smart Contract Risk** - Raydium protocol bugs or exploits
- **Meme Token Volatility** - High-APR pools can drop 50-90% in hours
- **Slippage** - Low liquidity pools have high entry/exit slippage

**This software is provided "as-is" without warranty. You are solely responsible for all trading outcomes. Never invest more than you can afford to lose completely. Not financial advice.**
