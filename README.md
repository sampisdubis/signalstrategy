# Signal Strategy - configurable setup monitor

A real-time **decision-support and alerting** monitor for one XABCD setup at a time. It reads public Binance market data, builds a deterministic snapshot, asks the OpenCode `entry-supervisor` agent for a decision, and shows the result in a local dashboard. It **does not place, cancel, or modify exchange orders**.

## Features

- Setup editor in the GUI: exchange, market, symbol, direction, primary interval, UTC offset, X/A/B/C/D, ENTRY, TG1 and SL.
- Long and short setups on Binance Spot or USD-M Futures.
- Deterministic market-analysis module for candles, swings, XABCD ratios, C-D and post-D behavior, indicators, level touches and live flow.
- Snapshot-driven agent: the agent receives the active setup and all derived data, so no market values are hard-coded in the prompt.
- Lifecycle protection: historical ENTRY touches are not treated as missed entries, while TG1/SL touches terminate the setup.
- Server-side guard against late, stale, malformed, out-of-zone or unconfirmed AI recommendations.
- Dashboard with setup preview, live status, path assessment, candlestick chart, AI reasoning, freshness indicators, diagnostics and event log.
- Setup-scoped state. Changing the setup archives the old runtime state and starts clean, while `events.jsonl` remains append-only.

## Setup model

The canonical setup is stored in `config.json`. The GUI writes it atomically; it is not necessary to edit Python or the agent prompt for a new setup.

Required fields include:

- `exchange`: `binance`
- `market_type`: `spot` or `futures`
- `symbol`: for example `BTCUSDT`
- `direction`: `long` or `short`
- `main_interval`: `15m`, `30m`, `1h` or `4h`
- `timezone_offset_minutes`: display/input offset from UTC
- `setup_x_timestamp_utc` through `setup_d_timestamp_utc`
- `entry`, `tg1`, `sl`

For a long setup `SL < ENTRY < TG1`; for a short setup `TG1 < ENTRY < SL`. X, A, B, C and D must be chronological. The stable `setup_id` is derived from the material setup fields, for example `btcusdt-long-f28da0984e06`.

Operational fields such as `max_market_data_age_seconds`, `stale_ai_price_move_pct`, `live_trigger_cooldown_seconds` and snapshot limits also live in `config.json`.

The dashboard displays times using the configured UTC offset and sends UTC timestamps to the backend. The offset is independent of the machine timezone.

## Setup lifecycle

A previous ENTRY crossing alone does **not** mean the entry was missed. If price crossed ENTRY and later returned into the zone, the same setup remains eligible unless a terminal level was touched.

Startup replays 1m candles since D using paginated public REST requests. The setup becomes terminal only when:

- SL was touched: `SETUP_INVALIDATED`;
- TG1 was touched before an entry: `TARGET_REACHED_BEFORE_ENTRY`;
- TG1 and SL were both touched in the same candle and their intrabar order is unknown: `OUTCOME_LEVELS_TOUCHED_AMBIGUOUS`;
- required history is missing or inconsistent: `DATA_INVALID`.

While price is above ENTRY and below TG1, the monitor waits for price to return below ENTRY before evaluating another upward crossing. When price returns into the `SL < price < ENTRY` zone (or the mirrored short zone), AI evaluation resumes.

AI decisions are guarded before display or notification:

- the `setup_id` must match;
- the result must not be stale or based on outdated market data;
- a `PLACE_ENTRY_ORDER_NOW` recommendation is rejected if the setup became terminal, price left the pre-entry zone, or price moved too far after the snapshot;
- an armed order changes the meaning of a later TG1 touch to `TARGET_REACHED_CHECK_ORDER`.

## Requirements

- Python 3.11 or newer
- OpenCode CLI installed, authenticated and available as `opencode` in `PATH`
- A configured OpenCode model/provider
- Network access to Binance public REST and WebSocket endpoints

No Binance API key is required because the monitor only reads public market data. `auto_trade` is always `false`; the project has no order-execution code.

## Install and run

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
opencode models
```

Start the GUI:

```powershell
.\Pokreni-GUI.cmd
```

Or run the monitor directly in a terminal:

```powershell
.venv\Scripts\python.exe run_monitor.py
```

The GUI opens `http://127.0.0.1:8765`. The monitor runs hidden in the background and writes `runtime/heartbeat.json`, `runtime/state.json`, `runtime/ai_snapshot.json` and `runtime/events.jsonl`.

## GUI workflow

1. Fill the setup form and save it. The preview shows the generated `setup_id`, UTC timestamps and order side.
2. Click **Pokreni pracenje** to start the monitor.
3. Follow the decision, reasoning, path assessment, charts and event log.
4. Use **Zaustavi** to stop monitoring gracefully. Exchange orders are not touched.
5. Saving a different setup while the monitor is running is blocked. Stop it first.

If `config.json` is edited manually while a monitor is running, the monitor detects the change and refuses to use the new values until it is restarted. On restart, the old setup state is archived under `runtime/archive/`.

## Manual order acknowledgement

The program cannot read private Binance order status. If the agent returns `PLACE_ENTRY_ORDER_NOW`, place the conditional order manually. Only after Binance confirms that the order is resting, click **Potvrdi da je nalog postavljen**. This creates `runtime/entry_order_armed.flag` and changes later warnings; it is only an acknowledgement, not proof of a fill.

If you did not place an order, do not create the flag.

## Agent contract

The agent reads `runtime/ai_snapshot.json`. Snapshot version 2 contains:

- the active setup and fixed levels;
- X/A/B/C/D swings and ratios;
- C-D leg statistics and post-D reaction;
- multi-timeframe context and indicators;
- historical touches, live flow and `setup_status`;
- compatible raw candle arrays for charting;
- deterministic `derived_features` for diagnostics.

The agent must return strict JSON with `decision`, Serbian explanation fields, path assessment, timestamps, and an `order` object only for `PLACE_ENTRY_ORDER_NOW` (otherwise `null`). The agent never claims that an order was executed, and `ENTRY_MISSED` is not a valid decision.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite covers setup validation and timezone handling, setup-scoped persistence, XABCD and post-D calculations, lifecycle rules, the post-AI guard, and snapshot/agent-output integration.
