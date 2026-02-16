# Raydium LP Bot

**The first open-source LP fee farming bot for Solana.**

Autonomously discovers, evaluates, and provides liquidity to high-yield Raydium pools — then actively manages positions to farm trading fees while protecting against rug pulls and impermanent loss. No other open-source tool on Solana does this.

## Features

- **Raydium V3 API** — Paginated, mint-filtered pool discovery (WSOL pairs only)
- **LP Burn Verification** — Uses V3 `burnPercent` for real on-chain LP burn data
- **On-Chain LP Lock Analysis** — Queries Solana RPC to verify LP tokens are burned, protocol-locked, or in time-lock contracts
- **RugCheck Integration** — Token safety scoring via `score_normalised` (0-100, lower = safer) with strict holder concentration and danger-level risk rejection
- **Multi-Layer Safety** — Combines V3 burn %, on-chain LP lock analysis, and RugCheck for comprehensive rug-pull protection
- **Pool Scoring** — 5-factor scoring: APR (35%), Vol/TVL (20%), Liquidity (20%), IL (10%), LP Burn (15%)
- **Raydium SDK Bridge** — Full transaction execution via Node.js + `@raydium-io/raydium-sdk`
- **Risk Management** — Stop-loss, take-profit, IL monitoring, time-based forced exit
- **Paper Trading** — Dry-run mode for testing without real transactions

---

## Thank you for your support!

Donations are graciously accepted for your continuing support in the development of this software and others.

- **XMR**: 8BwmJHCfeaL9z3f1DwjStW7i1bvwKPL8oXhDnfcXbjRNSQAxVk9PVFv74SoFWVGWEVVQDCfb1bTsa1S53KP18zrwVizUeqe
- **BTC**: bc1q3frlupheaz79v88t4hc8lgzfqwy4nekvc4gtj7
- **ETH**: 0x9038E310D0a6B8E7819A8b7c33E53ebCF6964eF9
- **SOL**: Gzea6q2aBmpUUPMwCcAUUPsxGqSua5k6HT8PaGHD3ewn

---

## Project Structure

```
raydium-lp-bot/
├── run.py                      # Entry point — starts the bot
├── recover.py                  # Utility to manually recover orphaned LP positions
├── bot/
│   ├── config.py               # Central BotConfig dataclass (~40 fields)
│   ├── raydium_client.py       # Raydium V3 API client (paginated, cached, WSOL-filtered)
│   ├── main.py                 # LiquidityBot main orchestration loop
│   ├── state.py                # Position state persistence (JSON file I/O)
│   ├── analysis/
│   │   ├── pool_analyzer.py    # 5-factor pool scoring + position sizing
│   │   ├── pool_quality.py     # Multi-layer risk assessment (V3 burn + RugCheck + LP lock)
│   │   ├── price_tracker.py    # Price derivation from pool reserve ratios
│   │   └── snapshot_tracker.py # Historical pool state tracking
│   ├── safety/
│   │   ├── rugcheck.py         # RugCheck API (score_normalised, holder analysis, risks[])
│   │   └── liquidity_lock.py   # On-chain LP lock analysis (burn/protocol/contract/unlocked)
│   └── trading/
│       ├── executor.py         # Transaction execution via Node.js bridge + RPC queries
│       └── position_manager.py # Position lifecycle (open/update/close, SL/TP/IL/time exits)
├── bridge/
│   └── raydium_sdk_bridge.js   # Node.js Raydium SDK wrapper (add/remove liquidity, swap, balance)
├── tests/
│   ├── test_trading.py
│   ├── test_wallet.py
│   └── test_bridge.py
├── docs/
│   ├── TRADING_SETUP.md
│   └── ENABLE_TRADING.md
├── .env                    # Wallet key + RPC URL (never commit)
├── .env.example
├── requirements.txt
└── package.json            # @raydium-io/raydium-sdk, @solana/web3.js
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

## ⚠️ Critical Wallet Setup

**USE A DEDICATED WALLET ONLY**

- Create a **new Solana wallet exclusively for this bot** — never use your main wallet
- Fund it with **SOL only** — the bot will automatically wrap SOL to WSOL as needed
- Never manually send tokens to this wallet — the bot manages all token operations
- The bot will automatically clean up leftover tokens and close empty accounts
- **Start small** (0.5-1 SOL) until you understand the bot's behavior

Any existing tokens or manual operations on this wallet may interfere with the bot's balance tracking and position management.

**Create a dedicated wallet:**
```bash
# Install Solana CLI if you haven't already
# Download from https://docs.solana.com/cli/install-solana-cli-tools

# Generate a new keypair (dedicated to this bot only)
solana-keygen new --outfile ~/raydium-bot-keypair.json

# Get the public address
solana-keygen pubkey ~/raydium-bot-keypair.json

# Fund it with SOL (start with 0.5-1 SOL for testing)
# Transfer SOL from your main wallet to the address above
```

**Configure environment:**
```bash
cp .env.example .env
```

Edit `.env`:
```env
# Export your keypair to base58 format:
# cat ~/raydium-bot-keypair.json | python -c "import sys,json,base58; print(base58.b58encode(bytes(json.load(sys.stdin))).decode())"
WALLET_PRIVATE_KEY=your_base58_private_key

# Get a free Helius RPC key: https://www.helius.dev/
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# Optional: Jupiter API key for better SOL/USD pricing
# JUPITER_API_KEY=your_jupiter_key
```

### 3. Run (Paper Trading Mode)

```bash
./venv/bin/python run.py
```

The bot starts in **paper trading mode** by default — it will scan pools, score them, and simulate positions without executing real transactions. This is safe for testing.

**What happens on first run:**
- Scans Raydium V3 API for WSOL pools
- Filters by TVL, volume, APR, and LP burn percentage
- Checks token safety via RugCheck
- Displays top-scored pools
- Simulates entry/exit without blockchain transactions

### 4. Enable Live Trading (When Ready)

⚠️ **Only enable live trading after:**
1. Running in paper trading mode successfully
2. Reading [docs/ENABLE_TRADING.md](docs/ENABLE_TRADING.md) completely
3. Understanding the risks and exit conditions
4. Funding your **dedicated wallet** with an amount you can afford to lose

**To enable live trading:**

Edit [bot/config.py](bot/config.py):
```python
TRADING_ENABLED: bool = True  # Enable real blockchain transactions
DRY_RUN: bool = False          # Disable simulation mode
```

**Start with conservative settings:**
- `MAX_ABSOLUTE_POSITION_SOL = 0.2` (start small)
- `MAX_CONCURRENT_POSITIONS = 1` (one position at a time)
- `STOP_LOSS_PERCENT = -25.0` (tighter stop loss for meme tokens)

Then run:
```bash
./venv/bin/python run.py
```

The bot will now execute real transactions on Solana mainnet.

---

## How It Works

### High-Level Flow

```
Discovery → Safety Check → Scoring → Entry → Active Monitoring → Smart Exit
```

The bot continuously cycles through this process, managing multiple positions simultaneously.

### Detailed Pipeline

1. **Pool Discovery** 
   - Fetches WSOL pairs from Raydium V3 API (`/pools/info/mint?mint1=WSOL`)
   - Paginates up to 1000 pools per scan
   - Caches pool data (60s TTL) to avoid rate limits

2. **Initial Filtering**
   - TVL ≥ $10,000 (configurable)
   - Volume/TVL ratio ≥ 0.5 (pools must have trading activity)
   - 24h APR ≥ 5%
   - LP burn ≥ 50% (prevents rug pulls via liquidity withdrawal)

3. **Token Safety Check** (RugCheck)
   - Queries RugCheck API for token analysis
   - Rejects tokens with `score_normalised` > 60 (0=safest, 100=riskiest)
   - Hard rejection on any "danger" level risks
   - Checks holder concentration (top 10 holders, single whale limits)
   - Verifies no freeze/mint authority

4. **Pool Scoring** (5 factors, weighted 0-100)
   - APR (35%): Higher APR = more fee income
   - Volume/TVL ratio (20%): Trading activity indicator
   - Liquidity (20%): Deeper pools = less slippage
   - Impermanent Loss (10%): Price stability score
   - LP Burn (15%): Rug-pull protection

5. **Position Entry**
   - Size = `base_amount * (score/100) * pool_factor`
   - Capped by `MAX_ABSOLUTE_POSITION_SOL`
   - Respects `MAX_CONCURRENT_POSITIONS` and `RESERVE_PERCENT`
   - Enforces cooldown period (86400s = 24h) before re-entering same pool
   - Executes via Node.js bridge → Raydium SDK → Solana blockchain

6. **Active Monitoring** (every 10 seconds)
   - Calculates current price from pool reserves (no external oracle needed)
   - Tracks P&L percentage vs entry price
   - Monitors impermanent loss
   - Updates position metrics and displays status

7. **Smart Exit** (multiple conditions, first to trigger wins)
   - **Stop Loss**: P&L ≤ -25% (default, configurable)
   - **Take Profit**: P&L ≥ +20% (default, configurable)
   - **Max Hold Time**: Position held ≥ 24 hours (default)
   - **Impermanent Loss**: IL ≥ 3% (default)
   - **Ghost Detection**: LP balance = 0 (pool rugged or exploited)
   - Removes liquidity and swaps tokens back to SOL
   - Records trade in `data/trade_history.jsonl`

### Transaction Architecture

```
Python Bot (Analysis & Logic)
      ↓
   IPC JSON
      ↓
Node.js Bridge (Raydium SDK)
      ↓
Solana Blockchain
```

The bot uses a hybrid architecture: Python for pool analysis and position management, Node.js for blockchain transactions via the official Raydium SDK. Communication happens through `subprocess` with JSON-formatted commands.

---

## Configuration

All settings are in [bot/config.py](bot/config.py) as a Python dataclass. Edit values directly in the file.

### Core Trading Parameters

| Setting | Default | Description |
|---------|---------|-------------|
| `TRADING_ENABLED` | `True` | Master switch for blockchain transactions |
| `DRY_RUN` | `False` | Paper trading mode (simulates without transactions) |
| `MAX_ABSOLUTE_POSITION_SOL` | `0.265` | Hard cap per position in SOL |
| `MAX_CONCURRENT_POSITIONS` | `3` | Max simultaneous LP positions |
| `RESERVE_PERCENT` | `0.20` | Keep 20% of wallet as reserve (never deploy) |
| `MIN_LIQUIDITY_USD` | `10000` | Minimum pool TVL to consider |

### Exit Conditions

| Setting | Default | Description |
|---------|---------|-------------|
| `STOP_LOSS_PERCENT` | `-25.0` | Exit when P&L drops below -25% |
| `TAKE_PROFIT_PERCENT` | `+20.0` | Exit when P&L rises above +20% |
| `MAX_HOLD_TIME_HOURS` | `24` | Force exit after 24 hours regardless of P&L |
| `MAX_IMPERMANENT_LOSS` | `-3.0` | Exit if IL exceeds 3% (price divergence) |

### Pool Filtering

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_VOLUME_TVL_RATIO` | `0.5` | 24h volume must be ≥ 50% of TVL |
| `MIN_APR_24H` | `5.0` | Minimum 24h APR percentage |
| `MIN_BURN_PERCENT` | `50.0` | Minimum LP tokens burned (rug protection) |
| `POOL_SCAN_INTERVAL` | `120` | Seconds between pool rescans |

### Token Safety (RugCheck)

| Setting | Default | Description |
|---------|---------|-------------|
| `CHECK_TOKEN_SAFETY` | `True` | Enable RugCheck API safety analysis |
| `MAX_RUGCHECK_SCORE` | `60` | Max score (0-100, lower=safer, USDC≈0) |
| `MAX_TOP10_HOLDER_PERCENT` | `50.0` | Reject if top 10 holders own >50% |
| `MAX_SINGLE_HOLDER_PERCENT` | `20.0` | Reject if single holder owns >20% |

**Note:** Any token with "danger" level risks (freeze authority, mint authority, etc.) is automatically rejected regardless of score.

### LP Lock Safety (On-Chain)

| Setting | Default | Description |
|---------|---------|-------------|
| `CHECK_LP_LOCK` | `True` | Enable on-chain LP lock verification |
| `MIN_SAFE_LP_PERCENT` | `50.0` | Min % of LP that must be burned/locked |
| `MAX_SINGLE_LP_HOLDER_PERCENT` | `25.0` | Max % single wallet can hold of unlocked LP |

**Note:** This is separate from V3 API `burnPercent`. The bot checks BOTH the initial burn % AND where the remaining LP tokens are held (wallets vs time-lock contracts).

### Performance & Rate Limiting

| Setting | Default | Description |
|---------|---------|-------------|
| `POSITION_UPDATE_INTERVAL` | `10` | Seconds between position metric updates |
| `BALANCE_REFRESH_INTERVAL` | `60` | Seconds between wallet balance RPC calls |
| `POOL_CACHE_TTL` | `60` | Seconds to cache Raydium API responses |
| `RUGCHECK_RETRY_DELAY` | `2` | Seconds between RugCheck retries |
| `RUGCHECK_MAX_RETRIES` | `3` | Max retry attempts for RugCheck API |

---

## Data Sources & APIs

| Source | Endpoint | Purpose | Auth Required |
|--------|----------|---------|---------------|
| **Raydium V3 API** | `api-v3.raydium.io/pools/info/mint` | Pool discovery, TVL, APR, volume, burnPercent | No |
| **RugCheck API** | `api.rugcheck.xyz/v1/tokens/{mint}/report` | Token safety scoring + risk analysis | No |
| **Jupiter Price API** | `price.jup.ag/v6/price` | SOL/USD pricing (primary) | Optional (recommended) |
| **CoinGecko API** | `api.coingecko.com/api/v3/simple/price` | SOL/USD pricing (fallback) | No |
| **Solana RPC** | Configurable | Wallet balance, tx submission | API key recommended |

### SOL/USD Pricing Strategy

The bot displays all monetary values in both SOL and USD. It uses a two-tier fallback for SOL/USD pricing:

1. **Jupiter Price API v6** (primary)
   - Used when `JUPITER_API_KEY` is set in `.env`
   - More accurate and higher rate limits
   - Get a free key at https://www.jup.ag/

2. **CoinGecko Free API** (fallback)
   - Automatic fallback when no Jupiter key configured
   - No API key required
   - Limited to ~30 requests/minute

### RugCheck Scoring System

RugCheck provides a comprehensive token safety report. The bot uses:

- **`score_normalised`**: 0-100 scale, **lower = safer**
  - USDC/USDT ≈ 0-5
  - Blue chips ≈ 10-30
  - Risky tokens ≈ 60+
  - Bot rejects tokens with score > 60 (configurable)

- **`risks[]` array**: List of risk items with severity levels
  - `danger`: Hard rejection (freeze authority, single whale >20%, etc.)
  - `warn`: Counted in score but not auto-rejected
  - `info`: Informational only
  - `good`: Positive signals

- **Holder concentration checks**:
  - Top 10 holders combined > 50% → rejected
  - Single holder > 20% → rejected
  - Extracted from `risks[]` items, not unreliable top-level fields

**The bot performs hard rejection on ANY "danger" level risk**, regardless of score. This catches freeze authorities, mint authorities, and severe holder concentration even if the normalized score looks acceptable.

---

## Testing & Validation

### Pre-Launch Testing

Before enabling live trading, validate each component:

**1. Wallet Connection & Balance**
```bash
./venv/bin/python tests/test_wallet.py
```
- Verifies RPC connection
- Displays wallet address and SOL balance
- Tests SOL/USD price fetching

**2. Pool Discovery & Scoring**
```bash
./venv/bin/python tests/test_trading.py
```
- Fetches real Raydium V3 pools
- Applies filtering and scoring logic
- Shows top-ranked pools
- Tests RugCheck integration

**3. Bridge Communication**
```bash
./venv/bin/python tests/test_bridge.py
```
- Tests Python ↔ Node.js IPC
- Validates Raydium SDK initialization
- Confirms wallet keypair loading

### Paper Trading (Dry Run)

The safest way to test the full bot logic:

```bash
# In bot/config.py:
# TRADING_ENABLED = True
# DRY_RUN = True

./venv/bin/python run.py
```

**In dry run mode:**
- ✅ Real pool discovery and scoring
- ✅ Real RugCheck safety analysis
- ✅ Position entry/exit logic executed
- ✅ P&L calculations and metrics
- ❌ No blockchain transactions
- ❌ No real money at risk

Monitor for 30-60 minutes to ensure:
- Pool scanning completes without errors
- Positions are "entered" and "exited" correctly
- Exit conditions trigger as expected
- No Python/Node.js exceptions

### Live Trading Validation

**First live run checklist:**

1. ✅ Dedicated wallet created and funded with **0.5-1 SOL only**
2. ✅ Conservative settings in `bot/config.py`:
   - `MAX_ABSOLUTE_POSITION_SOL = 0.2`
   - `MAX_CONCURRENT_POSITIONS = 1`
   - `STOP_LOSS_PERCENT = -25.0`
3. ✅ Paper trading ran successfully for 30+ minutes
4. ✅ Read [docs/ENABLE_TRADING.md](docs/ENABLE_TRADING.md) completely
5. ✅ Understand you may lose all deployed capital

**Enable live trading:**
```python
# bot/config.py
TRADING_ENABLED: bool = True
DRY_RUN: bool = False
```

**Run and monitor:**
```bash
./venv/bin/python run.py
```

Watch the first position entry carefully:
- Verify SOL → WSOL wrap transaction succeeds
- Confirm LP position appears in Raydium UI (https://raydium.io/liquidity-pools/)
- Check `data/bot_state.json` shows correct position data
- Monitor P&L updates every 10 seconds

**Let the bot run for 24 hours** before increasing position sizes or concurrent positions.

---

## Multi-Layer Rug-Pull Protection

The bot uses **three independent safety systems** to filter out dangerous pools before entry:

### 1. Raydium V3 API Burn Percentage

**What it measures:** Percentage of LP tokens that were burned (sent to dead addresses) when the pool was created.

- Extracted from V3 API: `pool.burnPercent` field
- **Minimum threshold:** 50% (configurable via `MIN_BURN_PERCENT`)
- **Why it matters:** If LP tokens aren't burned, the creator can withdraw all liquidity (rug pull)

**Example:**
```
Pool: WSOL/$HACHI
burnPercent: 94.2%  ✅ PASS (creator burned 94.2%, can't rug)
```

### 2. On-Chain LP Lock Analysis

**What it measures:** Real-time analysis of WHERE the remaining (unburned) LP tokens are held.

The `LiquidityLockAnalyzer` queries Solana RPC to:
1. Get total LP supply for the pool's LP mint
2. Fetch top ~20 LP token holders
3. Classify each holder as:
   - **Burned** — Dead addresses (0x1111..., incinerator)
   - **Protocol-locked** — Held by Raydium authority (can't withdraw)
   - **Contract-locked** — In known time-lock contracts (Streamflow, Jupiter Lock, Fluxbeam, etc.)
   - **Unlocked** — Regular wallets (can rug at any time)

**Thresholds:**
- `MIN_SAFE_LP_PERCENT = 50` — At least 50% of circulating LP must be burned/protocol/contract-locked
- `MAX_SINGLE_LP_HOLDER_PERCENT = 25` — No single unlocked wallet can hold >25% of LP

**Known time-lock programs detected:**
- `strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m` — Streamflow
- `2r5VekMNiWPzi1pWwvJczrdPaZnJG59u91unSrTunwJg` — Jupiter Lock
- `FLockTopXvM3MRs5ThJTsSQDQNmzWfnj5s7xUQXKTc1v` — Fluxbeam Locker
- `GJa1VEhNhjMEJoeqYyPvH5Ts9XadZAdFmRSi8ijrSU7G` — Raydium LP Lock

**Example output:**
```
LP Lock Analysis for Pool XYZ:
  Total LP Supply: 1,000,000
  Burned:           60.0%  (sent to 0x1111...1111)
  Protocol-locked:  10.0%  (Raydium authority)
  Contract-locked:  20.0%  (Streamflow time-lock until 2027)
  Unlocked:         10.0%  (regular wallets)
  ────────────────────────
  Safe LP:          90.0%  ✅ PASS
  Max single whale:  5.0%  ✅ PASS
```

**Why this matters:**
- V3 API `burnPercent` only shows initial burn at pool creation
- Remaining LP could be in regular wallets = rug risk
- On-chain analysis verifies the remaining LP is actually locked
- Catches pools where creator burned 50% but holds 50% in a regular wallet

### 3. RugCheck Token Safety

**What it measures:** Token-level risks (holder concentration, freeze authority, mint authority, copycat detection).

See [RugCheck Scoring System](#rugcheck-scoring-system) section below for full details.

**Combined approach:**
```
Pool enters evaluation
  ├─→ V3 burnPercent ≥ 50%? ────→ NO  → REJECT
  ├─→ On-chain LP lock ≥ 50%? ──→ NO  → REJECT
  ├─→ RugCheck score ≤ 60? ─────→ NO  → REJECT
  ├─→ Any danger-level risks? ──→ YES → REJECT
  └─→ All pass ─────────────────→ Proceed to scoring
```

This three-layer approach catches:
- ✅ Pools with high API burn % but unlocked remaining LP (caught by layer 2)
- ✅ Pools with locked LP but token has freeze authority (caught by layer 3)
- ✅ Pools with safe token but no LP burn (caught by layer 1)

---

## Bot Lifecycle & State Management

### Startup Behavior

On every startup, the bot automatically:

1. **Recovers existing LP positions**
   - Scans wallet for Raydium LP tokens
   - Reconstructs position data from blockchain state
   - Continues tracking recovered positions

2. **Cleans up leftover tokens**
   - Swaps any non-SOL/WSOL tokens back to SOL (from previous exits)
   - Closes empty token accounts to reclaim rent (~0.002 SOL each)
   - Unwraps excess WSOL to SOL

3. **Detects ghost positions**
   - Positions where LP balance = 0 (pool rugged/exploited)
   - Automatically closes and records in trade history

4. **Loads persisted state**
   - Reads `data/bot_state.json` for position history
   - Enforces cooldown periods (won't re-enter pools exited <24h ago)

### Runtime Operations

**Every 10 seconds:**
- Updates position metrics (P&L, IL, fees)
- Checks exit conditions (SL, TP, time, IL)
- Displays status table in terminal

**Every 60 seconds:**
- Refreshes wallet SOL balance via RPC
- (Balance refresh is throttled to reduce RPC calls)

**Every 120 seconds:**
- Rescans Raydium pools for new opportunities
- Applies filtering, safety checks, and scoring
- Attempts to open new positions (if slots available)

**On position exit:**
- Removes liquidity from Raydium pool
- Swaps received tokens back to SOL (3x retry with backoff)
- Closes token account to reclaim rent
- Records trade in `data/trade_history.jsonl`
- Updates `data/bot_state.json` with cooldown

### State Persistence

**`data/bot_state.json`**
- Active positions with entry price, size, timestamps
- Closed positions (for cooldown enforcement)
- Last known P&L (persists across restarts)
- Updated on every position change

**`data/trade_history.jsonl`**
- One JSON object per line (append-only)
- Complete trade record: entry/exit prices, P&L, fees, IL, hold time
- Used for performance analysis and backtesting

### Graceful Shutdown

Press `Ctrl+C` to stop the bot:
- Saves current state to `data/bot_state.json`
- Preserves last known P&L for all positions
- **Does NOT auto-exit positions** — positions remain active
- On next startup, positions are recovered and tracking continues

To force-exit all positions before shutdown:
```bash
# Run the position management script
./venv/bin/python tests/manage_positions.py
```

---

## ⚠️ Risks & Disclaimer

### Financial Risks

This bot trades **real money** on Solana mainnet. Understand these risks:

1. **Impermanent Loss** — Price divergence between WSOL and paired token can cause loss even if fees are earned
2. **Rug Pulls** — Despite LP burn checks and RugCheck, tokens can still be exploited
3. **Smart Contract Risk** — Raydium protocol bugs or exploits (rare but possible)
4. **Slippage** — Low liquidity pools can have high slippage on entry/exit
5. **Meme Token Volatility** — High-APR pools are usually volatile meme tokens that can drop 50-90% in hours

### Disclaimer

**This software is provided "as-is" without any warranty.** You are solely responsible for:
- Understanding the code before running it
- Monitoring your positions actively
- Accepting all financial losses

**The authors are not responsible for:**
- Lost funds due to bugs, market conditions, or user error
- Rug pulls, exploits, or protocol failures
- RPC failures or API outages

**Never invest more than you can afford to lose completely.**

By running this bot, you acknowledge:
- You have reviewed the code and understand the risks
- You accept full responsibility for all trading outcomes
- This is experimental software not suitable for large capital

**Use at your own risk. Not financial advice.**
