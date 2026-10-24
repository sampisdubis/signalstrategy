#!/usr/bin/env python3
"""Live, configurable setup monitor. Alerts only; never places exchange orders."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import websockets

from analysis_features import build_derived_features, historical_touches, interval_ms
from decision_guard import TERMINAL_STATES, guard_ai_result
from setup_config import (
    Setup,
    SetupValidationError,
    compact_candles,
    default_operational_config,
    utc_iso,
)
from setup_store import (
    CONFIG_PATH,
    RUNTIME,
    append_event,
    config_file_changed,
    load_raw_config,
    load_setup,
    atomic_write_json,
    load_state_for_setup,
    read_armed_flag,
    save_state,
    snapshot_path,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HEARTBEAT_PATH = RUNTIME / "heartbeat.json"
STOP_PATH = RUNTIME / "stop.flag"
INSTANCE_LOCK_PORT = int(os.getenv("BTCW_MONITOR_LOCK_PORT", "48623"))
_instance_lock: socket.socket | None = None
_owns_instance = False

ENDPOINTS = {
    ("binance", "spot"): {
        "label": "Binance Spot",
        "rest": "https://api.binance.com",
        "ws": "wss://stream.binance.com:9443/stream",
        "time": "/api/v3/time",
        "ticker": "/api/v3/ticker/price",
        "klines": "/api/v3/klines",
    },
    ("binance", "futures"): {
        "label": "Binance USDⓈ-M Futures",
        "rest": "https://fapi.binance.com",
        "ws": "wss://fstream.binance.com/stream",
        "time": "/fapi/v1/time",
        "ticker": "/fapi/v1/ticker/price",
        "klines": "/fapi/v1/klines",
    },
}


def exchange_config(setup: Setup) -> dict[str, Any]:
    base = ENDPOINTS.get((setup.exchange, setup.market_type), ENDPOINTS[("binance", "spot")])
    config = dict(base)
    if os.getenv("BINANCE_REST_BASE"):
        config["rest"] = os.environ["BINANCE_REST_BASE"]
    if os.getenv("BINANCE_WS_BASE"):
        config["ws"] = os.environ["BINANCE_WS_BASE"]
    return config


def get_json(endpoint: dict[str, Any], path: str, params: dict[str, Any] | None = None) -> Any:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    req = urllib.request.Request(
        endpoint["rest"] + path + query,
        headers={"User-Agent": "signalstrategy/2.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_kline(row: list[Any]) -> dict[str, Any]:
    return {
        "open_time_ms": int(row[0]),
        "open_time_utc": utc_iso(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time_ms": int(row[6]),
        "closed": int(row[6]) < int(time.time() * 1000),
        "quote_volume": float(row[7]),
        "trades": int(row[8]),
        "taker_buy_base": float(row[9]),
        "taker_buy_quote": float(row[10]),
    }


class EntryMonitor:
    def __init__(
        self,
        setup: Setup,
        config: dict[str, Any],
        *,
        runtime: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.setup = setup
        self.config_path = config_path or CONFIG_PATH
        self.runtime = runtime or RUNTIME
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.cfg = default_operational_config()
        self.cfg.update({key: value for key, value in config.items() if key in self.cfg})
        self.endpoint = exchange_config(setup)
        self.state = load_state_for_setup(setup, runtime=self.runtime)
        self.price: float | None = None
        self.price_event_ms: int | None = None
        self.last_price_samples: deque[tuple[float, float]] = deque(maxlen=2000)
        self.recent_trades: deque[dict[str, Any]] = deque(maxlen=30)
        self.primary: list[dict[str, Any]] = []
        self.k1m: list[dict[str, Any]] = []
        self.k5: list[dict[str, Any]] = []
        self.k1h: list[dict[str, Any]] = []
        self.history_minutes: list[dict[str, Any]] = []
        self.last_zone_eval = 0.0
        self.ai_running = False
        self.pending_reason: str | None = None
        self.last_market_msg_monotonic = time.monotonic()
        self.config_changed = bool(self.state.get("config_changed_while_running"))

    # ------------------------------------------------------------------ state
    def write_heartbeat(self) -> None:
        payload = {
            "pid": os.getpid(),
            "epoch": time.time(),
            "utc": utc_iso(),
            "setup_id": self.setup.setup_id,
            "setup_fingerprint": self.setup.fingerprint(),
            "decision_state": self.state.get("decision_state"),
            "price": self.price,
            "price_event_ms": self.price_event_ms,
            "ai_running": self.ai_running,
            "config_changed": self.config_changed,
        }
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HEARTBEAT_PATH)

    def log_event(self, event: dict[str, Any]) -> None:
        append_event({"setup_id": self.setup.setup_id, **event}, runtime=self.runtime)

    def notify(self, title: str, body: str) -> None:
        print("\n" + "=" * 78, flush=True)
        print(f"{title}\n{body}", flush=True)
        print("\a" * 2, end="", flush=True)
        self.log_event({"type": "notification", "title": title, "body": body})

    # ------------------------------------------------------------------- data
    def fetch_klines(
        self,
        interval: str,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "symbol": self.setup.symbol,
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        raw = get_json(self.endpoint, self.endpoint["klines"], params)
        return [parse_kline(row) for row in raw]

    def fetch_klines_since(self, interval: str, start_time: int, end_time: int) -> list[dict[str, Any]]:
        step = interval_ms(interval)
        cursor = start_time
        rows: list[dict[str, Any]] = []
        while cursor <= end_time:
            page = self.fetch_klines(interval, limit=1000, start_time=cursor, end_time=end_time)
            if not page:
                break
            rows.extend(page)
            next_cursor = page[-1]["open_time_ms"] + step
            if next_cursor <= cursor:
                raise RuntimeError("Binance kline pagination failed to advance")
            cursor = next_cursor
            if len(page) < 1000:
                break
        by_open = {row["open_time_ms"]: row for row in rows if start_time <= row["open_time_ms"] <= end_time}
        return [by_open[key] for key in sorted(by_open)]

    def load_pattern_context(self, now_ms: int) -> None:
        main_step = interval_ms(self.setup.main_interval)
        primary_start = max(self.setup.x_ms - 80 * main_step, 0)
        self.primary = self.fetch_klines_since(self.setup.main_interval, primary_start, now_ms)
        if self.setup.main_interval == "1h":
            self.k1h = self.primary
        else:
            k1h_start = max(self.setup.x_ms - 80 * 3_600_000, 0)
            self.k1h = self.fetch_klines_since("1h", k1h_start, now_ms)
        k5_start = max(self.setup.d_ms - 12 * 3_600_000, now_ms - 288 * 300_000, 0)
        self.k5 = self.fetch_klines_since("5m", k5_start, now_ms)

    def history_status(self, minute_bars: list[dict[str, Any]] | None = None) -> tuple[str, str]:
        touches = historical_touches(self.setup, minute_bars if minute_bars is not None else self.history_minutes)
        return str(touches["status"]), str(touches["reason"])

    def resolved_history_status(
        self,
        status: str,
        reason: str,
        *,
        armed: bool | None = None,
    ) -> tuple[str, str]:
        is_armed = bool(self.state.get("entry_order_armed")) if armed is None else bool(armed)
        if not is_armed:
            return status, reason
        if status == "TARGET_REACHED_BEFORE_ENTRY":
            return (
                "TARGET_REACHED_CHECK_ORDER",
                reason + " Entry nalog je potvrden kao postavljen; proveri izvrsenje i poziciju na berzi.",
            )
        if status == "SETUP_INVALIDATED":
            return (
                status,
                reason + " Ako je entry nalog jos otvoren i neizvrsen, rucno ga otkazi; monitor ne upravlja nalozima.",
            )
        return status, reason

    async def bootstrap(self) -> bool:
        label = self.endpoint["label"]
        print(f"[{utc_iso()}] Preuzimam {label} podatke za {self.setup.symbol}...")
        try:
            server = get_json(self.endpoint, self.endpoint["time"])
            ticker = get_json(self.endpoint, self.endpoint["ticker"], {"symbol": self.setup.symbol})
            self.price = float(ticker["price"])
            self.price_event_ms = int(server["serverTime"])
            now_ms = int(server["serverTime"])
            self.load_pattern_context(now_ms)
            minutes = self.fetch_klines_since("1m", self.setup.d_ms, now_ms)
            self.history_minutes = minutes
            limit_1m = int(self.cfg.get("snapshot_limit_1m", 240))
            self.k1m = minutes[-limit_1m:]
            flag_exists, flag_content = read_armed_flag(runtime=self.runtime)
            flag_is_current = flag_exists and flag_content in ("", "armed", self.setup.setup_id)
            armed_now = bool(
                self.state.get("entry_order_armed")
                or (flag_is_current and self.state.get("signal_sent"))
            )
            status, reason = self.history_status(minutes)
            status, reason = self.resolved_history_status(status, reason, armed=armed_now)
            print(f"Snapshot: price={self.price:.2f}; exchange_time={utc_iso(now_ms)}; history={status}")
            print(reason)
            self.log_event(
                {
                    "type": "bootstrap",
                    "price": self.price,
                    "exchange_time_utc": utc_iso(now_ms),
                    "history_status": status,
                    "history_reason": reason,
                }
            )
            if status != "ACTIVE_WATCH":
                self.state["entry_order_armed"] = armed_now
                self.state["decision_state"] = status
                self.state["setup_terminal_reason"] = reason
                save_state(self.state, runtime=self.runtime)
                self.notify(f"SETUP NIJE AKTIVAN: {status}", reason)
                return False

            self.state["setup_terminal_reason"] = None
            if flag_is_current and self.state.get("signal_sent"):
                self.state["entry_order_armed"] = True
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
            else:
                self.state["entry_order_armed"] = False
                self.state["signal_sent"] = False
                self.state["decision_state"] = (
                    "WAIT_FOR_ENTRY_ZONE"
                    if self.setup.past_entry_before_target(self.price)
                    else "WAIT_FOR_CONFIRMATION"
                )
            save_state(self.state, runtime=self.runtime)
            if self.state.get("entry_order_armed"):
                print("Postojeća armed potvrda je pronađena; pratim nivoe i upozorenja, bez nove AI preporuke za ulazak.")
            elif self.setup.in_pre_entry_zone(self.price):
                asyncio.create_task(self.evaluate("initial_live_snapshot"))
            else:
                print("Cena je na pravoj strani ENTRY ka TG1; čekam povratak u zonu pre ENTRY. Prethodni ENTRY dodir nije terminalan.")
            return True
        except Exception as exc:
            self.notify(
                "DATA_INVALID — monitor nije spreman",
                f"Ne mogu da preuzmem pouzdane {label} podatke: {type(exc).__name__}: {exc}",
            )
            self.log_event({"type": "bootstrap_error", "error": repr(exc)})
            return False

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, trigger: str) -> dict[str, Any]:
        now = time.time()
        now_ms = int(now * 1000)
        kept = [(ts, price) for ts, price in self.last_price_samples if now - ts <= 60]
        old_price = kept[0][1] if kept else (self.price or 0.0)
        raw_change_pct = ((self.price / old_price) - 1.0) * 100.0 if old_price else 0.0
        distance_pct = self.setup.approach_gap_pct(self.price)
        stream_age = max(0.0, time.monotonic() - self.last_market_msg_monotonic)
        price_age = max(0.0, (now_ms - self.price_event_ms) / 1000) if self.price_event_ms else None
        primary = sorted(self.primary, key=lambda row: int(row["open_time_ms"]))
        limits = {
            "1m": int(self.cfg.get("snapshot_limit_1m", 240)),
            "5m": int(self.cfg.get("snapshot_limit_5m", 288)),
            "primary": int(self.cfg.get("snapshot_limit_primary", 720)),
        }
        derived = build_derived_features(
            self.setup,
            primary_candles=primary,
            candles_1h=self.k1h,
            candles_5m=self.k5,
            candles_1m=self.k1m,
            live_price=self.price,
            live_price_ms=self.price_event_ms,
            now_ms=now_ms,
            recent_trades=list(self.recent_trades),
            candles_1m_history=self.history_minutes,
        )
        touches = derived["historical_touches"]
        before_x = [row for row in primary if int(row["open_time_ms"]) < self.setup.x_ms][-100:]
        pattern_rows = [
            row for row in primary
            if self.setup.x_ms <= int(row["open_time_ms"]) <= self.setup.d_ms
        ]
        after_d = [row for row in primary if int(row["open_time_ms"]) > self.setup.d_ms]
        post_d_1m = [row for row in self.k1m if int(row["open_time_ms"]) >= self.setup.d_ms]
        post_d_5m = [row for row in self.k5 if int(row["open_time_ms"]) >= self.setup.d_ms]
        post_d_1h = [row for row in self.k1h if int(row["open_time_ms"]) >= self.setup.d_ms]
        forming = derived.get("forming_candles") or {}
        order_type = str(self.cfg.get("order_type_for_upward_crossing", "STOP_LOSS_LIMIT"))
        time_in_force = str(self.cfg.get("time_in_force", "GTC"))
        return {
            "snapshot_version": 2,
            "setup_id": self.setup.setup_id,
            "purpose": (
                "Evaluate whether to recommend the fixed ENTRY order now. The setup remains "
                "eligible unless TG1 or SL has been touched since D; an earlier ENTRY crossing "
                "alone is not terminal. Use derived_features first, then verify in raw candles."
            ),
            "trigger": trigger,
            "exchange": self.endpoint["label"],
            "exchange_id": self.setup.exchange,
            "market_type": self.setup.market_type,
            "symbol": self.setup.symbol,
            "data_as_of_utc": utc_iso(now_ms),
            "setup": {
                **self.setup.public(),
                "entry": self.setup.entry,
                "tg1": self.setup.tg1,
                "sl": self.setup.sl,
                "order": self.setup.order_details(order_type=order_type, time_in_force=time_in_force),
            },
            "fixed_levels": {"ENTRY": self.setup.entry, "TG1": self.setup.tg1, "SL": self.setup.sl},
            "live_price": self.price,
            "price_exchange_timestamp_utc": utc_iso(self.price_event_ms) if self.price_event_ms else None,
            "price_age_seconds": round(price_age, 2) if price_age is not None else None,
            "last_message_age_seconds": round(stream_age, 2),
            "distance_to_entry_pct": round(distance_pct, 4) if distance_pct is not None else None,
            "price_change_last_60s_pct": round(raw_change_pct, 4),
            "entry_cross_direction": "upward" if self.setup.is_long else "downward",
            "upward_entry_cross_seen_by_this_process": self.state.get("entry_crossing_time_utc") is not None,
            "entry_crossing_time_utc": self.state.get("entry_crossing_time_utc"),
            "entry_touched_since_D": bool(touches["entry"]["touched"]),
            "tg1_touched_since_D": bool(touches["tg1"]["touched"]),
            "sl_touched_since_D": bool(touches["sl"]["touched"]),
            "currently_in_entry_zone": self.setup.in_pre_entry_zone(self.price),
            "order_already_armed": bool(self.state.get("entry_order_armed")),
            "setup_status": {
                "status": touches["status"],
                "reason": touches["reason"],
                "reason_short": touches["reason_short"],
                "entry_touched_before_terminal": touches["entry_touched_before_terminal"],
                "bars_since_d": touches["bars_since_d"],
            },
            "history": {
                "context_before_x": {
                    "timeframe": self.setup.main_interval,
                    "candles": compact_candles(before_x, 100),
                },
                "pattern_x_to_d": {
                    "timeframe": self.setup.main_interval,
                    "X": utc_iso(self.setup.x_ms),
                    "D": utc_iso(self.setup.d_ms),
                    "candles": compact_candles(pattern_rows, limits["primary"]),
                },
                "post_d": {
                    "primary_timeframe": self.setup.main_interval,
                    "primary": compact_candles(after_d, limits["primary"]),
                    "1m": compact_candles(post_d_1m, limits["1m"]),
                    "5m": compact_candles(post_d_5m, limits["5m"]),
                    "1h": compact_candles(post_d_1h, limits["5m"]),
                },
                "current_live": {
                    "price": self.price,
                    "price_exchange_timestamp_utc": utc_iso(self.price_event_ms) if self.price_event_ms else None,
                    "last_message_age_seconds": round(stream_age, 2),
                    "price_age_seconds": round(price_age, 2) if price_age is not None else None,
                    "recent_trades": list(self.recent_trades)[-20:],
                    "forming_candles": forming,
                },
            },
            "derived_features": derived,
            # Backward-compatible flat candle fields used by the existing dashboard.
            "candles_1m": compact_candles(self.k1m, limits["1m"]),
            "candles_5m": compact_candles(self.k5, limits["5m"]),
            "candles_1h": compact_candles(self.k1h, limits["primary"]),
            "candles_primary": compact_candles(primary, limits["primary"]),
            "recent_live_trades": list(self.recent_trades)[-20:],
            "constraints": [
                "Never alter fixed levels or timestamps.",
                "No risk/reward, win rate, expected return or position sizing analysis.",
                "A historical ENTRY touch is not terminal; only TG1 or SL ends the setup.",
                "Do not invent missing candles, ratios, indicators or future events.",
                "No automatic order placement; the agent only recommends.",
            ],
        }

    # ------------------------------------------------------------------- AI
    async def evaluate(self, trigger: str) -> None:
        if self.config_changed:
            return
        terminal_map = {
            "TARGET_REACHED_BEFORE_ENTRY",
            "TARGET_REACHED_CHECK_ORDER",
            "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS",
            "SETUP_INVALIDATED",
        }
        if self.state.get("decision_state") in terminal_map:
            return
        if self.state.get("entry_order_armed"):
            return
        if self.ai_running:
            self.pending_reason = trigger
            return
        if self.price is None or self.price_event_ms is None:
            return
        price_age = max(0.0, (int(time.time() * 1000) - self.price_event_ms) / 1000)
        if price_age > float(self.cfg["max_market_data_age_seconds"]):
            self.state["decision_state"] = "DATA_INVALID"
            save_state(self.state, runtime=self.runtime)
            self.notify(
                "DATA_INVALID — zastareli market podaci",
                f"Poslednji Binance timestamp je star {price_age:.1f}s. Ne izdaje se ENTRY signal.",
            )
            return
        if self.setup.target_hit(self.price):
            self.state["decision_state"] = "TARGET_REACHED_BEFORE_ENTRY"
            self.state["setup_terminal_reason"] = f"Cena {self.price:.2f} je dostigla TG1 {self.setup.tg1:.2f} pre nove entry odluke."
            save_state(self.state, runtime=self.runtime)
            self.notify("SETUP ZAVRŠEN — TG1 dostignut", self.state["setup_terminal_reason"])
            return
        if self.setup.stop_hit(self.price):
            self.state["decision_state"] = "SETUP_INVALIDATED"
            self.state["setup_terminal_reason"] = f"Cena {self.price:.2f} je dostigla SL {self.setup.sl:.2f}."
            save_state(self.state, runtime=self.runtime)
            self.notify("SETUP PONIŠTEN — SL dostignut", self.state["setup_terminal_reason"])
            return
        if self.setup.past_entry_before_target(self.price):
            self.state["decision_state"] = "WAIT_FOR_ENTRY_ZONE"
            save_state(self.state, runtime=self.runtime)
            return
        if not self.setup.in_pre_entry_zone(self.price):
            return

        self.ai_running = True
        self.pending_reason = None
        try:
            payload = self.snapshot(trigger)
            target_snapshot = snapshot_path(self.runtime)
            atomic_write_json(target_snapshot, payload)
            snapshot_info = {
                "price": self.price,
                "price_event_ms": self.price_event_ms,
                "snapshot_time_utc": payload["data_as_of_utc"],
                "monotonic": time.monotonic(),
                "derived": payload["derived_features"],
            }
            prompt = (
                "Analyze the attached live snapshot file ai_snapshot.json and return exactly one JSON object "
                "matching the schema in your system instructions. Use the snapshot's setup object, "
                "fixed_levels, history blocks, raw candles and derived_features as the only market data. "
                "Do not place an order. Do not use fixed BTC example values. "
                f"The setup_id is {self.setup.setup_id}; echo it in the response."
            )
            opencode = shutil.which(os.getenv("OPENCODE_BIN", "opencode"))
            if not opencode:
                raise RuntimeError("OpenCode CLI nije pronađen u PATH-u. Instaliraj/prijavi OpenCode pre pokretanja monitora.")
            proc = await asyncio.create_subprocess_exec(
                opencode,
                "run",
                prompt,
                "--agent",
                "entry-supervisor",
                "--format",
                "json",
                "--dir",
                str(Path(__file__).resolve().parent),
                "--file",
                str(target_snapshot),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("OpenCode agent nije odgovorio u roku od 120s; trenutna odluka ostaje WAIT.")
            raw = stdout_b.decode("utf-8", "replace")
            if proc.returncode != 0:
                raise RuntimeError(f"opencode run vratio je kod {proc.returncode}: {stderr_b.decode('utf-8', 'replace')[:1200]}")
            result = extract_json_from_agent_output(raw)

            price_age_after = max(0.0, (int(time.time() * 1000) - self.price_event_ms) / 1000)
            stream_age_after = max(0.0, time.monotonic() - self.last_market_msg_monotonic)
            stale_age = max(price_age_after, stream_age_after)
            touches = snapshot_info["derived"]["historical_touches"]
            result = guard_ai_result(
                result,
                setup=self.setup,
                live_price=self.price,
                live_price_age_seconds=stale_age,
                max_data_age_seconds=float(self.cfg["max_market_data_age_seconds"]),
                snapshot_price=snapshot_info["price"],
                snapshot_time_utc=snapshot_info["snapshot_time_utc"],
                terminal_state=self.state.get("decision_state"),
                terminal_reason=self.state.get("setup_terminal_reason"),
                order_armed=bool(self.state.get("entry_order_armed")),
                target_touched_since_d=bool(touches["tg1"]["touched"]),
                stop_touched_since_d=bool(touches["sl"]["touched"]),
                stale_price_move_pct=float(self.cfg.get("stale_ai_price_move_pct", 0.002)),
                order_type=str(self.cfg.get("order_type_for_upward_crossing", "STOP_LOSS_LIMIT")),
                time_in_force=str(self.cfg.get("time_in_force", "GTC")),
                now_utc=utc_iso(),
            )
            prev = self.state.get("decision_state")
            self.state["decision_state"] = result.get("decision", "DATA_INVALID")
            self.state["last_decision"] = result
            self.state["last_decision_time_utc"] = utc_iso()
            self.state["last_ai_eval_monotonic"] = time.monotonic()
            self.state["last_ai_snapshot_utc"] = snapshot_info["snapshot_time_utc"]
            self.state["last_ai_snapshot_price"] = snapshot_info["price"]
            if result.get("decision") in TERMINAL_STATES:
                self.state["setup_terminal_reason"] = result.get("reason", "")
            save_state(self.state, runtime=self.runtime)
            self.log_event(
                {
                    "type": "ai_decision",
                    "trigger": trigger,
                    "previous_state": prev,
                    "decision": result,
                    "live_price_at_log": self.price,
                    "snapshot_price": snapshot_info["price"],
                    "snapshot_time_utc": snapshot_info["snapshot_time_utc"],
                }
            )
            if result.get("stale_guard", {}).get("changed"):
                self.log_event({"type": "ai_guard", "trigger": trigger, "guard": result.get("stale_guard")})
            if result.get("decision") == "PLACE_ENTRY_ORDER_NOW" and prev != "PLACE_ENTRY_ORDER_NOW":
                self.state["signal_sent"] = True
                save_state(self.state, runtime=self.runtime)
                self.notify(
                    "ACTION: POSTAVI USLOVNI ENTRY NALOG SADA",
                    json.dumps(result, ensure_ascii=False, indent=2),
                )
            elif result.get("decision") in TERMINAL_STATES | {"DATA_INVALID"} and result.get("decision") != prev:
                self.notify(f"STANJE PROMENJENO: {result.get('decision')}", str(result.get("reason", "")))
            else:
                print(
                    f"[{utc_iso()}] AI={result.get('decision')} price={self.price:.2f} "
                    f"trigger={trigger} — {result.get('reason', '')}",
                    flush=True,
                )
            if result.get("stale_guard", {}).get("changed") and result.get("decision") in (
                "WAIT_FOR_CONFIRMATION",
                "WAIT_FOR_ENTRY_ZONE",
            ) and self.setup.in_pre_entry_zone(self.price):
                self.pending_reason = self.pending_reason or "stale_ai_recheck"
        except Exception as exc:
            self.log_event({"type": "ai_error", "trigger": trigger, "error": repr(exc)})
            self.notify(
                "AI PROCENA NIJE USPELA — nema novog ENTRY signala",
                f"Okidač: {trigger}\nGreška: {type(exc).__name__}: {exc}\nMonitor nastavlja da prati podatke; nije poslat nalog.",
            )
        finally:
            self.ai_running = False
            pending = self.pending_reason
            self.pending_reason = None
            if pending:
                asyncio.create_task(self.evaluate("coalesced:" + pending))

    # -------------------------------------------------------------- live feed
    def on_trade(self, data: dict[str, Any]) -> None:
        price = float(data["p"])
        event_ms = int(data.get("T", data.get("E", int(time.time() * 1000))))
        previous_price = self.price
        self.price = price
        self.price_event_ms = event_ms
        self.last_market_msg_monotonic = time.monotonic()
        now = time.time()
        self.last_price_samples.append((now, price))
        self.recent_trades.append(
            {
                "time_utc": utc_iso(event_ms),
                "price": price,
                "qty": float(data.get("q", 0)),
                "buyer_is_maker": bool(data.get("m", False)),
            }
        )

        if self.state.get("decision_state") in TERMINAL_STATES:
            return
        if self.config_changed:
            return

        flag_exists, flag_content = read_armed_flag(runtime=self.runtime)
        if flag_exists and flag_content in ("", "armed", self.setup.setup_id) and self.state.get("signal_sent"):
            self.state["entry_order_armed"] = True
            if self.state.get("decision_state") != "ENTRY_ORDER_ARMED":
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
                save_state(self.state, runtime=self.runtime)

        if self.setup.target_hit(price):
            self.state["decision_state"] = (
                "TARGET_REACHED_CHECK_ORDER" if self.state.get("entry_order_armed") else "TARGET_REACHED_BEFORE_ENTRY"
            )
            self.state["setup_terminal_reason"] = f"Tržište je dostiglo TG1 {self.setup.tg1:.2f} na {price:.2f}. Proveri stvarni status naloga/pozicije na berzi."
            save_state(self.state, runtime=self.runtime)
            self.notify("TG1 DOSTIGNUT — proveri status naloga", self.state["setup_terminal_reason"])
            return
        if self.setup.stop_hit(price):
            self.state["decision_state"] = "SETUP_INVALIDATED"
            self.state["setup_terminal_reason"] = f"Tržište je dostiglo SL {self.setup.sl:.2f} na {price:.2f}. Proveri stvarni status naloga/pozicije na berzi."
            if self.state.get("entry_order_armed"):
                self.state["setup_terminal_reason"] += " Ako je entry nalog još otvoren i neizvršen, ručno ga otkaži; monitor ne može da upravlja nalozima."
            save_state(self.state, runtime=self.runtime)
            self.notify("SL DOSTIGNUT — setup poništen", self.state["setup_terminal_reason"])
            return

        if self.state.get("entry_order_armed"):
            if self.setup.entry_touched({"open": price, "high": price, "low": price}) and not self.state.get("entry_crossing_time_utc"):
                self.state["entry_crossing_time_utc"] = utc_iso(event_ms)
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
                save_state(self.state, runtime=self.runtime)
                self.notify(
                    "ENTRY nivo dostignut — proveri izvršenje naloga",
                    f"Poslednja cena={price:.2f}. STOP_LOSS_LIMIT nalog je mogao da se aktivira, ali izvršenje po {self.setup.entry:.2f} nije garantovano. Proveri stvarni status naloga na berzi.",
                )
            return

        crossed = self.setup.entry_crossed(previous_price, price)
        if crossed and not self.state.get("entry_crossing_time_utc"):
            self.state["entry_crossing_time_utc"] = utc_iso(event_ms)

        if self.setup.past_entry_before_target(price):
            if self.state.get("decision_state") != "WAIT_FOR_ENTRY_ZONE":
                self.state["decision_state"] = "WAIT_FOR_ENTRY_ZONE"
                save_state(self.state, runtime=self.runtime)
                if crossed:
                    self.notify(
                        "CENA JE PROŠLA KROZ ENTRY — SETUP JOŠ NIJE ZAVRŠEN",
                        f"Cena={price:.2f}; ENTRY={self.setup.entry:.2f}; TG1 nije dostignut. Čekam povratak na stranu pre ENTRY radi nove procene.",
                    )
            return

        previous_past_entry = previous_price is not None and self.setup.past_entry_before_target(previous_price)
        returned_to_zone = previous_past_entry and self.setup.in_pre_entry_zone(price)
        if returned_to_zone:
            self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
            self.state["setup_terminal_reason"] = None
            if not self.state.get("entry_order_armed"):
                self.state["signal_sent"] = False
            save_state(self.state, runtime=self.runtime)
            asyncio.create_task(self.evaluate("price_returned_to_pre_entry_zone"))
            return

        if not self.setup.in_pre_entry_zone(price):
            return

        if self.state.get("decision_state") in ("DATA_INVALID", "WAIT_FOR_ENTRY_ZONE"):
            self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
            save_state(self.state, runtime=self.runtime)

        gap_pct = self.setup.approach_gap_pct(price)
        gap_fraction = (gap_pct / 100.0) if gap_pct is not None else None
        samples = [sample for sample in self.last_price_samples if now - sample[0] <= 60]
        fast = False
        if samples and samples[0][1] > 0:
            fast = self.setup.directional_move_pct(samples[0][1], price) >= float(self.cfg["fast_move_pct_60s"])
        near = gap_fraction is not None and 0 <= gap_fraction <= float(self.cfg["wake_distance_pct"])
        trigger_cooldown = float(self.cfg.get("live_trigger_cooldown_seconds", 30))
        if (near or fast) and now - self.last_zone_eval >= trigger_cooldown:
            self.last_zone_eval = now
            asyncio.create_task(self.evaluate("live_price_approaching_entry" if near else "rapid_move_toward_entry"))

    def on_kline(self, data: dict[str, Any]) -> None:
        kline = data.get("k", {})
        interval = kline.get("i")
        bar = {
            "open_time_ms": int(kline["t"]),
            "open_time_utc": utc_iso(int(kline["t"])),
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "trades": int(kline["n"]),
            "closed": bool(kline["x"]),
            "close_time_ms": int(kline["T"]),
            "quote_volume": float(kline.get("q", 0)),
            "taker_buy_base": float(kline.get("V", 0)),
            "taker_buy_quote": float(kline.get("Q", 0)),
        }
        targets: list[tuple[list[dict[str, Any]], int]] = []
        if interval == "1m":
            targets.append((self.k1m, int(self.cfg.get("snapshot_limit_1m", 240))))
        elif interval == "5m":
            targets.append((self.k5, int(self.cfg.get("snapshot_limit_5m", 288))))
        elif interval == "1h":
            targets.append((self.k1h, int(self.cfg.get("snapshot_limit_primary", 720))))
        if interval == self.setup.main_interval:
            targets.append((self.primary, int(self.cfg.get("snapshot_limit_primary", 720))))
        seen_ids: set[int] = set()
        for target, limit in targets:
            if id(target) in seen_ids:
                continue
            seen_ids.add(id(target))
            self.upsert_candle(target, bar, limit)
        if not bar["closed"]:
            return

        if interval == "1m":
            self.upsert_candle(self.history_minutes, bar, 120_000)
            if self.state.get("decision_state") not in TERMINAL_STATES and not self.config_changed:
                status, reason = self.history_status(self.history_minutes)
                status, reason = self.resolved_history_status(status, reason)
                if status != "ACTIVE_WATCH":
                    self.state["decision_state"] = status
                    self.state["setup_terminal_reason"] = reason
                    save_state(self.state, runtime=self.runtime)
                    self.notify(f"SETUP STANJE: {status}", reason)
                    return
        asyncio.create_task(self.evaluate(f"closed_{interval}_candle"))

    @staticmethod
    def upsert_candle(target: list[dict[str, Any]], bar: dict[str, Any], limit: int) -> None:
        if target and target[-1]["open_time_ms"] == bar["open_time_ms"]:
            target[-1] = bar
        elif not target or bar["open_time_ms"] > target[-1]["open_time_ms"]:
            target.append(bar)
            del target[:-max(1, limit)]

    # --------------------------------------------------------------- watchdog
    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            if STOP_PATH.exists():
                try:
                    STOP_PATH.unlink()
                except OSError:
                    pass
                save_state(self.state, runtime=self.runtime)
                try:
                    HEARTBEAT_PATH.unlink()
                except FileNotFoundError:
                    pass
                self.notify("MONITOR ZAUSTAVLJEN", "Zaustavljen iz GUI panela. Nalozi na Binance-u se ne diraju.")
                os._exit(0)
            self.write_heartbeat()
            if not self.config_changed and config_file_changed(self.setup, self.config_path):
                self.config_changed = True
                self.state["config_changed_while_running"] = True
                self.state["decision_state"] = "DATA_INVALID"
                self.state["setup_terminal_reason"] = (
                    "Konfiguracija je promenjena dok monitor radi. Monitor ne usvaja izmene u letu; "
                    "zaustavi ga, proveri novi setup i pokreni ponovo."
                )
                save_state(self.state, runtime=self.runtime)
                self.log_event({"type": "config_changed_while_running"})
                self.notify("KONFIGURACIJA PROMENJENA — monitor ne usvaja novi setup", self.state["setup_terminal_reason"])
                continue
            if self.config_changed:
                continue
            age = time.monotonic() - self.last_market_msg_monotonic
            if age > float(self.cfg["max_market_data_age_seconds"]):
                if self.state.get("decision_state") not in TERMINAL_STATES and self.state.get("decision_state") != "DATA_INVALID":
                    self.state["decision_state"] = "DATA_INVALID"
                    save_state(self.state, runtime=self.runtime)
                    self.notify(
                        "DATA_INVALID — nema svežih market podataka",
                        f"Bez Binance stream poruke {age:.1f}s. Ne izdavati nove ENTRY preporuke dok se stream ne oporavi.",
                    )
            elif self.state.get("decision_state") == "DATA_INVALID" and self.state.get("setup_terminal_reason") is None:
                self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
                save_state(self.state, runtime=self.runtime)
                asyncio.create_task(self.evaluate("market_stream_recovered"))

    async def run(self) -> None:
        if self.state.get("decision_state") not in TERMINAL_STATES:
            self.state["decision_state"] = "BOOTSTRAPPING"
            save_state(self.state, runtime=self.runtime)
        self.write_heartbeat()
        if not await self.bootstrap():
            return
        symbol = self.setup.symbol.lower()
        streams = f"{symbol}@aggTrade/{symbol}@kline_1m/{symbol}@kline_5m/{symbol}@kline_1h"
        if self.setup.main_interval not in ("1m", "5m", "1h"):
            streams += f"/{symbol}@kline_{self.setup.main_interval}"
        url = self.endpoint["ws"] + "?streams=" + streams
        local = self.setup.local_times()
        self.notify(
            "SETUP MONITOR JE AKTIVAN",
            (
                f"{self.setup.symbol} · {self.endpoint['label']} · {self.setup.direction.upper()}\n"
                f"ENTRY={self.setup.entry:.2f} | TG1={self.setup.tg1:.2f} | SL={self.setup.sl:.2f}\n"
                f"X={local['x']} | A={local['a']} | B={local['b']} | C={local['c']} | D={local['d']} ({self.setup.timezone_label})\n"
                "Praćenje: aggTrade + 1m/5m/1h sveće. Samo odluke i upozorenja; ne postavlja stvarne naloge."
            ),
        )
        asyncio.create_task(self.watchdog())
        while True:
            try:
                print(f"[{utc_iso()}] Povezivanje na WebSocket...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**20) as ws:
                    async for raw in ws:
                        self.last_market_msg_monotonic = time.monotonic()
                        message = json.loads(raw)
                        data = message.get("data", message)
                        event = data.get("e")
                        if event == "aggTrade":
                            self.on_trade(data)
                        elif event == "kline":
                            self.on_kline(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log_event({"type": "websocket_disconnect", "error": repr(exc)})
                self.notify(
                    "BERZA STREAM PREKINUT",
                    f"{type(exc).__name__}: {exc}\nPokušavam ponovno povezivanje; za vreme prekida ne veruj starim signalima.",
                )
                await asyncio.sleep(2)
                if self.config_changed:
                    continue
                try:
                    server = get_json(self.endpoint, self.endpoint["time"])
                    now_ms = int(server["serverTime"])
                    ticker = get_json(self.endpoint, self.endpoint["ticker"], {"symbol": self.setup.symbol})
                    self.price = float(ticker["price"])
                    self.price_event_ms = now_ms
                    self.load_pattern_context(now_ms)
                    backfill_start = (
                        max(self.setup.d_ms, self.history_minutes[-1]["open_time_ms"])
                        if self.history_minutes
                        else self.setup.d_ms
                    )
                    recovered = self.fetch_klines_since("1m", backfill_start, now_ms)
                    merged = {row["open_time_ms"]: row for row in self.history_minutes}
                    merged.update({row["open_time_ms"]: row for row in recovered})
                    self.history_minutes = [merged[key] for key in sorted(merged)]
                    self.k1m = self.history_minutes[-int(self.cfg.get("snapshot_limit_1m", 240)):]
                    status, reason = self.history_status(self.history_minutes)
                    status, reason = self.resolved_history_status(status, reason)
                    if status != "ACTIVE_WATCH":
                        self.state["decision_state"] = status
                        self.state["setup_terminal_reason"] = reason
                        save_state(self.state, runtime=self.runtime)
                        self.notify(f"SETUP STANJE POSLE PREKIDA: {status}", reason)
                    elif self.setup.in_pre_entry_zone(self.price) and not self.state.get("entry_order_armed"):
                        self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
                        save_state(self.state, runtime=self.runtime)
                        asyncio.create_task(self.evaluate("after_stream_recovery_in_entry_zone"))
                except Exception as refresh_err:
                    self.log_event({"type": "rest_refresh_error", "error": repr(refresh_err)})


def extract_json_from_agent_output(raw: str) -> dict[str, Any]:
    texts: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(event.get("text"), str):
                    texts.append(event["text"])
                elif isinstance(event.get("message"), dict) and isinstance(event["message"].get("content"), str):
                    texts.append(event["message"]["content"])
                else:
                    texts.append(line)
            else:
                texts.append(line)
        except json.JSONDecodeError:
            texts.append(line)
    joined = "\n".join(texts) if texts else raw
    try:
        obj = json.loads(joined.strip())
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    except Exception:
        pass
    for start, char in enumerate(joined):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(joined)):
            current = joined[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = joined[start:end + 1]
                    if '"decision"' in candidate:
                        try:
                            value = json.loads(candidate)
                            if isinstance(value, dict) and "decision" in value:
                                return value
                        except Exception:
                            break
                    break
    raise ValueError("OpenCode output does not contain the required JSON decision. First 1500 chars: " + joined[:1500])


async def main() -> None:
    global _instance_lock, _owns_instance
    _instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_lock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        _instance_lock.listen(1)
    except OSError:
        print("Monitor je već pokrenut (drugi proces drži instancu). Zaustavi ga pre pokretanja novog.", flush=True)
        return
    _owns_instance = True
    try:
        config = load_raw_config()
        setup = load_setup()
    except SetupValidationError as exc:
        print("Neispravan config.json:")
        for error in exc.errors:
            print(f"- {error}")
        return
    print("Signal Strategy — decision support only, no auto-trading.")
    print(f"OpenCode: {shutil.which(os.getenv('OPENCODE_BIN', 'opencode')) or 'NOT FOUND'}")
    print(f"Setup: {setup.setup_id} | {setup.symbol} {setup.direction} {setup.main_interval} | {setup.exchange}/{setup.market_type}")
    print(f"Levels: ENTRY={setup.entry} TG1={setup.tg1} SL={setup.sl} | {setup.timezone_label}")
    monitor = EntryMonitor(setup, config)
    await monitor.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor zaustavljen ručno.")
    finally:
        if _instance_lock is not None:
            _instance_lock.close()
        if _owns_instance:
            try:
                HEARTBEAT_PATH.unlink()
            except FileNotFoundError:
                pass
