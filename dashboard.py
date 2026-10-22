#!/usr/bin/env python3
"""Lokalni GUI panel za BTC Entry Watch. Standardna biblioteka; bez dodatnih paketa."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
HTML_PATH = ROOT / "dashboard.html"
CFG_PATH = ROOT / "config.json"
STATE_PATH = RUNTIME / "state.json"
EVENTS_PATH = RUNTIME / "events.jsonl"
SNAPSHOT_PATH = RUNTIME / "ai_snapshot.json"
HEARTBEAT_PATH = RUNTIME / "heartbeat.json"
STOP_PATH = RUNTIME / "stop.flag"
ARMED_PATH = RUNTIME / "entry_order_armed.flag"
LOG_PATH = RUNTIME / "monitor.log"

HOST = "127.0.0.1"
PORT = int(os.getenv("BTCW_PORT", "8765"))
STALE_SECONDS = 20.0
GUARD_HEADER = "X-BTCW"

_monitor_proc: subprocess.Popen | None = None
_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def tail_lines(path: Path, limit: int, max_bytes: int = 256_000) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-limit:]
    except Exception:
        return []


def tail_events(limit: int = 30) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in tail_lines(EVENTS_PATH, limit):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[::-1]


def append_event(event: dict[str, Any]) -> None:
    try:
        RUNTIME.mkdir(exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"logged_at_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), **event}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def monitor_status() -> dict[str, Any]:
    hb = read_json(HEARTBEAT_PATH)
    age = None
    if hb.get("epoch") is not None:
        try:
            age = max(0.0, time.time() - float(hb["epoch"]))
        except (TypeError, ValueError):
            age = None
    proc_alive = _monitor_proc is not None and _monitor_proc.poll() is None
    alive = bool(age is not None and age <= STALE_SECONDS)
    if not alive and proc_alive:
        alive = True
    if alive and age is not None and age > 3600:
        alive = False
    return {
        "alive": alive,
        "age_seconds": round(age, 1) if age is not None else None,
        "pid": hb.get("pid"),
        "utc": hb.get("utc"),
        "price": hb.get("price"),
        "price_event_ms": hb.get("price_event_ms"),
        "ai_running": bool(hb.get("ai_running")),
    }


def build_summary() -> dict[str, Any]:
    state = read_json(STATE_PATH)
    snapshot = read_json(SNAPSHOT_PATH)
    cfg = read_json(CFG_PATH)
    levels = snapshot.get("fixed_levels") or {
        "ENTRY": cfg.get("entry"),
        "TG1": cfg.get("tg1"),
        "SL": cfg.get("sl"),
    }
    setup = snapshot.get("setup") or {
        "X": cfg.get("setup_x_timestamp_utc"),
        "A": cfg.get("setup_a_timestamp_utc"),
        "B": cfg.get("setup_b_timestamp_utc"),
        "C": cfg.get("setup_c_timestamp_utc"),
        "D": cfg.get("setup_d_timestamp_utc"),
    }
    decision = state.get("last_decision")
    return {
        "server_time_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "monitor": monitor_status(),
        "state": {
            "decision_state": state.get("decision_state"),
            "decision": decision if isinstance(decision, dict) else None,
            "decision_time_utc": state.get("last_decision_time_utc"),
            "signal_sent": bool(state.get("signal_sent")),
            "entry_order_armed": bool(state.get("entry_order_armed")),
            "setup_terminal_reason": state.get("setup_terminal_reason"),
        },
        "snapshot": {
            "live_price": snapshot.get("live_price"),
            "price_exchange_timestamp_utc": snapshot.get("price_exchange_timestamp_utc"),
            "last_message_age_seconds": snapshot.get("last_message_age_seconds"),
            "distance_to_entry_pct": snapshot.get("distance_to_entry_pct"),
            "price_change_last_60s_pct": snapshot.get("price_change_last_60s_pct"),
            "entry_touched_since_D": snapshot.get("entry_touched_since_D"),
            "tg1_touched_since_D": snapshot.get("tg1_touched_since_D"),
            "sl_touched_since_D": snapshot.get("sl_touched_since_D"),
            "currently_in_entry_zone": snapshot.get("currently_in_entry_zone"),
        },
        "levels": levels,
        "setup": setup,
        "order_armed_flag": ARMED_PATH.exists(),
        "events": tail_events(30),
        "log": tail_lines(LOG_PATH, 40),
    }


def find_monitor_processes() -> list[int]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*btc-entry-watch*run_monitor.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return [int(token) for token in out.stdout.split() if token.strip().isdigit()]


def start_monitor() -> tuple[bool, str]:
    global _monitor_proc
    with _lock:
        status = monitor_status()
        if status["alive"]:
            return False, "Monitor je već pokrenut."
        legacy = find_monitor_processes()
        if legacy:
            pids = ", ".join(str(pid) for pid in legacy)
            return False, f"Pronađen je stari monitor (PID {pids}) koji ne šalje heartbeat. Zaustavi ga sa Ctrl+C u terminalu, pa pokušaj ponovo."
        RUNTIME.mkdir(exist_ok=True)
        try:
            STOP_PATH.unlink()
        except FileNotFoundError:
            pass
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            with LOG_PATH.open("ab") as log:
                _monitor_proc = subprocess.Popen(
                    [str(python), "-u", str(ROOT / "run_monitor.py")],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
        except Exception as e:
            return False, f"Monitor nije pokrenut: {type(e).__name__}: {e}"
        append_event({"type": "gui_start", "title": "Monitor pokrenut iz GUI panela", "pid": _monitor_proc.pid})
        return True, "Monitor je pokrenut. Prva AI procena stiže za 10–60 sekundi."


def stop_monitor() -> tuple[bool, str]:
    status = monitor_status()
    if not status["alive"]:
        return False, "Monitor nije pokrenut."
    RUNTIME.mkdir(exist_ok=True)
    STOP_PATH.write_text("stop", encoding="utf-8")
    append_event({"type": "gui_stop", "title": "Zaustavljanje zatraženo iz GUI panela"})
    return True, "Zaustavljanje je zatraženo. Monitor izlazi u roku od 5 sekundi."


def arm_order() -> tuple[bool, str]:
    state = read_json(STATE_PATH)
    decision = state.get("last_decision") if isinstance(state.get("last_decision"), dict) else {}
    if decision.get("decision") != "PLACE_ENTRY_ORDER_NOW" or not state.get("signal_sent"):
        return False, "Potvrda nije dostupna: AI trenutno ne traži postavljanje naloga."
    if state.get("entry_order_armed") or ARMED_PATH.exists():
        return True, "Nalog je već označen kao postavljen."
    ARMED_PATH.write_text("armed", encoding="utf-8")
    append_event({"type": "gui_armed", "title": "Korisnik potvrdio da je ENTRY nalog postavljen na Binance-u"})
    return True, "Potvrda je zabeležena. Monitor će nastaviti da prati ENTRY, TG1 i SL."


class Handler(BaseHTTPRequestHandler):
    server_version = "BTCEntryWatchGUI/1.0"

    def _send(self, code: int, body: str | bytes, content_type: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            try:
                html = HTML_PATH.read_text(encoding="utf-8")
            except Exception as e:
                self._send(500, f"dashboard.html nije pronađen: {e}", "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path.startswith("/api/summary"):
            self._json(200, build_summary())
        elif self.path.startswith("/api/snapshot"):
            self._json(200, read_json(SNAPSHOT_PATH))
        else:
            self._json(404, {"ok": False, "message": "Nepoznata ruta."})

    def do_OPTIONS(self) -> None:
        self._send(204, b"")

    def do_POST(self) -> None:
        if self.headers.get(GUARD_HEADER) != "1":
            self._json(403, {"ok": False, "message": "Odbijeno."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/api/start":
            ok, message = start_monitor()
        elif self.path == "/api/stop":
            ok, message = stop_monitor()
        elif self.path == "/api/armed":
            ok, message = arm_order()
        elif self.path == "/api/shutdown":
            self._json(200, {"ok": True, "message": "Panel se zatvara. Monitor nastavlja da radi."})
            if _server is not None:
                threading.Thread(target=_server.shutdown, daemon=True).start()
            return
        else:
            self._json(404, {"ok": False, "message": "Nepoznata ruta."})
            return
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    global _server
    RUNTIME.mkdir(exist_ok=True)
    url = f"http://{HOST}:{PORT}/"
    try:
        _server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Panel je već pokrenut ili je port zauzet: {url}")
        if os.getenv("BTCW_NO_BROWSER") != "1":
            webbrowser.open(url)
        return
    print(f"BTC Entry Watch GUI: {url}")
    if os.getenv("BTCW_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
