# Raydium LP Bot

**The first open-source LP fee farming bot for Solana.**

Autonomously discovers, evaluates, and provides liquidity to high-yield Raydium pools — then actively manages positions to farm trading fees while protecting against rug pulls and impermanent loss. No other open-source tool on Solana does this.

Automated Solana liquidity pool bot that discovers, scores, and trades WSOL pairs on Raydium DEX.

### What makes this different

| Category | Existing Solana bots | This bot |
|---|---|---|
| **Strategy** | Snipe tokens, copy trades, rug pools | Provide liquidity, earn trading fees |
| **Pool selection** | Hardcoded single pair or manual | Autonomous discovery + 5-factor scoring across all WSOL pairs |
| **Safety** | None or basic | RugCheck + on-chain LP lock analysis + holder concentration checks |
| **Risk management** | Buy and pray | Stop-loss, take-profit, IL monitoring, time-based exits |
| **Revenue model** | Price speculation (buy low / sell high) | Fee farming (earn 0.25% of every trade through your pool) |

## Features

- **Raydium V3 API** — Paginated, mint-filtered pool discovery (WSOL pairs only)
- **LP Burn Verification** — Uses V3 `burnPercent` for real on-chain LP burn data
- **RugCheck Integration** — Token safety scoring via `score_normalised` (0-100, lower = safer) with strict holder concentration and danger-level risk rejection
- **Pool Scoring** — 5-factor scoring: APR (35%), Vol/TVL (20%), Liquidity (20%), IL (10%), LP Burn (15%)
- **Raydium SDK Bridge** — Full transaction execution via Node.js + `@raydium-io/raydium-sdk`
- **Risk Management** — Stop-loss, take-profit, IL monitoring, time-based forced exit
- **Paper Trading** — Dry-run mode for testing without real transactions

---

## Project Structure

```
raydium-lp-bot/
├── run.py                  # Entry point
├── bot/
│   ├── config.py           # Central BotConfig dataclass (21 fields)
│   ├── raydium_client.py   # V3 API client (paginated, cached, WSOL-filtered)
│   ├── main.py             # LiquidityBot orchestration loop
│   ├── analysis/
│   │   ├── pool_analyzer.py    # 5-factor pool scoring + position sizing
│   │   ├── pool_quality.py     # Risk assessment (burnPercent + RugCheck)
│   │   └── price_tracker.py    # Price derivation from pool reserves
│   ├── safety/
│   │   └── rugcheck.py         # RugCheck API (score_normalised, risks[])
│   └── trading/
│       ├── executor.py         # Transaction execution via Node.js bridge
│       └── position_manager.py # Position tracking with SL/TP/IL/time exits
├── bridge/
│   └── raydium_sdk_bridge.js   # Node.js Raydium SDK wrapper (add/remove/balance)
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

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```env
WALLET_PRIVATE_KEY=your_base58_private_key
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### 3. Run (Paper Trading)

```bash
./venv/bin/python run.py
```

The bot will scan for WSOL pools, score them, and simulate positions without real transactions.

### 4. Enable Live Trading

See [docs/ENABLE_TRADING.md](docs/ENABLE_TRADING.md) for the full safety checklist.

In `bot/config.py`:
```python
TRADING_ENABLED: bool = True
DRY_RUN: bool = False
```

---

## How It Works

### Pipeline

```
Raydium V3 API → Filter (TVL/Vol/APR/Burn) → RugCheck → Score → Trade → Monitor → Exit
```

1. **Discovery** — Fetches WSOL pools from V3 API (`/pools/info/mint?mint1=WSOL`), paginated up to 1000 pools
2. **Filtering** — Applies TVL ≥ $10k, Vol/TVL ≥ 0.5, APR ≥ 5%, burn ≥ 50%
3. **Safety** — RugCheck token scoring (optional, `CHECK_TOKEN_SAFETY=True`)
4. **Scoring** — 5-factor weighted score (0-100)
5. **Entry** — Position sized by score, capped by `MAX_ABSOLUTE_POSITION_SOL`
6. **Monitoring** — Price checks every 10 seconds via pool reserve ratios
7. **Exit** — Triggers on stop-loss (-2%), take-profit (+5%), IL (-3%), or time (24h)

### Transaction Flow

```
Python Bot → Node.js Bridge → Raydium SDK → Solana
     ↓
  Position
  Tracking
```

---

## Configuration

All settings in `bot/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_LIQUIDITY_USD` | 10,000 | Minimum pool TVL in USD |
| `MIN_VOLUME_TVL_RATIO` | 0.5 | 24h volume must be ≥ 50% of TVL |
| `MIN_APR_24H` | 5.0 | Minimum 24h APR (%) |
| `MIN_BURN_PERCENT` | 50.0 | Minimum LP tokens burned (%) |
| `CHECK_TOKEN_SAFETY` | True | Enable RugCheck token analysis |
| `MAX_RUGCHECK_SCORE` | 60 | Max acceptable RugCheck score (0-100, lower=safer) |
| `MAX_TOP10_HOLDER_PERCENT` | 50.0 | Reject if top 10 holders own more than this % |
| `MAX_SINGLE_HOLDER_PERCENT` | 20.0 | Reject if any single holder owns more than this % |
| `MAX_ABSOLUTE_POSITION_SOL` | 5.0 | Hard cap per position (SOL) |
| `MAX_CONCURRENT_POSITIONS` | 5 | Max simultaneous positions |
| `RESERVE_PERCENT` | 0.20 | Keep 20% capital in reserve |
| `STOP_LOSS_PERCENT` | -2.0 | Exit at -2% loss |
| `TAKE_PROFIT_PERCENT` | 5.0 | Exit at +5% profit |
| `MAX_HOLD_TIME_HOURS` | 24 | Force exit after 24 hours |
| `MAX_IMPERMANENT_LOSS` | -3.0 | Exit if IL exceeds 3% |
| `TRADING_ENABLED` | True | Enable real transactions |
| `DRY_RUN` | False | Paper trading mode |

---

## Data Sources

| Source | Endpoint | Purpose |
|--------|----------|---------|
| **Raydium V3 API** | `api-v3.raydium.io/pools/info/mint` | Pool discovery, TVL, APR, volume, burnPercent |
| **RugCheck API** | `api.rugcheck.xyz/v1/tokens/{mint}/report` | Token safety scoring + risk analysis |
| **Jupiter Price API** | `api.jup.ag/price/v3` | SOL/USD pricing (primary, needs `JUPITER_API_KEY`) |
| **CoinGecko API** | `api.coingecko.com/api/v3/simple/price` | SOL/USD pricing (fallback, no key needed) |
| **Solana RPC** | Configurable (Helius recommended) | Wallet balance, transaction submission |

### SOL/USD Pricing

The bot displays all values in both SOL and USD. Pricing uses a two-tier fallback:

1. **Jupiter Price API v3** — Used when `JUPITER_API_KEY` is set in `.env`. Recommended for accuracy and rate limits.
2. **CoinGecko free API** — Automatic fallback when no Jupiter key is configured. No API key required, but subject to CoinGecko's free-tier rate limits (~30 req/min).

### Raydium V3 vs V2

The bot uses V3 exclusively. V2 (`api.raydium.io/v2/main/pairs`) returned 704k+ pools (mostly dead) with no burn data. V3 provides:
- Mint-based filtering (only WSOL pairs)
- Pagination (100 per page)
- `burnPercent` field (real LP burn %)
- Nested `day` stats (apr, volume, volumeFee, feeApr)

### RugCheck Scoring

- `score_normalised`: 0-100, **lower = safer** (USDC ≈ 0, risky tokens > 60)
- `risks[]`: Array of risk items with `level` (danger/warn/info/good)
- **All danger-level items cause hard pool rejection** (top 10 holder concentration, single whale, freeze/mint authority, etc.)
- Holder concentration checked: top 10 > 50% or single holder > 20% → rejected
- Freeze/mint authority detected from `risks[]` array (not unreliable top-level fields)

---

## Testing

```bash
# Test wallet connection + pool scanning
./venv/bin/python tests/test_wallet.py

# Test trading pipeline
./venv/bin/python tests/test_trading.py

# Test Node.js bridge
./venv/bin/python tests/test_bridge.py
```

---

## ⚠️ Disclaimer

This bot trades **real money** on Solana mainnet. High-APR WSOL pools are mostly high-risk meme tokens with extreme volatility. You can lose everything.

**Never invest more than you can afford to lose.**
