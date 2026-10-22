#!/usr/bin/env python3
"""Live BTCUSDT Spot entry monitor. Alerts only; never places exchange orders."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
LOG_PATH = RUNTIME / "events.jsonl"
STATE_PATH = RUNTIME / "state.json"
HEARTBEAT_PATH = RUNTIME / "heartbeat.json"
STOP_PATH = RUNTIME / "stop.flag"
INSTANCE_LOCK_PORT = int(os.getenv("BTCW_MONITOR_LOCK_PORT", "48623"))
_instance_lock: socket.socket | None = None
_owns_instance = False
REST_BASE = os.getenv("BINANCE_REST_BASE", "https://api.binance.com")
WS_BASE = os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443/stream")
SYMBOL = CFG["symbol"].lower()
ENTRY = float(CFG["entry"])
TG1 = float(CFG["tg1"])
SL = float(CFG["sl"])


def iso_utc_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


SETUP_X_MS = iso_utc_ms(CFG["setup_x_timestamp_utc"])
SETUP_C_MS = iso_utc_ms(CFG["setup_c_timestamp_utc"])
SETUP_D_MS = iso_utc_ms(CFG["setup_d_timestamp_utc"])


def utc_iso(ms: int | float | None = None) -> str:
    if ms is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).isoformat(timespec="seconds")


def log_event(event: dict[str, Any]) -> None:
    item = {"logged_at_utc": utc_iso(), **event}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def notify(title: str, body: str) -> None:
    print("\n" + "=" * 78, flush=True)
    print(f"{title}\n{body}", flush=True)
    print("\a" * 2, end="", flush=True)
    log_event({"type": "notification", "title": title, "body": body})


def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    req = urllib.request.Request(
        REST_BASE + path + query,
        headers={"User-Agent": "btc-entry-watch/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_kline(row: list[Any]) -> dict[str, Any]:
    return {
        "open_time_ms": int(row[0]),
        "open_time_utc": utc_iso(row[0]),
        "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
        "close": float(row[4]), "volume": float(row[5]),
        "close_time_ms": int(row[6]), "closed": int(row[6]) < int(time.time() * 1000),
        "quote_volume": float(row[7]), "trades": int(row[8]),
        "taker_buy_base": float(row[9]), "taker_buy_quote": float(row[10]),
    }


def fetch_klines(interval: str, limit: int = 100, start_time: int | None = None, end_time: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"symbol": CFG["symbol"], "interval": interval, "limit": min(limit, 1000)}
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time
    raw = get_json("/api/v3/klines", params)
    return [parse_kline(r) for r in raw]


def fetch_klines_since(interval: str, start_time: int, end_time: int) -> list[dict[str, Any]]:
    """Fetch every page from start_time through end_time (Binance caps each response at 1000 bars)."""
    interval_ms = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}.get(interval)
    if interval_ms is None:
        raise ValueError(f"Unsupported interval for paginated history: {interval}")
    cursor = start_time
    rows: list[dict[str, Any]] = []
    while cursor <= end_time:
        page = fetch_klines(interval, limit=1000, start_time=cursor, end_time=end_time)
        if not page:
            break
        rows.extend(page)
        next_cursor = page[-1]["open_time_ms"] + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline pagination failed to advance")
        cursor = next_cursor
        if len(page) < 1000:
            break
    # Deduplicate defensively and return chronological order.
    by_open = {row["open_time_ms"]: row for row in rows if start_time <= row["open_time_ms"] <= end_time}
    return [by_open[k] for k in sorted(by_open)]


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if s.get("setup_d_timestamp_utc") == CFG["setup_d_timestamp_utc"]:
                return s
        except Exception:
            pass
    return {
        "setup_d_timestamp_utc": CFG["setup_d_timestamp_utc"],
        "decision_state": "BOOTSTRAPPING",
        "signal_sent": False,
        "entry_order_armed": False,
        "last_decision": None,
        "last_ai_eval_monotonic": 0,
        "entry_crossing_time_utc": None,
        "setup_terminal_reason": None,
    }


def history_status(minute_bars: list[dict[str, Any]]) -> tuple[str, str]:
    """A previous ENTRY touch is NOT terminal. Only TG1 or SL ends this setup."""
    bars = sorted(
        (b for b in minute_bars if b["open_time_ms"] >= SETUP_D_MS),
        key=lambda b: b["open_time_ms"],
    )
    if not bars:
        return "DATA_INVALID", "Nema dostupnih 1m sveća od D; ne mogu da proverim da li su TG1 ili SL već bili dodirnuti."

    for b in bars:
        o, h, l = b["open"], b["high"], b["low"]
        t = b["open_time_utc"]
        # If the candle opens beyond an outcome level, that opening print establishes the first event.
        if o >= TG1:
            return "TARGET_REACHED_BEFORE_ENTRY", f"1m sveća {t} otvorila se na/iznad TG1 ({o:.2f}); trenutni setup je završio pre nove entry odluke."
        if o <= SL:
            return "SETUP_INVALIDATED", f"1m sveća {t} otvorila se na/ispod SL ({o:.2f}); trenutni setup je poništen."
        hit_tg1 = h >= TG1
        hit_sl = l <= SL
        # If both extremes occur within one candle whose open is between SL and TG1, ordering is unknown.
        if hit_tg1 and hit_sl:
            return "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", f"Ista 1m sveća {t} obuhvata i TG1 i SL; OHLC ne pokazuje kojim redosledom. Setup se ne tretira kao aktivan."
        if hit_sl:
            return "SETUP_INVALIDATED", f"SL ({SL:.2f}) je dodirnut u 1m sveći {t}; trenutni setup je poništen."
        if hit_tg1:
            return "TARGET_REACHED_BEFORE_ENTRY", f"TG1 ({TG1:.2f}) je dodirnut u 1m sveći {t}; trenutni setup je završio pre nove entry odluke."

    entry_touches = sum(1 for b in bars if b["high"] >= ENTRY or b["open"] >= ENTRY)
    tail = f" ENTRY je dodirnut u {entry_touches} istorijskih 1m sveća, ali sam dodir ENTRY nije terminalan."
    return "ACTIVE_WATCH", "Od D do sada nisu dodirnuti ni TG1 ni SL. Setup ostaje aktivan; ako je cena ponovo u zoni između SL i ENTRY, AI mora nastaviti analizu. Prethodni prelazak ENTRY sam po sebi ne znači ENTRY_MISSED." + tail


def extract_json_from_agent_output(raw: str) -> dict[str, Any]:
    texts: list[str] = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                # OpenCode JSON event streams may put model text in a part field.
                part = ev.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(ev.get("text"), str):
                    texts.append(ev["text"])
                elif isinstance(ev.get("message"), dict) and isinstance(ev["message"].get("content"), str):
                    texts.append(ev["message"]["content"])
        except json.JSONDecodeError:
            texts.append(line)
    joined = "\n".join(texts) if texts else raw
    try:
        obj = json.loads(joined.strip())
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    except Exception:
        pass
    # Extract the first balanced JSON object containing a decision field; nested order objects are supported.
    for start, ch in enumerate(joined):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(joined)):
            c = joined[end]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
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


class EntryMonitor:
    def __init__(self) -> None:
        self.state = load_state()
        self.price: float | None = None
        self.price_event_ms: int | None = None
        self.last_price_samples: deque[tuple[float, float]] = deque(maxlen=2000)  # (epoch seconds, price)
        self.recent_trades: deque[dict[str, Any]] = deque(maxlen=30)
        self.k1m: list[dict[str, Any]] = []
        self.k5: list[dict[str, Any]] = []
        self.k1h: list[dict[str, Any]] = []
        self.history_minutes: list[dict[str, Any]] = []
        self.last_k5_open: int | None = None
        self.last_k1h_open: int | None = None
        self.last_zone_eval = 0.0
        self.ai_running = False
        self.pending_reason: str | None = None
        self.last_market_msg_monotonic = time.monotonic()
        self.started_monotonic = time.monotonic()

    def write_heartbeat(self) -> None:
        payload = {
            "pid": os.getpid(),
            "epoch": time.time(),
            "utc": utc_iso(),
            "decision_state": self.state.get("decision_state"),
            "price": self.price,
            "price_event_ms": self.price_event_ms,
            "ai_running": self.ai_running,
        }
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HEARTBEAT_PATH)

    def load_pattern_context(self, now_ms: int) -> None:
        # Always include the full 1h structure from X (small pad) so the whole XABCD is verifiable.
        k1h_start = max(0, min(SETUP_X_MS - 3 * 3_600_000, now_ms), now_ms - 720 * 3_600_000)
        self.k1h = fetch_klines_since("1h", k1h_start, now_ms)
        # 5m execution context from C/D, capped to the last 24h for a bounded snapshot.
        k5_start = max(min(SETUP_C_MS, SETUP_D_MS, now_ms) - 3_600_000, now_ms - 288 * 300_000)
        self.k5 = fetch_klines_since("5m", k5_start, now_ms)

    async def bootstrap(self) -> bool:
        print(f"[{utc_iso()}] Preuzimam Binance Spot podatke za {CFG['symbol']}...")
        try:
            server = get_json("/api/v3/time")
            ticker = get_json("/api/v3/ticker/price", {"symbol": CFG["symbol"]})
            self.price = float(ticker["price"])
            self.price_event_ms = int(server["serverTime"])
            now_ms = int(server["serverTime"])
            self.load_pattern_context(now_ms)
            # Paginate all 1m candles since D; the setup remains live unless TG1 or SL has been touched.
            minutes = fetch_klines_since("1m", SETUP_D_MS, now_ms)
            self.history_minutes = minutes
            self.k1m = minutes[-120:]
            status, reason = history_status(minutes)
            print(f"Snapshot: price={self.price:.2f}; exchange_time={utc_iso(now_ms)}; history={status}")
            print(reason)
            log_event({"type": "bootstrap", "price": self.price, "exchange_time_utc": utc_iso(now_ms), "history_status": status, "history_reason": reason})
            if status != "ACTIVE_WATCH":
                if status == "SETUP_INVALIDATED" and self.state.get("entry_order_armed"):
                    reason += " Ako BUY entry nalog još stoji neizvršen u Binance-u, ručno ga otkaži i proveri status naloga/pozicije; monitor ne može da upravlja nalozima."
                self.state["decision_state"] = status
                self.state["setup_terminal_reason"] = reason
                save_state(self.state)
                notify(f"SETUP NIJE AKTIVAN: {status}", reason)
                return False
            # Migrate the old, incorrect ENTRY_MISSED state: an ENTRY touch is not a terminal event.
            self.state["setup_terminal_reason"] = None
            armed_flag_exists = (ROOT / "runtime" / "entry_order_armed.flag").exists()
            if armed_flag_exists and self.state.get("signal_sent"):
                self.state["entry_order_armed"] = True
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
            else:
                self.state["entry_order_armed"] = False
                # A legacy ENTRY_MISSED state or unacknowledged old signal must not block this still-active setup.
                self.state["signal_sent"] = False
                self.state["decision_state"] = "WAIT_FOR_ENTRY_ZONE" if self.price >= ENTRY else "WAIT_FOR_CONFIRMATION"
            save_state(self.state)
            if self.state.get("entry_order_armed"):
                print("Postojeća armed potvrda je pronađena; pratim nivoe i upozorenja, bez nove AI preporuke za ulazak.")
            elif self.price < ENTRY:
                # Start the first AI evaluation asynchronously so the WebSocket stream connects immediately;
                # never block live price observation while the model is reasoning.
                asyncio.create_task(self.evaluate("initial_live_snapshot"))
            else:
                print("Cena je iznad ENTRY, ali ispod TG1: nastavljam da pratim povratak u zonu; prethodni ENTRY dodir ne gasi setup.")
            return True
        except Exception as e:
            notify("DATA_INVALID — monitor nije spreman", f"Ne mogu da preuzmem pouzdane Binance Spot podatke: {type(e).__name__}: {e}")
            log_event({"type": "bootstrap_error", "error": repr(e)})
            return False

    def snapshot(self, trigger: str) -> dict[str, Any]:
        now = time.time()
        kept = [(ts, p) for ts, p in self.last_price_samples if now - ts <= 60]
        old_price = kept[0][1] if kept else (self.price or 0.0)
        delta_pct = ((self.price / old_price) - 1) if old_price else 0.0
        distance_pct = ((ENTRY - self.price) / ENTRY) if self.price is not None else None
        def compact(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
            out = []
            for c in rows[-limit:]:
                out.append({k: c[k] for k in ("open_time_utc", "open", "high", "low", "close", "volume", "trades", "closed") if k in c})
            return out
        return {
            "purpose": "Evaluate whether to recommend the fixed ENTRY order now. The setup remains eligible unless TG1 or SL has been touched since D; an earlier ENTRY crossing alone is not terminal.",
            "trigger": trigger,
            "exchange": "Binance Spot",
            "symbol": CFG["symbol"],
            "setup": {
                "X": CFG["setup_x_timestamp_utc"],
                "A": CFG["setup_a_timestamp_utc"],
                "B": CFG["setup_b_timestamp_utc"],
                "C": CFG["setup_c_timestamp_utc"],
                "D": CFG["setup_d_timestamp_utc"],
                "entry": ENTRY,
                "tg1": TG1,
                "sl": SL,
            },
            "live_price": self.price,
            "price_exchange_timestamp_utc": utc_iso(self.price_event_ms) if self.price_event_ms else None,
            "last_message_age_seconds": round(time.monotonic() - self.last_market_msg_monotonic, 2),
            "distance_to_entry_pct": round(distance_pct * 100, 4) if distance_pct is not None else None,
            "price_change_last_60s_pct": round(delta_pct * 100, 4),
            "upward_entry_cross_seen_by_this_process": self.state.get("entry_crossing_time_utc") is not None,
            "entry_touched_since_D": any(b["high"] >= ENTRY or b["open"] >= ENTRY for b in self.history_minutes),
            "tg1_touched_since_D": any(b["high"] >= TG1 or b["open"] >= TG1 for b in self.history_minutes),
            "sl_touched_since_D": any(b["low"] <= SL or b["open"] <= SL for b in self.history_minutes),
            "currently_in_entry_zone": SL < (self.price or 0) < ENTRY,
            "order_already_armed": bool(self.state.get("entry_order_armed")),
            "candles_1m": compact(self.k1m, 120),
            "candles_5m": compact(self.k5, 288),
            "candles_1h": compact(self.k1h, 720),
            "recent_live_trades": list(self.recent_trades)[-20:],
            "fixed_levels": {"ENTRY": ENTRY, "TG1": TG1, "SL": SL},
            "constraints": [
                "Never alter fixed levels.",
                "No risk/reward analysis.",
                "A historical ENTRY touch is not terminal; if current price is below ENTRY and TG1/SL were not touched, evaluate the next upward-crossing opportunity.",
                "No automatic order placement.",
            ],
        }

    async def evaluate(self, trigger: str) -> None:
        if self.state.get("decision_state") in ("TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED"):
            return
        if self.state.get("entry_order_armed"):
            return
        if self.ai_running:
            self.pending_reason = trigger
            return
        if self.price is None or self.price_event_ms is None:
            return
        age = max(0, (int(time.time() * 1000) - self.price_event_ms) / 1000)
        if age > float(CFG["max_market_data_age_seconds"]):
            self.state["decision_state"] = "DATA_INVALID"
            save_state(self.state)
            notify("DATA_INVALID — zastareli market podaci", f"Poslednji Binance timestamp je star {age:.1f}s. Ne izdaje se ENTRY signal.")
            return
        # A price above ENTRY but below TG1 is not a missed setup. Do not arm an upward stop-limit there;
        # wait for price to return below ENTRY and reassess the next upward attempt.
        if self.price >= ENTRY and self.price < TG1 and not self.state.get("entry_order_armed"):
            self.state["decision_state"] = "WAIT_FOR_ENTRY_ZONE"
            save_state(self.state)
            return
        if self.price >= TG1 and not self.state.get("entry_order_armed"):
            self.state["decision_state"] = "TARGET_REACHED_BEFORE_ENTRY"
            self.state["setup_terminal_reason"] = f"Cena {self.price:.2f} je dostigla TG1 {TG1:.2f} pre nove entry odluke."
            save_state(self.state)
            notify("SETUP ZAVRŠEN — TG1 dostignut", self.state["setup_terminal_reason"])
            return
        if self.price <= SL and not self.state.get("entry_order_armed"):
            self.state["decision_state"] = "SETUP_INVALIDATED"
            self.state["setup_terminal_reason"] = f"Cena {self.price:.2f} je dostigla SL {SL:.2f}."
            save_state(self.state)
            notify("SETUP PONIŠTEN — SL dostignut", self.state["setup_terminal_reason"])
            return
        self.ai_running = True
        self.pending_reason = None
        try:
            payload = self.snapshot(trigger)
            snapshot_path = RUNTIME / "ai_snapshot.json"
            snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            prompt = (
                "Analyze the attached live snapshot file ai_snapshot.json and return the exact JSON schema required by your system instructions. "
                "Do not place an order. The attached JSON is the current monitor snapshot and is the only market data you may use."
            )
            opencode = shutil.which(os.getenv("OPENCODE_BIN", "opencode"))
            if not opencode:
                raise RuntimeError("OpenCode CLI nije pronađen u PATH-u. Instaliraj/prijavi OpenCode pre pokretanja monitora.")
            proc = await asyncio.create_subprocess_exec(
                opencode, "run", prompt, "--agent", "entry-supervisor", "--format", "json", "--dir", str(ROOT),
                "--file", str(snapshot_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
            # Validate the CURRENT market state after every model response, not only after a PLACE signal.
            # Otherwise an AI response based on an older snapshot could overwrite a live SL/TG1 event.
            live_terminal = self.state.get("decision_state")
            if live_terminal == "TARGET_REACHED_CHECK_ORDER":
                result["decision"] = "TARGET_REACHED_CHECK_ORDER"
                result["order"] = None
                result["reason"] = "TG1 je dostignut dok je AI analizirao, ali monitor nema potvrdu statusa naloga. Proveri stvarni status naloga/pozicije na Binance-u."
            elif live_terminal == "TARGET_REACHED_BEFORE_ENTRY":
                result["decision"] = "TARGET_REACHED_BEFORE_ENTRY"
                result["order"] = None
                result["reason"] = self.state.get("setup_terminal_reason") or "TG1 je već dostignut; trenutni setup je završen."
            elif live_terminal == "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS":
                result["decision"] = "DATA_INVALID"
                result["order"] = None
                result["reason"] = self.state.get("setup_terminal_reason") or "Istorijski redosled TG1/SL nije moguće odrediti iz 1m OHLC podataka."
            elif live_terminal == "SETUP_INVALIDATED":
                result["decision"] = "SETUP_INVALIDATED"
                result["order"] = None
                result["reason"] = self.state.get("setup_terminal_reason") or "SL je dostignut; trenutni setup je poništen."
            elif self.price is None:
                result["decision"] = "DATA_INVALID"
                result["order"] = None
                result["reason"] = "Aktuelna cena nije dostupna nakon AI analize."
            elif self.price >= TG1:
                result["decision"] = "TARGET_REACHED_BEFORE_ENTRY"
                result["order"] = None
                result["reason"] = f"Dok je AI rezonovao, cena je dostigla TG1 ({self.price:.2f}); nema nove entry odluke za ovaj setup. " + str(result.get("reason", ""))
            elif self.price <= SL:
                result["decision"] = "SETUP_INVALIDATED"
                result["order"] = None
                result["reason"] = f"Dok je AI rezonovao, cena je dotakla SL ({self.price:.2f}); setup je poništen. " + str(result.get("reason", ""))
            elif (time.monotonic() - self.last_market_msg_monotonic) > float(CFG["max_market_data_age_seconds"]):
                result["decision"] = "DATA_INVALID"
                result["order"] = None
                result["reason"] = "Market stream je zastareo dok je AI rezonovao; ne izdaje se ENTRY signal."
            elif self.price >= ENTRY and not self.state.get("entry_order_armed") and result.get("decision") not in ("SETUP_INVALIDATED", "DATA_INVALID", "TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS"):
                result["decision"] = "WAIT_FOR_ENTRY_ZONE"
                result["order"] = None
                result["reason"] = f"Dok je AI rezonovao, cena je iznad ENTRY na {self.price:.2f}, ali TG1 nije dostignut. Čekati povratak ispod ENTRY pa ponovo proceniti; prethodni ENTRY dodir nije terminalan. " + str(result.get("reason", ""))
            elif self.price < ENTRY and result.get("decision") == "WAIT_FOR_ENTRY_ZONE":
                result["decision"] = "WAIT_FOR_CONFIRMATION"
                result["order"] = None
                result["reason"] = f"Cena se vratila ispod ENTRY na {self.price:.2f} dok je AI rezonovao. Ponovo proceniti aktivnu zonu na sledećem svežem snapshotu; ENTRY crossing nije terminalan. " + str(result.get("reason", ""))
            elif result.get("decision") == "TARGET_REACHED_BEFORE_ENTRY" and not any(b["high"] >= TG1 or b["open"] >= TG1 for b in self.history_minutes):
                # An AI claim that TG1 was touched must be supported by the monitor's actual candles/live events.
                result["decision"] = "WAIT_FOR_CONFIRMATION"
                result["order"] = None
                result["reason"] = "AI je naveo da je TG1 dostignut, ali nijedna dostavljena 1m sveća to ne potvrđuje. Ne gasim setup na osnovu nepotkrepljene tvrdnje; nastaviti sa svežim podacima."
            prev = self.state.get("decision_state")
            self.state["decision_state"] = result.get("decision", "DATA_INVALID")
            self.state["last_decision"] = result
            self.state["last_decision_time_utc"] = utc_iso()
            self.state["last_ai_eval_monotonic"] = time.monotonic()
            if result.get("decision") in ("TARGET_REACHED_BEFORE_ENTRY", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED"):
                self.state["setup_terminal_reason"] = result.get("reason", "")
            save_state(self.state)
            log_event({"type": "ai_decision", "trigger": trigger, "previous_state": prev, "decision": result, "live_price_at_log": self.price})
            if result.get("decision") == "PLACE_ENTRY_ORDER_NOW" and prev != "PLACE_ENTRY_ORDER_NOW":
                self.state["signal_sent"] = True
                save_state(self.state)
                notify("ACTION: POSTAVI USLOVNI ENTRY NALOG SADA", json.dumps(result, ensure_ascii=False, indent=2))
            elif result.get("decision") in ("TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED", "DATA_INVALID") and result.get("decision") != prev:
                notify(f"STANJE PROMENJENO: {result.get('decision')}", str(result.get("reason", "")))
            else:
                print(f"[{utc_iso()}] AI={result.get('decision')} price={self.price:.2f} trigger={trigger} — {result.get('reason', '')}", flush=True)
        except Exception as e:
            self.state["decision_state"] = "DATA_INVALID" if "zastareo" in str(e).lower() or "fresh" in str(e).lower() else self.state.get("decision_state", "WAIT_FOR_CONFIRMATION")
            save_state(self.state)
            log_event({"type": "ai_error", "trigger": trigger, "error": repr(e)})
            notify("AI PROCENA NIJE USPela — nema novog ENTRY signala", f"Okidač: {trigger}\nGreška: {type(e).__name__}: {e}\nMonitor nastavlja da prati podatke; nije poslat nalog.")
        finally:
            self.ai_running = False
            pending = self.pending_reason
            self.pending_reason = None
            if pending:
                # Coalesce multiple market events into one fresh evaluation.
                asyncio.create_task(self.evaluate("coalesced:" + pending))

    def on_trade(self, data: dict[str, Any]) -> None:
        p = float(data["p"])
        event_ms = int(data.get("T", data.get("E", int(time.time() * 1000))))
        previous_price = self.price
        self.price = p
        self.price_event_ms = event_ms
        self.last_market_msg_monotonic = time.monotonic()
        now = time.time()
        self.last_price_samples.append((now, p))
        self.recent_trades.append({"time_utc": utc_iso(event_ms), "price": p, "qty": float(data.get("q", 0)), "buyer_is_maker": bool(data.get("m", False))})

        terminal_states = ("TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED")
        terminal = self.state.get("decision_state") in terminal_states
        if terminal:
            return

        # Manual order acknowledgement is informational only; public market data cannot prove fill status.
        armed_flag = (ROOT / "runtime" / "entry_order_armed.flag").exists()
        if armed_flag and self.state.get("signal_sent"):
            self.state["entry_order_armed"] = True
            if self.state.get("decision_state") != "ENTRY_ORDER_ARMED":
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
                save_state(self.state)

        if p >= TG1:
            self.state["decision_state"] = "TARGET_REACHED_BEFORE_ENTRY" if not self.state.get("entry_order_armed") else "TARGET_REACHED_CHECK_ORDER"
            self.state["setup_terminal_reason"] = f"Tržište je dostiglo TG1 {TG1:.2f} na {p:.2f}. Proveri stvarni status naloga/pozicije na Binance-u."
            save_state(self.state)
            notify("TG1 DOSTIGNUT — proveri status naloga", self.state["setup_terminal_reason"])
            return
        if p <= SL:
            self.state["decision_state"] = "SETUP_INVALIDATED"
            self.state["setup_terminal_reason"] = f"Tržište je dostiglo SL {SL:.2f} na {p:.2f}. Proveri stvarni status naloga/pozicije na Binance-u."
            if self.state.get("entry_order_armed"):
                self.state["setup_terminal_reason"] += " Ako je BUY entry nalog još otvoren i neizvršen, ručno ga otkaži; monitor ne može da upravlja nalozima."
            save_state(self.state)
            notify("SL DOSTIGNUT — setup poništen", self.state["setup_terminal_reason"])
            return

        if self.state.get("entry_order_armed"):
            if p >= ENTRY and not self.state.get("entry_crossing_time_utc"):
                self.state["entry_crossing_time_utc"] = utc_iso(event_ms)
                self.state["decision_state"] = "ENTRY_ORDER_ARMED"
                save_state(self.state)
                notify("ENTRY nivo dostignut — proveri izvršenje naloga", f"Poslednja cena={p:.2f}. STOP_LOSS_LIMIT nalog je mogao da se aktivira, ali izvršenje po 82147 nije garantovano. Proveri stvarni status naloga na Binance-u.")
            return

        if p >= ENTRY:
            crossed_now = previous_price is not None and previous_price < ENTRY
            if crossed_now and not self.state.get("entry_crossing_time_utc"):
                self.state["entry_crossing_time_utc"] = utc_iso(event_ms)
            if self.state.get("decision_state") != "WAIT_FOR_ENTRY_ZONE":
                self.state["decision_state"] = "WAIT_FOR_ENTRY_ZONE"
                save_state(self.state)
                if crossed_now:
                    notify("CENA IZNAD ENTRY — SETUP JOŠ NIJE ZAVRŠEN", f"Cena={p:.2f}; ENTRY={ENTRY:.2f}; TG1 nije dostignut. Ne zaključujem ENTRY_MISSED. Čekam da se cena vrati ispod ENTRY radi nove procene.")
            return

        # Price has returned below ENTRY before either terminal level. This is still a live opportunity.
        returned_to_zone = previous_price is not None and previous_price >= ENTRY and SL < p < ENTRY
        if returned_to_zone:
            self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
            self.state["setup_terminal_reason"] = None
            if not self.state.get("entry_order_armed"):
                # The previous recommendation did not get acknowledged as an armed order; a new evaluation is allowed.
                self.state["signal_sent"] = False
            save_state(self.state)
            asyncio.create_task(self.evaluate("price_returned_below_entry_zone"))
            return

        if self.state.get("decision_state") in ("DATA_INVALID", "WAIT_FOR_ENTRY_ZONE"):
            self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
            save_state(self.state)

        dist = (ENTRY - p) / ENTRY
        fast = False
        samples = [x for x in self.last_price_samples if now - x[0] <= 60]
        if samples and samples[0][1] > 0:
            fast = (p / samples[0][1] - 1.0) >= float(CFG["fast_move_pct_60s"])
        near = 0 <= dist <= float(CFG["wake_distance_pct"])
        # The base cadence is each closed 1m candle. Use live-trade events for early warnings only,
        # throttled so a fast tape does not spawn an unbounded stream of LLM calls.
        trigger_cooldown = float(CFG.get("live_trigger_cooldown_seconds", 30))
        if (near or fast) and now - self.last_zone_eval >= trigger_cooldown:
            self.last_zone_eval = now
            asyncio.create_task(self.evaluate("live_price_approaching_entry" if near else "rapid_upward_move"))

    def on_kline(self, data: dict[str, Any]) -> None:
        k = data.get("k", {})
        interval = k.get("i")
        bar = {
            "open_time_ms": int(k["t"]), "open_time_utc": utc_iso(int(k["t"])),
            "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
            "close": float(k["c"]), "volume": float(k["v"]), "trades": int(k["n"]),
            "closed": bool(k["x"]), "close_time_ms": int(k["T"]),
            "quote_volume": float(k.get("q", 0)), "taker_buy_base": float(k.get("V", 0)),
            "taker_buy_quote": float(k.get("Q", 0)),
        }
        target = self.k1m if interval == "1m" else self.k5 if interval == "5m" else self.k1h if interval == "1h" else None
        if target is None:
            return
        if target and target[-1]["open_time_ms"] == bar["open_time_ms"]:
            target[-1] = bar
        elif not target or bar["open_time_ms"] > target[-1]["open_time_ms"]:
            target.append(bar)
            limit = 120 if interval == "1m" else 288 if interval == "5m" else 720
            del target[:-limit]
        if bar["closed"]:
            if interval == "1m":
                # Keep live history synced; an ENTRY touch does not terminate the setup.
                if self.history_minutes and self.history_minutes[-1]["open_time_ms"] == bar["open_time_ms"]:
                    self.history_minutes[-1] = bar
                else:
                    self.history_minutes.append(bar)
                self.history_minutes = self.history_minutes[-2000:]
                if self.state.get("decision_state") not in ("TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED"):
                    status, reason = history_status(self.history_minutes)
                    if status != "ACTIVE_WATCH":
                        self.state["decision_state"] = status
                        self.state["setup_terminal_reason"] = reason
                        save_state(self.state)
                        notify(f"SETUP STANJE: {status}", reason)
                        return
            asyncio.create_task(self.evaluate(f"closed_{interval}_candle"))

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            if STOP_PATH.exists():
                try:
                    STOP_PATH.unlink()
                except OSError:
                    pass
                save_state(self.state)
                try:
                    HEARTBEAT_PATH.unlink()
                except FileNotFoundError:
                    pass
                notify("MONITOR ZAUSTAVLJEN", "Zaustavljen iz GUI panela. Nalozi na Binance-u se ne diraju.")
                os._exit(0)
            self.write_heartbeat()
            age = time.monotonic() - self.last_market_msg_monotonic
            if age > float(CFG["max_market_data_age_seconds"]):
                if self.state.get("decision_state") != "DATA_INVALID":
                    self.state["decision_state"] = "DATA_INVALID"
                    save_state(self.state)
                    notify("DATA_INVALID — nema svežih market podataka", f"Bez Binance stream poruke {age:.1f}s. Ne izdavati nove ENTRY preporuke dok se stream ne oporavi.")
            elif self.state.get("decision_state") == "DATA_INVALID" and self.state.get("setup_terminal_reason") is None:
                self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
                save_state(self.state)
                asyncio.create_task(self.evaluate("market_stream_recovered"))

    async def run(self) -> None:
        terminal_states = ("TARGET_REACHED_BEFORE_ENTRY", "TARGET_REACHED_CHECK_ORDER", "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS", "SETUP_INVALIDATED")
        if self.state.get("decision_state") not in terminal_states:
            self.state["decision_state"] = "BOOTSTRAPPING"
            save_state(self.state)
        self.write_heartbeat()
        if not await self.bootstrap():
            return
        streams = f"{SYMBOL}@aggTrade/{SYMBOL}@kline_1m/{SYMBOL}@kline_5m/{SYMBOL}@kline_1h"
        url = WS_BASE + "?streams=" + streams
        notify("BTC ENTRY WATCH JE AKTIVAN", f"{CFG['symbol']} Spot\nENTRY={ENTRY:.2f} | TG1={TG1:.2f} | SL={SL:.2f}\nPraćenje: aggTrade + 1m/5m/1h sveće\nSamo odluke i upozorenja; ne postavlja stvarne naloge.")
        asyncio.create_task(self.watchdog())
        while True:
            try:
                print(f"[{utc_iso()}] Povezivanje na Binance WebSocket...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**20) as ws:
                    async for raw in ws:
                        self.last_market_msg_monotonic = time.monotonic()
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        event = data.get("e")
                        if event == "aggTrade":
                            self.on_trade(data)
                        elif event == "kline":
                            self.on_kline(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_event({"type": "websocket_disconnect", "error": repr(e)})
                notify("BINANCE STREAM PREKINUT", f"{type(e).__name__}: {e}\nPokušavam ponovno povezivanje; za vreme prekida ne veruj starim signalima.")
                await asyncio.sleep(2)
                # Refresh price and backfill 1m bars after a disconnect; an outcome level may have been hit during the gap.
                try:
                    server = get_json("/api/v3/time")
                    now_ms = int(server["serverTime"])
                    ticker = get_json("/api/v3/ticker/price", {"symbol": CFG["symbol"]})
                    self.price = float(ticker["price"])
                    self.price_event_ms = now_ms
                    self.load_pattern_context(now_ms)
                    if self.history_minutes:
                        backfill_start = max(SETUP_D_MS, self.history_minutes[-1]["open_time_ms"])
                    else:
                        backfill_start = SETUP_D_MS
                    recovered = fetch_klines_since("1m", backfill_start, now_ms)
                    merged = {b["open_time_ms"]: b for b in self.history_minutes}
                    merged.update({b["open_time_ms"]: b for b in recovered})
                    self.history_minutes = [merged[k] for k in sorted(merged)][-2000:]
                    self.k1m = self.history_minutes[-120:]
                    status, reason = history_status(self.history_minutes)
                    if status != "ACTIVE_WATCH":
                        self.state["decision_state"] = status
                        self.state["setup_terminal_reason"] = reason
                        save_state(self.state)
                        notify(f"SETUP STANJE POSLE PREKIDA: {status}", reason)
                    elif SL < self.price < ENTRY and not self.state.get("entry_order_armed"):
                        self.state["decision_state"] = "WAIT_FOR_CONFIRMATION"
                        save_state(self.state)
                        asyncio.create_task(self.evaluate("after_stream_recovery_in_entry_zone"))
                    # The REST refresh does not prove that the WebSocket stream has recovered.
                except Exception as refresh_err:
                    log_event({"type": "rest_refresh_error", "error": repr(refresh_err)})


async def main() -> None:
    global _instance_lock, _owns_instance
    _instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_lock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        _instance_lock.listen(1)
    except OSError:
        print("Monitor je već pokrenut (drugi proces drži instancu). Zaustavi ga sa Ctrl+C pre pokretanja novog.", flush=True)
        return
    _owns_instance = True
    if STOP_PATH.exists():
        STOP_PATH.unlink()
    print("BTC Entry Watch — decision support only, no auto-trading.")
    print(f"OpenCode: {shutil.which(os.getenv('OPENCODE_BIN', 'opencode')) or 'NOT FOUND'}")
    print(f"Configuration: {CFG}")
    monitor = EntryMonitor()
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
