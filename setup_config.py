"""Shared, deterministic setup rules for the Signal Strategy monitor.

The confirmed setup is the single source of truth. This module owns setup
validation, timezone normalization, setup-id generation and direction-aware
level helpers. It never talks to an exchange.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

UTC = timezone.utc

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "4h": 4 * 3_600_000,
    "1d": 24 * 3_600_000,
}
SUPPORTED_PRIMARY_INTERVALS = ("15m", "30m", "1h", "4h")
SUPPORTED_EXCHANGES = ("binance",)
SUPPORTED_MARKET_TYPES = ("spot", "futures")
SUPPORTED_DIRECTIONS = ("long", "short")

TIMESTAMP_KEYS = ("x", "a", "b", "c", "d")


class SetupValidationError(ValueError):
    """Raised when setup input is incomplete or internally inconsistent."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = errors or [message]
        super().__init__(" ".join(self.errors))


def offset_label(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    total = abs(int(offset_minutes))
    hours, minutes = divmod(total, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def parse_offset_minutes(value: Any) -> int:
    """Accept 120, '120', '+02:00', '-0530', 'UTC+2' or 'UTC+02:00'."""
    if isinstance(value, bool):
        raise SetupValidationError("Vremenska zona nije ispravna.")
    if isinstance(value, (int, float)):
        minutes = int(value)
    else:
        text = str(value or "").strip().upper().replace("UTC", "").replace("GMT", "").strip()
        if not text:
            raise SetupValidationError("Vremenska zona (UTC offset) je obavezna.")
        sign = 1
        if text[0] in "+-":
            sign = -1 if text[0] == "-" else 1
            text = text[1:]
        text = text.replace(":", "")
        if "." in text:
            text = text.split(".", 1)[0]
        if len(text) == 4 and text.isdigit():
            hours, minutes = int(text[:2]), int(text[2:])
        elif len(text) <= 2 and text.isdigit():
            hours, minutes = int(text), 0
        else:
            raise SetupValidationError(f"Ne mogu da pročitam vremensku zonu: {value!r}.")
        minutes = sign * (hours * 60 + minutes)
    if not -14 * 60 <= minutes <= 14 * 60:
        raise SetupValidationError("UTC offset mora biti između -14:00 i +14:00.")
    return minutes


def parse_utc_datetime(value: Any, field_name: str = "vreme") -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric > 10_000_000_000:
            return int(numeric)
        return int(numeric * 1000)
    text = str(value or "").strip()
    if not text:
        raise SetupValidationError(f"{field_name} je obavezno.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SetupValidationError(f"{field_name} nije ispravan datum/vreme: {text}.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def utc_iso(ms: int | float | None = None) -> str:
    if ms is None:
        return datetime.now(UTC).isoformat(timespec="seconds")
    return datetime.fromtimestamp(float(ms) / 1000, UTC).isoformat(timespec="seconds")


def to_offset_iso(ms: int | float, offset_minutes: int) -> str:
    tz = timezone(timedelta(minutes=offset_minutes))
    return datetime.fromtimestamp(float(ms) / 1000, UTC).astimezone(tz).isoformat(timespec="minutes")


def to_local_input(ms: int | float, offset_minutes: int) -> str:
    tz = timezone(timedelta(minutes=offset_minutes))
    return datetime.fromtimestamp(float(ms) / 1000, UTC).astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def local_input_to_utc(value: Any, offset_minutes: int, field_name: str) -> int:
    text = str(value or "").strip()
    if not text:
        raise SetupValidationError(f"{field_name} je obavezno.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SetupValidationError(f"{field_name} nije ispravan datum/vreme: {text}.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def parse_price(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise SetupValidationError(f"{field_name} mora biti broj.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value or "").strip().replace(" ", "").replace(",", "")
        if not text:
            raise SetupValidationError(f"{field_name} je obavezan.")
        try:
            number = float(text)
        except ValueError as exc:
            raise SetupValidationError(f"{field_name} mora biti numerička vrednost.") from exc
    if not math.isfinite(number) or number <= 0:
        raise SetupValidationError(f"{field_name} mora biti pozitivan broj.")
    return number


def normalized_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or not text.isalnum():
        raise SetupValidationError("Simbol je obavezan i sme da sadrži samo slova i brojeve (npr. BTCUSDT).")
    return text


def make_setup_id(
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    direction: str,
    main_interval: str,
    timestamps_ms: dict[str, int],
    entry: float,
    tg1: float,
    sl: float,
) -> str:
    canonical = {
        "exchange": exchange,
        "market_type": market_type,
        "symbol": symbol,
        "direction": direction,
        "main_interval": main_interval,
        "x": int(timestamps_ms["x"]),
        "a": int(timestamps_ms["a"]),
        "b": int(timestamps_ms["b"]),
        "c": int(timestamps_ms["c"]),
        "d": int(timestamps_ms["d"]),
        "entry": round(float(entry), 12),
        "tg1": round(float(tg1), 12),
        "sl": round(float(sl), 12),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"{symbol.lower()}-{direction}-{digest}"


@dataclass(frozen=True)
class Setup:
    exchange: str
    market_type: str
    symbol: str
    direction: str
    main_interval: str
    timezone_offset_minutes: int
    entry: float
    tg1: float
    sl: float
    x_ms: int
    a_ms: int
    b_ms: int
    c_ms: int
    d_ms: int
    confirmed_at_utc: str | None = None

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def setup_id(self) -> str:
        return make_setup_id(
            exchange=self.exchange,
            market_type=self.market_type,
            symbol=self.symbol,
            direction=self.direction,
            main_interval=self.main_interval,
            timestamps_ms=self.timestamps_ms,
            entry=self.entry,
            tg1=self.tg1,
            sl=self.sl,
        )

    @property
    def timestamps_ms(self) -> dict[str, int]:
        return {"x": self.x_ms, "a": self.a_ms, "b": self.b_ms, "c": self.c_ms, "d": self.d_ms}

    @property
    def timezone_label(self) -> str:
        return offset_label(self.timezone_offset_minutes)

    @property
    def order_side(self) -> str:
        return "BUY" if self.is_long else "SELL"

    @property
    def trigger_description(self) -> str:
        if self.is_long:
            return "rastući prelazak kroz ENTRY odozdo"
        return "padajući prelazak kroz ENTRY odozgo"

    def local_times(self) -> dict[str, str]:
        return {
            key: to_offset_iso(value, self.timezone_offset_minutes)
            for key, value in self.timestamps_ms.items()
        }

    def local_inputs(self) -> dict[str, str]:
        return {
            key: to_local_input(value, self.timezone_offset_minutes)
            for key, value in self.timestamps_ms.items()
        }

    def entry_price(self) -> float:
        return self.entry

    def in_pre_entry_zone(self, price: float | None) -> bool:
        if price is None:
            return False
        if self.is_long:
            return self.sl < price < self.entry
        return self.entry < price < self.sl

    def past_entry_before_target(self, price: float | None) -> bool:
        if price is None:
            return False
        if self.is_long:
            return self.entry <= price < self.tg1
        return self.tg1 < price <= self.entry

    def target_hit(self, price: float | None) -> bool:
        if price is None:
            return False
        return price >= self.tg1 if self.is_long else price <= self.tg1

    def stop_hit(self, price: float | None) -> bool:
        if price is None:
            return False
        return price <= self.sl if self.is_long else price >= self.sl

    def entry_crossed(self, previous_price: float | None, price: float | None) -> bool:
        if previous_price is None or price is None:
            return False
        if self.is_long:
            return previous_price < self.entry <= price
        return previous_price > self.entry >= price

    def directional_move_pct(self, old_price: float, new_price: float) -> float:
        """Positive means moving toward the entry trigger."""
        if old_price <= 0:
            return 0.0
        raw = (new_price / old_price) - 1.0
        return raw if self.is_long else -raw

    def entry_touched(self, candle: dict[str, Any]) -> bool:
        high = float(candle["high"])
        low = float(candle["low"])
        opened = float(candle["open"])
        if self.is_long:
            return max(high, opened) >= self.entry
        return min(low, opened) <= self.entry

    def target_touched(self, candle: dict[str, Any]) -> bool:
        high = float(candle["high"])
        low = float(candle["low"])
        opened = float(candle["open"])
        if self.is_long:
            return max(high, opened) >= self.tg1
        return min(low, opened) <= self.tg1

    def stop_touched(self, candle: dict[str, Any]) -> bool:
        high = float(candle["high"])
        low = float(candle["low"])
        opened = float(candle["open"])
        if self.is_long:
            return min(low, opened) <= self.sl
        return max(high, opened) >= self.sl

    def classify_terminal_candle(self, candle: dict[str, Any]) -> str | None:
        """Return TARGET, STOP, AMBIGUOUS or None, using candle-open ordering."""
        opened = float(candle["open"])
        target = self.target_touched(candle)
        stop = self.stop_touched(candle)
        if self.is_long:
            if opened >= self.tg1:
                return "TARGET"
            if opened <= self.sl:
                return "STOP"
        else:
            if opened <= self.tg1:
                return "TARGET"
            if opened >= self.sl:
                return "STOP"
        if target and stop:
            return "AMBIGUOUS"
        if target:
            return "TARGET"
        if stop:
            return "STOP"
        return None

    def price_distance_to_entry_pct(self, price: float | None) -> float | None:
        if price is None:
            return None
        return ((price - self.entry) / self.entry) * 100.0

    def approach_gap_pct(self, price: float | None) -> float | None:
        """Positive gap means price is still on the pre-entry side."""
        if price is None:
            return None
        gap = ((self.entry - price) / self.entry) * 100.0
        return gap if self.is_long else -gap

    def order_details(self, order_type: str = "STOP_LOSS_LIMIT", time_in_force: str = "GTC") -> dict[str, Any]:
        return {
            "type": order_type,
            "side": self.order_side,
            "stopPrice": self.entry,
            "price": self.entry,
            "timeInForce": time_in_force,
        }

    def to_config(self, operational: dict[str, Any] | None = None) -> dict[str, Any]:
        config: dict[str, Any] = {
            "setup_id": self.setup_id,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "direction": self.direction,
            "main_interval": self.main_interval,
            "timezone_offset_minutes": self.timezone_offset_minutes,
            "entry": self.entry,
            "tg1": self.tg1,
            "sl": self.sl,
            "setup_x_timestamp_utc": utc_iso(self.x_ms),
            "setup_a_timestamp_utc": utc_iso(self.a_ms),
            "setup_b_timestamp_utc": utc_iso(self.b_ms),
            "setup_c_timestamp_utc": utc_iso(self.c_ms),
            "setup_d_timestamp_utc": utc_iso(self.d_ms),
            "confirmed_at_utc": self.confirmed_at_utc or utc_iso(),
        }
        if operational:
            config.update(operational)
        return config

    def public(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "direction": self.direction,
            "main_interval": self.main_interval,
            "timezone_offset_minutes": self.timezone_offset_minutes,
            "timezone_label": self.timezone_label,
            "entry": self.entry,
            "tg1": self.tg1,
            "sl": self.sl,
            "X": utc_iso(self.x_ms),
            "A": utc_iso(self.a_ms),
            "B": utc_iso(self.b_ms),
            "C": utc_iso(self.c_ms),
            "D": utc_iso(self.d_ms),
            "local_times": self.local_times(),
            "confirmed_at_utc": self.confirmed_at_utc,
        }

    def fingerprint(self) -> str:
        payload = {
            "setup_id": self.setup_id,
            "entry": self.entry,
            "tg1": self.tg1,
            "sl": self.sl,
            "timestamps": self.timestamps_ms,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _pick(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def normalize_setup_payload(payload: dict[str, Any] | None) -> Setup:
    """Validate an untrusted GUI payload and return a normalized Setup."""
    if not isinstance(payload, dict):
        raise SetupValidationError("Setup nije poslat kao JSON objekat.")

    errors: list[str] = []

    def guarded(parse, *args):
        try:
            return parse(*args)
        except SetupValidationError as exc:
            errors.extend(exc.errors)
            return None

    exchange = str(payload.get("exchange") or "binance").strip().lower()
    market_type = str(payload.get("market_type") or "spot").strip().lower()
    if exchange not in SUPPORTED_EXCHANGES:
        errors.append(f"Nepodržana berza: {exchange}.")
    if market_type not in SUPPORTED_MARKET_TYPES:
        errors.append(f"Nepodržan tip tržišta: {market_type}.")

    symbol = guarded(normalized_symbol, payload.get("symbol"))
    direction = str(payload.get("direction") or "long").strip().lower()
    if direction not in SUPPORTED_DIRECTIONS:
        errors.append("Smer mora biti long ili short.")
    main_interval = str(payload.get("main_interval") or "1h").strip().lower()
    if main_interval not in SUPPORTED_PRIMARY_INTERVALS:
        errors.append("Primarni timeframe mora biti 15m, 30m, 1h ili 4h.")

    offset = guarded(parse_offset_minutes, payload.get("timezone_offset_minutes"))

    entry = guarded(parse_price, _pick(payload, "entry"), "ENTRY")
    tg1 = guarded(parse_price, _pick(payload, "tg1"), "TG1")
    sl = guarded(parse_price, _pick(payload, "sl"), "SL")

    timestamps: dict[str, int] = {}
    for key in TIMESTAMP_KEYS:
        label = key.upper()
        local_value = payload.get(f"setup_{key}_local") or payload.get(f"{key}_local")
        utc_value = payload.get(f"setup_{key}_timestamp_utc") or payload.get(f"{key}_utc")
        if local_value:
            if offset is None:
                errors.append(f"{label}: nedostaje vremenska zona za lokalno vreme.")
                continue
            value = guarded(local_input_to_utc, local_value, offset, f"{label} lokalno vreme")
        elif utc_value:
            value = guarded(parse_utc_datetime, utc_value, f"{label} UTC vreme")
        else:
            errors.append(f"{label} vreme je obavezno.")
            value = None
        if value is not None:
            timestamps[key] = value

    if len(timestamps) == 5:
        ordered = [timestamps[k] for k in TIMESTAMP_KEYS]
        if not all(left < right for left, right in zip(ordered, ordered[1:])):
            errors.append("Vremena moraju biti hronološki poređana: X < A < B < C < D.")

    if entry is not None and tg1 is not None and sl is not None:
        if direction == "long":
            if not (sl < entry < tg1):
                errors.append("Za long setup mora da važi SL < ENTRY < TG1.")
        elif direction == "short":
            if not (tg1 < entry < sl):
                errors.append("Za short setup mora da važi TG1 < ENTRY < SL.")
        elif not (sl < entry < tg1 or tg1 < entry < sl):
            errors.append("Nivoi ENTRY/TG1/SL nisu u skladu sa smerom setupa.")

    if errors:
        raise SetupValidationError("Setup nije ispravan.", errors)

    assert symbol is not None and offset is not None and entry is not None
    assert tg1 is not None and sl is not None
    return Setup(
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        direction=direction,
        main_interval=main_interval,
        timezone_offset_minutes=offset,
        entry=entry,
        tg1=tg1,
        sl=sl,
        x_ms=timestamps["x"],
        a_ms=timestamps["a"],
        b_ms=timestamps["b"],
        c_ms=timestamps["c"],
        d_ms=timestamps["d"],
        confirmed_at_utc=utc_iso(),
    )


def setup_from_config(config: dict[str, Any]) -> Setup:
    """Build and validate a Setup from persisted config."""
    payload = dict(config)
    if not payload.get("exchange"):
        payload["exchange"] = "binance"
    if not payload.get("market_type"):
        payload["market_type"] = "spot"
    if not payload.get("direction"):
        payload["direction"] = "long"
    if not payload.get("main_interval"):
        payload["main_interval"] = "1h"
    if "timezone_offset_minutes" not in payload:
        payload["timezone_offset_minutes"] = 0
    setup = normalize_setup_payload(payload)
    confirmed = payload.get("confirmed_at_utc")
    if confirmed:
        return Setup(
            exchange=setup.exchange,
            market_type=setup.market_type,
            symbol=setup.symbol,
            direction=setup.direction,
            main_interval=setup.main_interval,
            timezone_offset_minutes=setup.timezone_offset_minutes,
            entry=setup.entry,
            tg1=setup.tg1,
            sl=setup.sl,
            x_ms=setup.x_ms,
            a_ms=setup.a_ms,
            b_ms=setup.b_ms,
            c_ms=setup.c_ms,
            d_ms=setup.d_ms,
            confirmed_at_utc=str(confirmed),
        )
    return setup


def default_operational_config() -> dict[str, Any]:
    return {
        "wake_distance_pct": 0.005,
        "fast_move_pct_60s": 0.0015,
        "max_market_data_age_seconds": 15,
        "historical_replay_interval": "1m",
        "order_type_for_upward_crossing": "STOP_LOSS_LIMIT",
        "time_in_force": "GTC",
        "auto_trade": False,
        "live_trigger_cooldown_seconds": 30,
        "stale_ai_price_move_pct": 0.002,
        "snapshot_limit_1m": 240,
        "snapshot_limit_5m": 288,
        "snapshot_limit_primary": 720,
    }


def compact_candles(rows: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    keys = (
        "open_time_utc", "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "closed",
    )
    items = list(rows)[-max(1, int(limit)):]
    return [{key: row[key] for key in keys if key in row} for row in items]
