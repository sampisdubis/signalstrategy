#!/usr/bin/env python3
"""Lokalni GUI panel za Signal Strategy. Standardna biblioteka; bez dodatnih paketa."""
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

from setup_config import (
    Setup,
    SetupValidationError,
    default_operational_config,
    setup_from_config,
)
from setup_store import (
    CONFIG_PATH,
    RUNTIME,
    append_event,
    load_raw_config,
    load_setup,
    read_armed_flag,
    read_json,
    read_state_raw,
    save_setup_payload,
    write_armed_flag,
)

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "dashboard.html"
STATE_PATH = RUNTIME / "state.json"
SNAPSHOT_PATH = RUNTIME / "ai_snapshot.json"
HEARTBEAT_PATH = RUNTIME / "heartbeat.json"
STOP_PATH = RUNTIME / "stop.flag"
ARMED_PATH = RUNTIME / "entry_order_armed.flag"
EVENTS_PATH = RUNTIME / "events.jsonl"
LOG_PATH = RUNTIME / "monitor.log"

HOST = "127.0.0.1"
PORT = int(os.getenv("BTCW_PORT", "8765"))
STALE_SECONDS = 20.0
GUARD_HEADER = "X-BTCW"

_monitor_proc: subprocess.Popen | None = None
_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


def tail_lines(path: Path, limit: int, max_bytes: int = 256_000) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", "replace")
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


def current_setup() -> Setup | None:
    try:
        return load_setup()
    except SetupValidationError:
        return None


def append_gui_event(event: dict[str, Any]) -> None:
    setup = current_setup()
    payload = {"setup_id": setup.setup_id if setup else None, **event}
    try:
        append_event(payload, runtime=RUNTIME)
    except Exception:
        pass


def monitor_status() -> dict[str, Any]:
    heartbeat = read_json(HEARTBEAT_PATH, {})
    age = None
    if heartbeat.get("epoch") is not None:
        try:
            age = max(0.0, time.time() - float(heartbeat["epoch"]))
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
        "pid": heartbeat.get("pid"),
        "utc": heartbeat.get("utc"),
        "price": heartbeat.get("price"),
        "price_event_ms": heartbeat.get("price_event_ms"),
        "ai_running": bool(heartbeat.get("ai_running")),
        "setup_id": heartbeat.get("setup_id"),
        "config_changed": bool(heartbeat.get("config_changed")),
    }


def any_monitor_process() -> bool:
    if monitor_status()["alive"]:
        return True
    return bool(find_monitor_processes())


def build_summary() -> dict[str, Any]:
    state = read_state_raw(runtime=RUNTIME)
    snapshot = read_json(SNAPSHOT_PATH, {})
    config: dict[str, Any] = {}
    setup: Setup | None = None
    config_error: str | None = None
    try:
        config = load_raw_config()
        setup = setup_from_config(config)
    except SetupValidationError as exc:
        config_error = " ".join(exc.errors)
    except Exception as exc:
        config_error = f"{type(exc).__name__}: {exc}"

    if setup is None:
        state = {}
        snapshot = {}
    if setup and state.get("setup_id") != setup.setup_id:
        state = {}
    if setup and snapshot.get("setup_id") != setup.setup_id:
        snapshot = {}
    levels = (
        {"ENTRY": setup.entry, "TG1": setup.tg1, "SL": setup.sl}
        if setup
        else {"ENTRY": None, "TG1": None, "SL": None}
    )
    decision = state.get("last_decision")
    decision = decision if isinstance(decision, dict) else None
    status = monitor_status()
    setup_public = setup.public() if setup else None
    if setup_public:
        setup_public["order"] = setup.order_details(
            order_type=str(config.get("order_type_for_upward_crossing", "STOP_LOSS_LIMIT")),
            time_in_force=str(config.get("time_in_force", "GTC")),
        )
    flag_exists, flag_content = read_armed_flag(runtime=RUNTIME)
    flag_is_current = bool(
        setup and flag_exists and flag_content in ("", "armed", setup.setup_id)
    )
    return {
        "server_time_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "config_valid": setup is not None,
        "config_error": config_error,
        "monitor": {
            **status,
            "setup_mismatch": bool(setup and status.get("setup_id") and status.get("setup_id") != setup.setup_id),
        },
        "state": {
            "setup_id": state.get("setup_id") or (setup.setup_id if setup else None),
            "decision_state": state.get("decision_state"),
            "decision": decision,
            "decision_time_utc": state.get("last_decision_time_utc"),
            "signal_sent": bool(state.get("signal_sent")),
            "entry_order_armed": bool(state.get("entry_order_armed")),
            "setup_terminal_reason": state.get("setup_terminal_reason"),
            "config_changed": bool(state.get("config_changed_while_running")),
            "snapshot_time_utc": state.get("last_ai_snapshot_utc"),
        },
        "snapshot": {
            "setup_id": snapshot.get("setup_id"),
            "data_as_of_utc": snapshot.get("data_as_of_utc"),
            "live_price": snapshot.get("live_price"),
            "price_exchange_timestamp_utc": snapshot.get("price_exchange_timestamp_utc"),
            "price_age_seconds": snapshot.get("price_age_seconds"),
            "last_message_age_seconds": snapshot.get("last_message_age_seconds"),
            "distance_to_entry_pct": snapshot.get("distance_to_entry_pct"),
            "price_change_last_60s_pct": snapshot.get("price_change_last_60s_pct"),
            "entry_touched_since_D": snapshot.get("entry_touched_since_D"),
            "tg1_touched_since_D": snapshot.get("tg1_touched_since_D"),
            "sl_touched_since_D": snapshot.get("sl_touched_since_D"),
            "currently_in_entry_zone": snapshot.get("currently_in_entry_zone"),
            "setup_status": snapshot.get("setup_status"),
            "derived_features": snapshot.get("derived_features"),
        },
        "levels": levels,
        "setup": setup_public,
        "operational": {
            "auto_trade": False,
            "max_market_data_age_seconds": config.get("max_market_data_age_seconds"),
            "wake_distance_pct": config.get("wake_distance_pct"),
            "stale_ai_price_move_pct": config.get("stale_ai_price_move_pct"),
        },
        "order_armed_flag": flag_is_current,
        "events": tail_events(30),
        "log": tail_lines(LOG_PATH, 40),
    }


def setup_editor_payload() -> dict[str, Any]:
    config = load_raw_config()
    setup = setup_from_config(config)
    return {
        "ok": True,
        "setup": setup.public(),
        "local_inputs": setup.local_inputs(),
        "timezone_offset_minutes": setup.timezone_offset_minutes,
        "timezone_label": setup.timezone_label,
        "operational": {**default_operational_config(), **config},
        "monitor_running": any_monitor_process(),
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
        output = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return [int(token) for token in output.stdout.split() if token.strip().isdigit()]


def start_monitor() -> tuple[bool, str, dict[str, Any]]:
    global _monitor_proc
    with _lock:
        status = monitor_status()
        if status["alive"]:
            return False, "Monitor je već pokrenut.", {}
        legacy = find_monitor_processes()
        if legacy:
            pids = ", ".join(str(pid) for pid in legacy)
            return False, f"Pronađen je stari monitor (PID {pids}) koji ne šalje heartbeat. Zaustavi ga, pa pokušaj ponovo.", {}
        setup = current_setup()
        if setup is None:
            return False, "Setup nije ispravan. Sačuvaj ispravan setup pre pokretanja.", {}
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
        except Exception as exc:
            return False, f"Monitor nije pokrenut: {type(exc).__name__}: {exc}", {}
        append_gui_event(
            {
                "type": "gui_start",
                "title": "Monitor pokrenut iz GUI panela",
                "pid": _monitor_proc.pid,
                "setup_id": setup.setup_id,
            }
        )
        return True, "Monitor je pokrenut. Prva AI procena stiže za 10–60 sekundi.", {"setup_id": setup.setup_id}


def stop_monitor() -> tuple[bool, str]:
    if not any_monitor_process():
        return False, "Monitor nije pokrenut."
    RUNTIME.mkdir(exist_ok=True)
    STOP_PATH.write_text("stop", encoding="utf-8")
    append_gui_event({"type": "gui_stop", "title": "Zaustavljanje zatraženo iz GUI panela"})
    return True, "Zaustavljanje je zatraženo. Monitor izlazi u roku od 5 sekundi."


def save_setup(payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    setup = current_setup()
    if any_monitor_process():
        return False, "Monitor je pokrenut. Zaustavi ga pre izmene ili zamene setup-a.", {"setup_id": setup.setup_id if setup else None}
    try:
        new_setup, config = save_setup_payload(payload)
    except SetupValidationError as exc:
        return False, "Setup nije ispravan.", {"errors": exc.errors}
    except Exception as exc:
        return False, f"Setup nije sačuvan: {type(exc).__name__}: {exc}", {}
    return (
        True,
        f"Setup {new_setup.setup_id} je sačuvan. Vremena su sačuvana u UTC, a prikazana u {new_setup.timezone_label}.",
        {
            "setup": new_setup.public(),
            "setup_id": new_setup.setup_id,
            "config": config,
        },
    )


def arm_order() -> tuple[bool, str]:
    setup = current_setup()
    if setup is None:
        return False, "Setup nije ispravan."
    state = read_state_raw(runtime=RUNTIME)
    decision = state.get("last_decision") if isinstance(state.get("last_decision"), dict) else {}
    if state.get("setup_id") != setup.setup_id:
        return False, "Stanje ne pripada aktivnom setup-u. Sačekaj da monitor učita potvrđeni setup."
    if decision.get("decision") != "PLACE_ENTRY_ORDER_NOW" or not state.get("signal_sent"):
        return False, "Potvrda nije dostupna: AI trenutno ne traži postavljanje naloga."
    flag_exists, flag_content = read_armed_flag(runtime=RUNTIME)
    if state.get("entry_order_armed") or (flag_exists and flag_content in ("", "armed", setup.setup_id)):
        return True, "Nalog je već označen kao postavljen."
    write_armed_flag(setup.setup_id, runtime=RUNTIME)
    append_gui_event(
        {
            "type": "gui_armed",
            "title": "Korisnik potvrdio da je ENTRY nalog postavljen na berzi",
            "setup_id": setup.setup_id,
        }
    )
    return True, "Potvrda je zabeležena. Monitor će nastaviti da prati ENTRY, TG1 i SL."


class Handler(BaseHTTPRequestHandler):
    server_version = "SignalStrategyGUI/2.0"

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
            except Exception as exc:
                self._send(500, f"dashboard.html nije pronađen: {exc}", "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path.startswith("/api/summary"):
            self._json(200, build_summary())
        elif self.path.startswith("/api/setup"):
            try:
                self._json(200, setup_editor_payload())
            except SetupValidationError as exc:
                self._json(400, {"ok": False, "message": str(exc), "errors": exc.errors})
            except Exception as exc:
                self._json(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
        elif self.path.startswith("/api/snapshot"):
            setup = current_setup()
            snapshot = read_json(SNAPSHOT_PATH, {})
            if setup and snapshot.get("setup_id") != setup.setup_id:
                snapshot = {}
            self._json(200, snapshot)
        else:
            self._json(404, {"ok": False, "message": "Nepoznata ruta."})

    def do_OPTIONS(self) -> None:
        self._send(204, b"")

    def do_POST(self) -> None:
        if self.headers.get(GUARD_HEADER) != "1":
            self._json(403, {"ok": False, "message": "Odbijeno."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        payload: dict[str, Any] = {}
        if self.path == "/api/setup":
            try:
                payload = json.loads(raw_body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("JSON mora biti objekat.")
            except Exception as exc:
                self._json(400, {"ok": False, "message": f"Neispravan JSON: {exc}", "errors": [str(exc)]})
                return
        if self.path == "/api/start":
            ok, message, extra = start_monitor()
        elif self.path == "/api/stop":
            ok, message = stop_monitor()
            extra = {}
        elif self.path == "/api/armed":
            ok, message = arm_order()
            extra = {}
        elif self.path == "/api/setup":
            ok, message, extra = save_setup(payload)
        elif self.path == "/api/shutdown":
            self._json(200, {"ok": True, "message": "Panel se zatvara. Monitor nastavlja da radi."})
            if _server is not None:
                threading.Thread(target=_server.shutdown, daemon=True).start()
            return
        else:
            self._json(404, {"ok": False, "message": "Nepoznata ruta."})
            return
        self._json(200 if ok else 409, {"ok": ok, "message": message, **extra})

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
    print(f"Signal Strategy GUI: {url}")
    if os.getenv("BTCW_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
