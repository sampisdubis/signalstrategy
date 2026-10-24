"""Atomic persistence and setup isolation for Signal Strategy."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from setup_config import (
    Setup,
    SetupValidationError,
    default_operational_config,
    setup_from_config,
    utc_iso,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RUNTIME = ROOT / "runtime"
STATE_FILENAME = "state.json"
SNAPSHOT_FILENAME = "ai_snapshot.json"
ARMED_FILENAME = "entry_order_armed.flag"
EVENTS_FILENAME = "events.jsonl"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value is not None else default
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_raw_config(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    raw = read_json(path, {})
    if not isinstance(raw, dict) or not raw:
        raise SetupValidationError("config.json ne postoji ili nije ispravan JSON.")
    return raw


def load_setup(path: Path | None = None) -> Setup:
    return setup_from_config(load_raw_config(path))


def _operational_from(old_config: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    operational = default_operational_config()
    for key in list(operational):
        if key in old_config:
            operational[key] = old_config[key]
    supplied = (payload or {}).get("operational")
    if isinstance(supplied, dict):
        for key in list(operational):
            if key in supplied:
                operational[key] = supplied[key]
    # Safety invariant: this monitor only advises, it never trades.
    operational["auto_trade"] = False
    return operational


def save_setup_payload(
    payload: dict[str, Any],
    *,
    config_path: Path | None = None,
    runtime: Path | None = None,
) -> tuple[Setup, dict[str, Any]]:
    """Validate, atomically persist, isolate old state and initialize a new one."""
    from setup_config import normalize_setup_payload

    config_path = config_path or CONFIG_PATH
    runtime = runtime or RUNTIME
    old_config: dict[str, Any] = {}
    try:
        old_config = load_raw_config(config_path)
    except SetupValidationError:
        old_config = {}
    old_setup_id = str(old_config.get("setup_id") or "")
    setup = normalize_setup_payload(payload)
    operational = _operational_from(old_config, payload)
    config = setup.to_config(operational)
    atomic_write_json(config_path, config)

    if old_setup_id and old_setup_id != setup.setup_id:
        archive_setup_artifacts(old_setup_id, runtime=runtime)
    elif old_setup_id:
        armed_flag_path(runtime).unlink(missing_ok=True)
        snapshot_path(runtime).unlink(missing_ok=True)
    initialize_state_for_setup(setup, runtime=runtime, append_event_first=False)
    append_event(
        {
            "type": "setup_confirmed",
            "setup_id": setup.setup_id,
            "symbol": setup.symbol,
            "exchange": setup.exchange,
            "market_type": setup.market_type,
            "direction": setup.direction,
            "main_interval": setup.main_interval,
            "timezone_offset_minutes": setup.timezone_offset_minutes,
            "levels": {"ENTRY": setup.entry, "TG1": setup.tg1, "SL": setup.sl},
            "times_utc": {key.upper(): utc_iso(ms) for key, ms in setup.timestamps_ms.items()},
            "local_times": setup.local_times(),
            "message": "Korisnik je potvrdio setup; monitor još nije pokrenut.",
        },
        runtime=runtime,
    )
    if old_setup_id and old_setup_id != setup.setup_id:
        append_event(
            {
                "type": "setup_replaced",
                "setup_id": setup.setup_id,
                "previous_setup_id": old_setup_id,
                "message": "Prethodni setup je arhiviran; dnevnik događaja starih setupova je sačuvan.",
            },
            runtime=runtime,
        )
    return setup, config


def initial_state(setup: Setup) -> dict[str, Any]:
    return {
        "setup_id": setup.setup_id,
        "setup_fingerprint": setup.fingerprint(),
        "setup_d_timestamp_utc": utc_iso(setup.d_ms),
        "decision_state": "BOOTSTRAPPING",
        "signal_sent": False,
        "entry_order_armed": False,
        "last_decision": None,
        "last_ai_eval_monotonic": 0,
        "entry_crossing_time_utc": None,
        "setup_terminal_reason": None,
        "config_changed_while_running": False,
        "created_at_utc": utc_iso(),
    }


def state_path(runtime: Path | None = None) -> Path:
    return (runtime or RUNTIME) / STATE_FILENAME


def snapshot_path(runtime: Path | None = None) -> Path:
    return (runtime or RUNTIME) / SNAPSHOT_FILENAME


def armed_flag_path(runtime: Path | None = None) -> Path:
    return (runtime or RUNTIME) / ARMED_FILENAME


def events_path(runtime: Path | None = None) -> Path:
    return (runtime or RUNTIME) / EVENTS_FILENAME


def archive_dir(runtime: Path, setup_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = runtime / "archive" / f"{setup_id}-{stamp}"
    candidate = base
    index = 1
    while candidate.exists():
        candidate = base.with_name(base.name + f"-{index}")
        index += 1
    return candidate


def archive_setup_artifacts(setup_id: str, *, runtime: Path | None = None) -> Path | None:
    runtime = runtime or RUNTIME
    if not setup_id:
        return None
    runtime.mkdir(parents=True, exist_ok=True)
    source_state = state_path(runtime)
    entries = {
        "state.json": source_state,
        "ai_snapshot.json": snapshot_path(runtime),
        "entry_order_armed.flag": armed_flag_path(runtime),
    }
    if not any(path.exists() for path in entries.values()):
        return None
    target = archive_dir(runtime, setup_id)
    target.mkdir(parents=True, exist_ok=True)
    for name, path in entries.items():
        if path.exists():
            try:
                shutil.move(str(path), str(target / name))
            except OSError:
                pass
    return target


def initialize_state_for_setup(
    setup: Setup,
    *,
    runtime: Path | None = None,
    append_event_first: bool = True,
) -> dict[str, Any]:
    runtime = runtime or RUNTIME
    runtime.mkdir(parents=True, exist_ok=True)
    existing = read_json(state_path(runtime), {})
    if isinstance(existing, dict) and existing.get("setup_id") and existing.get("setup_id") != setup.setup_id:
        archive_setup_artifacts(str(existing["setup_id"]), runtime=runtime)
    state = initial_state(setup)
    atomic_write_json(state_path(runtime), state)
    if append_event_first:
        append_event(
            {
                "type": "state_initialized",
                "setup_id": setup.setup_id,
                "message": "Inicijalizovano stanje za potvrđeni setup.",
            },
            runtime=runtime,
        )
    return state


def load_state_for_setup(setup: Setup, *, runtime: Path | None = None) -> dict[str, Any]:
    runtime = runtime or RUNTIME
    state = read_json(state_path(runtime), {})
    if isinstance(state, dict) and state.get("setup_id") == setup.setup_id:
        state.setdefault("setup_fingerprint", setup.fingerprint())
        return state
    if isinstance(state, dict) and state:
        old_id = str(state.get("setup_id") or "legacy")
        archive_setup_artifacts(old_id, runtime=runtime)
    return initialize_state_for_setup(setup, runtime=runtime, append_event_first=True)


def save_state(state: dict[str, Any], *, runtime: Path | None = None) -> None:
    atomic_write_json(state_path(runtime or RUNTIME), state)


def read_state_raw(*, runtime: Path | None = None) -> dict[str, Any]:
    state = read_json(state_path(runtime), {})
    return state if isinstance(state, dict) else {}


def append_event(event: dict[str, Any], *, runtime: Path | None = None) -> None:
    runtime = runtime or RUNTIME
    runtime.mkdir(parents=True, exist_ok=True)
    item = {"logged_at_utc": utc_iso(), **event}
    with events_path(runtime).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_armed_flag(setup_id: str, *, runtime: Path | None = None) -> None:
    runtime = runtime or RUNTIME
    runtime.mkdir(parents=True, exist_ok=True)
    armed_flag_path(runtime).write_text(setup_id, encoding="utf-8")


def read_armed_flag(*, runtime: Path | None = None) -> tuple[bool, str]:
    path = armed_flag_path(runtime)
    try:
        return True, path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False, ""
    except OSError:
        return False, ""


def config_file_changed(loaded_setup: Setup, config_path: Path | None = None) -> bool:
    try:
        current = load_setup(config_path)
    except Exception:
        return True
    return current.setup_id != loaded_setup.setup_id or current.fingerprint() != loaded_setup.fingerprint()
