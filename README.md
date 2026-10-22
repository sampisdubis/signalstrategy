# BTC Entry Watch — revised active-zone logic

A real-time **decision-support and alerting** monitor for one fixed BTCUSDT Binance Spot setup. It listens to live aggregate trades and 1m/5m/1h kline streams, supplies recent data to the OpenCode `entry-supervisor` agent, and logs decisions. It **does not place, cancel, or modify exchange orders**.

## Fixed setup

- Symbol: BTCUSDT Spot
- ENTRY: 82147
- TG1: 83731
- SL: 80261
- D: `2026-10-22T08:00:00Z` (2026-10-22 10:00 UTC+02:00)

## Configuration

All setup inputs live in `config.json`: X/A/B/C/D timestamps, ENTRY/TG1/SL and the order type. The monitor derives the 1h history window from X, so a new setup automatically gets the full pattern context. The current values are also stated in `.opencode/agents/entry-supervisor.md`; keep them in sync when starting a genuinely new setup.

## Correct setup lifecycle

The setup is **not** declared missed just because price has touched or crossed ENTRY in the past. If price crossed above ENTRY and later returned below ENTRY, the agent continues analyzing the same setup provided neither TG1 nor SL has been touched since D.

Startup replays all 1m candles since D using paginated Binance REST requests (each API response is capped at 1000 bars). The replay stops the setup only if:

- SL has been touched: `SETUP_INVALIDATED`;
- TG1 has been touched before a new entry decision: `TARGET_REACHED_BEFORE_ENTRY`;
- the same 1m candle spans both TG1 and SL and their intrabar order is unknown: `OUTCOME_LEVELS_TOUCHED_AMBIGUOUS`;
- required market history is missing: `DATA_INVALID`.

An ENTRY touch alone is recorded as historical information and does **not** terminate the setup. When price is above ENTRY but below TG1, the monitor waits for price to return below ENTRY before evaluating an upward stop-limit entry again. When price returns into the zone `SL < price < ENTRY`, the monitor reactivates the AI evaluation.

## Requirements

- Python 3.11+
- OpenCode CLI installed, authenticated, and available as `opencode` in PATH
- An OpenCode model/provider configured
- Network access to Binance Spot REST and WebSocket market-data endpoints

No Binance API key is needed because this monitor only reads public market data. It never places trades.

## Run

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
opencode models
python run_monitor.py
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
opencode models
python run_monitor.py
```

The monitor logs decisions under `runtime/events.jsonl` and stores persistent state in `runtime/state.json`.

## GUI panel (recommended)

1. If a monitor is already running in a terminal, stop it with `Ctrl+C`.
2. Double-click `Pokreni-GUI.cmd`.
3. Your browser opens `http://127.0.0.1:8765`. Click **Pokreni praćenje**.

The panel runs the monitor hidden in the background and shows the current decision in Serbian, fixed levels, a 1m/5m/1h candlestick chart, the AI explanation, and the event log. The **Zaustavi** button stops the monitor gracefully via `runtime/stop.flag`; closing the panel does not stop the monitor. The monitor writes `runtime/heartbeat.json` so the panel knows it is alive. No extra Python packages are needed.

## Runtime behavior

- Startup verifies exchange time and current ticker, fetches the full 1h series from X (with a small pad) and 5m context from C/D, then replays 1m candles from D.
- Every AI snapshot contains the entire XABCD 1h structure, so swing prices and Fibonacci legs are read from actual candles; the agent must not invent missing swings.
- Live monitoring uses `aggTrade` plus `kline_1m`, `kline_5m`, and `kline_1h` streams.
- The first AI evaluation runs in the background while the WebSocket connects, so model latency does not block live price observation.
- AI is invoked on startup when price is below ENTRY, on every closed 1m/5m/1h candle while the setup is active and the price is below ENTRY, on return below ENTRY after a cross, and on throttled live price-action triggers.
- A previous ENTRY crossing is never sufficient by itself to classify the opportunity as missed.
- If price reaches TG1 or SL before a new entry decision, this setup is finished; the monitor will not recommend a late new entry.
- If the market stream becomes stale, the monitor refuses to issue fresh entry advice until valid data resumes.
- The recommended order for an upward crossing from below is a BUY `STOP_LOSS_LIMIT` with `stopPrice=82147`, `price=82147`, `timeInForce=GTC`. A fast jump may trigger it without filling at that exact limit.
- Only one monitor may run at a time: it holds a loopback socket on port 48623. The GUI also refuses to start a monitor when it detects an older monitor that is not writing a heartbeat.

## Manual order acknowledgement

This program does not have authenticated access to private Binance order status. If the agent alerts `PLACE_ENTRY_ORDER_NOW`, place the indicated conditional order manually, then confirm it with the **Potvrdi da je nalog postavljen** button in the GUI. That button only creates this flag **after Binance confirms the order is resting**. Manual alternative:

macOS/Linux:

```bash
mkdir -p runtime && touch runtime/entry_order_armed.flag
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force runtime | Out-Null
New-Item -ItemType File -Force runtime/entry_order_armed.flag | Out-Null
```

The flag is only a manual acknowledgement; it does not prove an order filled. If you did not place the order, do not create the flag. If the price crosses ENTRY and later returns below it before TG1 or SL, the current setup remains eligible and the monitor can reassess the next upward crossing.

## Tests

Run the regression tests from this directory:

```bash
python -m unittest discover -s tests -v
```

The tests cover the regression at issue: a historical ENTRY crossing followed by a return to the SL–ENTRY zone must remain active unless TG1 or SL was touched.
