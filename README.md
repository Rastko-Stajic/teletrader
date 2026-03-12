# TeleTrader ⚡

Automated trading bot: reads signals from a private Telegram group → calculates position size → executes trades on MetaTrader 5.

---

## Requirements

- **Windows** (MetaTrader5 Python API is Windows-only)
- Python 3.11+
- MetaTrader5 terminal installed and running
- Telegram account with read access to the signal group

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
# Edit .env with your values
```

**Get Telegram API credentials:**
1. Go to https://my.telegram.org
2. Log in → "API development tools"
3. Create app → copy `api_id` and `api_hash`

**Get your Telegram group ID:**
- Forward any message from the group to `@userinfobot`
- It will show the group ID (a negative number like `-1001234567890`)

### 3. Run
```bash
python main.py
```

On first run, Telethon will ask you to enter the code sent to your Telegram account.
A session file (`teletrader_session.session`) is saved locally so you only authenticate once.

### 4. Open the dashboard
```
http://127.0.0.1:8000
```

---

## Project Structure

```
trading-bot/
├── main.py                    # Entry point — wires all components together
├── config/
│   └── settings.py            # Loads all config from .env
├── core/
│   ├── signal.py              # Signal and CloseSignal dataclasses
│   ├── signal_parser.py       # Parses open and close signals from message text
│   ├── telegram_listener.py   # Telethon async listener — routes messages
│   ├── mt5_executor.py        # Opens, closes, and cancels MT5 orders
│   ├── lot_calculator.py      # Position sizing (local math engine)
│   ├── position_tracker.py    # Maps Telegram message IDs → MT5 tickets
│   ├── risk_manager.py        # Kill switch, lot cap, daily loss limit
│   └── logger.py              # Rotating file + console logger
├── ui/
│   ├── dashboard.py           # FastAPI backend (REST API)
│   └── dashboard.html         # Local web dashboard
├── logs/
│   ├── teletrader.log         # Main rotating log (auto-created)
│   └── unrecognized.log       # Messages that matched no pattern (auto-created)
├── requirements.txt
└── .env.example
```

---

## Signal Format

The bot expects signals in this format (human variation in spacing and wording is handled):

```
Sell XAUUSD
1%
E: 2318.50
SL: 2305.00
TP: OPEN
```

```
Buy EURUSD
2%
E: 1.0850
SL: 1.0820
TP: 1.0900
```

| Field | Format | Notes |
|---|---|---|
| Direction + Symbol | `Buy EURUSD`, `Sell Gold` | `Gold` → XAUUSD, `BTC` → BTCUSD, etc. |
| Risk % | `1%`, `1% risk`, `risk: 1%` | Must be between 0.1% and 10% |
| Entry price | `E: 2318.50`, `Entry: 1.0850` | Sets a limit order; omit for market order |
| Stop loss | `SL: 2305.00`, `S/L: 2305`, `Stop loss: 2305` | Required for lot size calculation |
| Take profit | `TP: 2340.00` or `TP: OPEN` | `OPEN` / `-` / `none` = no fixed target |

---

## Close Signals

| Message | Action |
|---|---|
| `Close XAUUSD now 2318 +32 pips` | Closes **all open positions** for XAUUSD |
| `Close all` | Closes **every open position** on the account |
| `Cancel` (as reply to open signal) | Cancels the specific pending order |

---

## Position Sizing

Lot size is calculated automatically using:

```
risk_amount  = balance × (risk% / 100)
lot_size     = risk_amount / (stop_loss_pips × pip_value_per_lot)
```

Pip conventions:
| Instrument | 1 pip |
|---|---|
| Standard Forex (non-JPY) | 0.0001 |
| JPY pairs (USDJPY, EURJPY…) | 0.01 |
| XAUUSD (Gold) | 0.10 |
| Indices (NAS100, US30…) | 1.0 point |

Account balance is read live from MT5 before every trade.

---

## Risk Controls

All configurable in `.env`:

| Setting | Default | Description |
|---|---|---|
| `DEFAULT_LOT_SIZE` | 0.01 | Fallback lot size if signal is missing risk % or SL |
| `MAX_LOT_SIZE` | 0.10 | Hard cap — no single order can exceed this |
| `MAX_OPEN_TRADES` | 5 | Blocks new trades if this many positions are open |
| `MAX_DAILY_LOSS_USD` | 100 | Auto-halts trading if daily loss reaches this amount |
| `ALLOWED_SYMBOLS` | (all) | Comma-separated whitelist — leave empty to allow all |

**Kill Switch**: One-click button in the dashboard to instantly block all new trades without stopping the bot.

---

## Dashboard

Local web UI at `http://127.0.0.1:8000` showing:

- **Account overview** — balance, equity, open P&L, free margin
- **Signal feed** — every parsed open signal with direction, symbol, entry, SL, TP
- **Open positions** — live MT5 positions with per-trade P&L
- **Executed trades** — history of all trades placed this session
- **System log** — last 100 lines of the main log file
- **Unrecognized messages** — any Telegram message that matched no known pattern (e.g. "SL hit" notifications), for human review

---

## Unrecognized Messages

Any message from the Telegram group that isn't recognized as an open signal or close signal is:
- Written to `logs/unrecognized.log` (one JSON entry per line, persists across restarts)
- Shown in the dashboard "Unrecognized Messages" panel for review

This gives you a full audit trail of everything the bot saw but didn't act on.

---

## Simulation Mode

If `MetaTrader5` is not installed (e.g. on Linux/Mac for development), the bot runs in **simulation mode** — all signals are parsed, lot sizes calculated, and results logged, but no real orders are placed. Useful for testing the parsing and sizing logic without a live MT5 connection.

---

## What's Not Built Yet

- Position management (move SL to breakeven, partial closes at TP levels)
- Dashboard charts and trade history persistence
- Playwright browser fallback for lot calculator (disabled — web calculator dropdown not yet configured)

---

## Disclaimer

This software is for educational purposes. Trading involves significant financial risk.
Always test thoroughly on a **demo account** before using real funds.
