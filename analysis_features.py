"""Deterministic market features for the Signal Strategy AI snapshot.

Everything in this module is calculated from supplied OHLCV data. It never
invents candles, levels or ratios: missing inputs yield ``None`` / explicit
warnings instead of a guessed value.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

from setup_config import INTERVAL_MS, Setup, utc_iso

UTC = timezone.utc


def _sorted(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in rows if row), key=lambda row: int(row["open_time_ms"]))


def _closed(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in _sorted(rows) if bool(row.get("closed", True))]


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _pct_change(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return ((new - old) / old) * 100.0


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"Nepodržan interval: {interval}") from exc


def candle_index_at(rows: list[dict[str, Any]], ts_ms: int, interval: str) -> int | None:
    step = interval_ms(interval)
    ordered = _sorted(rows)
    for index, candle in enumerate(ordered):
        opened = int(candle["open_time_ms"])
        if opened <= ts_ms < opened + step:
            return index
        if opened > ts_ms:
            return None
    return None


def candle_at(rows: list[dict[str, Any]], ts_ms: int, interval: str) -> dict[str, Any] | None:
    index = candle_index_at(rows, ts_ms, interval)
    if index is None:
        return None
    return _sorted(rows)[index]


def candle_anatomy(candle: dict[str, Any] | None, previous_close: float | None = None) -> dict[str, Any] | None:
    if not candle:
        return None
    opened = _num(candle.get("open"))
    high = _num(candle.get("high"))
    low = _num(candle.get("low"))
    close = _num(candle.get("close"))
    volume = _num(candle.get("volume"), 0.0) or 0.0
    if None in (opened, high, low, close):
        return None
    candle_range = high - low
    body = abs(close - opened)
    upper_wick = high - max(opened, close)
    lower_wick = min(opened, close) - low
    quote_volume = _num(candle.get("quote_volume"))
    trades = candle.get("trades")
    taker_buy_base = _num(candle.get("taker_buy_base"))
    taker_buy_quote = _num(candle.get("taker_buy_quote"))
    true_range = max(
        candle_range,
        abs(high - previous_close) if previous_close is not None else candle_range,
        abs(low - previous_close) if previous_close is not None else candle_range,
    )
    return {
        "open_time_utc": candle.get("open_time_utc"),
        "open": opened,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "quote_volume": quote_volume,
        "trades": trades,
        "closed": bool(candle.get("closed", True)),
        "direction": "bullish" if close > opened else "bearish" if close < opened else "flat",
        "range": _round(candle_range),
        "body": _round(body),
        "body_pct_of_range": _round(_ratio(body, candle_range)),
        "upper_wick": _round(upper_wick),
        "lower_wick": _round(lower_wick),
        "upper_wick_pct_of_range": _round(_ratio(upper_wick, candle_range)),
        "lower_wick_pct_of_range": _round(_ratio(lower_wick, candle_range)),
        "close_location": _round(_ratio(close - low, candle_range)),
        "true_range": _round(true_range),
        "taker_buy_ratio": _round(_ratio(taker_buy_base, volume)) if taker_buy_base is not None else None,
        "taker_buy_quote_ratio": _round(_ratio(taker_buy_quote, quote_volume)) if taker_buy_quote is not None else None,
        "taker_delta_base": _round((taker_buy_base or 0) - (volume - (taker_buy_base or 0))) if taker_buy_base is not None else None,
    }


def atr(candles: list[dict[str, Any]], period: int = 14) -> dict[str, Any] | None:
    rows = _closed(candles)
    if len(rows) < period + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        previous_close = float(rows[index - 1]["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = ((value * (period - 1)) + true_range) / period
    last_close = float(rows[-1]["close"])
    return {
        "period": period,
        "value": _round(value),
        "last_true_range": _round(true_ranges[-1]),
        "atr_pct_of_close": _round(_pct_change(last_close, last_close + value)),
        "bars_used": len(rows),
        "last_closed_at_utc": rows[-1].get("open_time_utc"),
    }


def rsi(candles: list[dict[str, Any]], period: int = 14) -> dict[str, Any] | None:
    rows = _closed(candles)
    if len(rows) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = float(rows[index]["close"]) - float(rows[index - 1]["close"])
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain = gains / period
    average_loss = losses / period
    for index in range(period + 1, len(rows)):
        change = float(rows[index]["close"]) - float(rows[index - 1]["close"])
        average_gain = ((average_gain * (period - 1)) + max(change, 0.0)) / period
        average_loss = ((average_loss * (period - 1)) + max(-change, 0.0)) / period
    if average_loss == 0:
        value = 100.0 if average_gain > 0 else 50.0
    else:
        relative_strength = average_gain / average_loss
        value = 100.0 - (100.0 / (1.0 + relative_strength))
    return {
        "period": period,
        "value": _round(value),
        "last_closed_at_utc": rows[-1].get("open_time_utc"),
        "bars_used": len(rows),
    }


def ema(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    multiplier = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append((float(value) - result[-1]) * multiplier + result[-1])
    return result


def ema_block(candles: list[dict[str, Any]], period: int, slope_bars: int = 5) -> dict[str, Any] | None:
    rows = _closed(candles)
    closes = [float(row["close"]) for row in rows]
    if len(closes) < period:
        return None
    series = ema(closes, period)
    value = series[-1]
    prior = series[-1 - slope_bars] if len(series) > slope_bars else None
    return {
        "period": period,
        "value": _round(value),
        "slope_bars": slope_bars,
        "slope_pct": _round(_pct_change(prior, value)),
        "slope_per_bar_pct": _round(_pct_change(prior, value) / slope_bars) if prior else None,
        "last_closed_at_utc": rows[-1].get("open_time_utc"),
    }


def relative_volume(candles: list[dict[str, Any]], lookback: int = 20) -> dict[str, Any] | None:
    rows = _closed(candles)
    if len(rows) < 2:
        return None
    last = rows[-1]
    baseline_rows = rows[-1 - lookback:-1] or rows[:-1]
    baseline = [float(row.get("volume") or 0) for row in baseline_rows]
    baseline_quote = [float(row.get("quote_volume") or 0) for row in baseline_rows if row.get("quote_volume") is not None]
    median = statistics.median(baseline) if baseline else None
    last_volume = float(last.get("volume") or 0)
    last_quote = _num(last.get("quote_volume"))
    taker_ratio = None
    taker_base = _num(last.get("taker_buy_base"))
    if taker_base is not None and last_volume > 0:
        taker_ratio = taker_base / last_volume
    trades = last.get("trades")
    return {
        "baseline_bars": len(baseline_rows),
        "baseline_volume_median": _round(median),
        "baseline_quote_volume_median": _round(statistics.median(baseline_quote)) if baseline_quote else None,
        "last_closed_volume": _round(last_volume),
        "last_closed_quote_volume": _round(last_quote),
        "last_closed_trades": trades,
        "last_closed_taker_buy_ratio": _round(taker_ratio),
        "relative_volume": _round(_ratio(last_volume, median)),
        "relative_quote_volume": _round(_ratio(last_quote, statistics.median(baseline_quote))) if baseline_quote else None,
        "last_closed_at_utc": last.get("open_time_utc"),
    }


def find_pivots(
    candles: list[dict[str, Any]],
    kind: str,
    left: int = 2,
    right: int = 2,
    limit: int = 6,
) -> list[dict[str, Any]]:
    rows = _closed(candles)
    pivots: list[dict[str, Any]] = []
    key = "high" if kind == "high" else "low"
    for index in range(left, len(rows) - right):
        value = float(rows[index][key])
        left_values = [float(rows[position][key]) for position in range(index - left, index)]
        right_values = [float(rows[position][key]) for position in range(index + 1, index + 1 + right)]
        if kind == "high":
            valid = value > max(left_values) and value >= max(right_values)
        else:
            valid = value < min(left_values) and value <= min(right_values)
        if valid:
            pivots.append(
                {
                    "index": index,
                    "open_time_utc": rows[index].get("open_time_utc"),
                    "price": value,
                }
            )
    return pivots[-limit:]


def market_structure(candles: list[dict[str, Any]]) -> dict[str, Any]:
    highs = find_pivots(candles, "high")
    lows = find_pivots(candles, "low")
    last_highs = highs[-2:]
    last_lows = lows[-2:]
    higher_high = None
    higher_low = None
    if len(last_highs) == 2:
        higher_high = last_highs[1]["price"] > last_highs[0]["price"]
    if len(last_lows) == 2:
        higher_low = last_lows[1]["price"] > last_lows[0]["price"]
    if higher_high is None or higher_low is None:
        label = "insufficient_swings"
    elif higher_high and higher_low:
        label = "bullish_higher_highs_higher_lows"
    elif not higher_high and not higher_low:
        label = "bearish_lower_highs_lower_lows"
    else:
        label = "mixed"
    rows = _closed(candles)
    recent = rows[-20:] if len(rows) >= 20 else rows
    recent_high = max((float(row["high"]) for row in recent), default=None)
    recent_low = min((float(row["low"]) for row in recent), default=None)
    return {
        "label": label,
        "higher_high": higher_high,
        "higher_low": higher_low,
        "last_swing_highs": last_highs,
        "last_swing_lows": last_lows,
        "recent_high": _round(recent_high),
        "recent_low": _round(recent_low),
        "recent_bars": len(recent),
    }


def level_interactions(
    candles: list[dict[str, Any]],
    setup: Setup,
    level: float,
    lookback: int = 20,
) -> dict[str, Any]:
    rows = _closed(candles)[-lookback:]
    touches = 0
    closes_beyond = 0
    closes_pre = 0
    last_touch: str | None = None
    for candle in rows:
        high = float(candle["high"])
        low = float(candle["low"])
        opened = float(candle["open"])
        close = float(candle["close"])
        touched = (max(high, opened) >= level) if setup.is_long else (min(low, opened) <= level)
        if not touched:
            continue
        touches += 1
        last_touch = candle.get("open_time_utc")
        if setup.is_long:
            if close > level:
                closes_beyond += 1
            else:
                closes_pre += 1
        else:
            if close < level:
                closes_beyond += 1
            else:
                closes_pre += 1
    return {
        "level": level,
        "lookback_bars": len(rows),
        "touch_count": touches,
        "closes_beyond_level": closes_beyond,
        "closes_before_level": closes_pre,
        "last_touch_utc": last_touch,
        "status": "no_recent_touch" if touches == 0 else "touched_recently",
    }


def timeframe_context(
    setup: Setup,
    candles: list[dict[str, Any]],
    timeframe: str,
    live_price: float | None,
    *,
    lookback_levels: int = 20,
) -> dict[str, Any]:
    rows = _sorted(candles)
    closed_rows = _closed(rows)
    forming = rows[-1] if rows and not bool(rows[-1].get("closed", True)) else None
    closed_anatomy = candle_anatomy(
        closed_rows[-1] if closed_rows else None,
        float(closed_rows[-2]["close"]) if len(closed_rows) > 1 else None,
    )
    last_close = float(closed_rows[-1]["close"]) if closed_rows else None
    changes: dict[str, float | None] = {}
    for bars in (3, 10):
        prior = float(closed_rows[-1 - bars]["close"]) if len(closed_rows) > bars else None
        changes[f"change_pct_last_{bars}_bars"] = _round(_pct_change(prior, last_close))

    entry_gap = setup.approach_gap_pct(live_price)
    approach_speed = None
    approach_reference = None
    if len(closed_rows) >= 6 and live_price is not None:
        first = closed_rows[-6]
        first_gap = setup.approach_gap_pct(float(first["close"]))
        step = interval_ms(timeframe)
        elapsed_hours = max(((int(closed_rows[-1]["open_time_ms"]) - int(first["open_time_ms"])) + step) / 3_600_000, 1 / 60)
        if first_gap is not None and entry_gap is not None:
            approach_speed = (first_gap - entry_gap) / elapsed_hours
        approach_reference = {
            "from_open_time_utc": first.get("open_time_utc"),
            "gap_then_pct": _round(first_gap),
            "gap_now_pct": _round(entry_gap),
            "elapsed_hours": _round(elapsed_hours),
        }

    atr_now = atr(closed_rows, 14)
    volatility_regime = "insufficient_data"
    atr_change = None
    if len(closed_rows) >= 60:
        atr_prior = atr(closed_rows[:-40], 14)
        if atr_now and atr_prior and atr_now["value"] and atr_prior["value"]:
            atr_change = (atr_now["value"] / atr_prior["value"]) - 1.0
            if atr_change > 0.25:
                volatility_regime = "high_vs_recent"
            elif atr_change < -0.20:
                volatility_regime = "low_vs_recent"
            else:
                volatility_regime = "normal"

    return {
        "timeframe": timeframe,
        "bars": len(rows),
        "closed_bars": len(closed_rows),
        "last_closed_at_utc": closed_rows[-1].get("open_time_utc") if closed_rows else None,
        "last_closed_close": _round(last_close),
        "forming_candle": candle_anatomy(
            forming,
            float(closed_rows[-1]["close"]) if closed_rows else None,
        ),
        "market_structure": market_structure(closed_rows),
        "momentum": {
            **changes,
            "rsi14": (rsi(closed_rows, 14) or {}).get("value"),
            "ema20": ema_block(closed_rows, 20, 5),
            "direction_last_3_bars": "up" if (changes.get("change_pct_last_3_bars") or 0) > 0 else "down" if (changes.get("change_pct_last_3_bars") or 0) < 0 else "flat",
        },
        "volatility": {
            "atr14": _round(atr_now["value"] if atr_now else None),
            "atr_pct_of_close": _round(atr_now["atr_pct_of_close"] if atr_now else None),
            "change_vs_40_bars_ago": _round(atr_change),
            "regime": volatility_regime,
        },
        "levels": {
            "distance_price_to_entry_pct": _round(setup.price_distance_to_entry_pct(live_price)),
            "distance_price_to_tg1_pct": _round(_pct_change(live_price, setup.tg1)),
            "distance_price_to_sl_pct": _round(_pct_change(live_price, setup.sl)),
            "entry_gap_pct": _round(entry_gap),
            "approach_speed_pp_per_hour": _round(approach_speed),
            "approach_reference": approach_reference,
            "entry": level_interactions(closed_rows, setup, setup.entry, lookback_levels),
            "tg1": level_interactions(closed_rows, setup, setup.tg1, lookback_levels),
            "sl": level_interactions(closed_rows, setup, setup.sl, lookback_levels),
        },
        "last_closed_candle_anatomy": closed_anatomy,
    }


def historical_touches(setup: Setup, minute_bars: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _sorted([row for row in minute_bars if int(row["open_time_ms"]) >= setup.d_ms])
    result: dict[str, Any] = {
        "status": "DATA_INVALID",
        "reason": "Nema dostupnih 1m sveća od D; ne mogu da proverim dodire ENTRY/TG1/SL.",
        "reason_short": "Nema 1m istorije od D.",
        "bars_since_d": len(rows),
        "entry": {"touched": False, "touch_count": 0, "first_touch_utc": None, "last_touch_utc": None},
        "tg1": {"touched": False, "touch_count": 0, "first_touch_utc": None, "last_touch_utc": None},
        "sl": {"touched": False, "touch_count": 0, "first_touch_utc": None, "last_touch_utc": None},
        "terminal": {"event": None, "ambiguous": False, "first_terminal_utc": None, "candle": None},
        "entry_touched_before_terminal": False,
    }
    if not rows:
        return result

    first_terminal: dict[str, Any] | None = None
    for candle in rows:
        stamp = candle.get("open_time_utc")
        for key, predicate in (
            ("entry", setup.entry_touched(candle)),
            ("tg1", setup.target_touched(candle)),
            ("sl", setup.stop_touched(candle)),
        ):
            if not predicate:
                continue
            bucket = result[key]
            bucket["touched"] = True
            bucket["touch_count"] += 1
            bucket["last_touch_utc"] = stamp
            if bucket["first_touch_utc"] is None:
                bucket["first_touch_utc"] = stamp
        if first_terminal is not None:
            continue
        event = setup.classify_terminal_candle(candle)
        if event is None:
            continue
        first_terminal = {"event": event, "candle": candle, "utc": stamp}
        if event == "AMBIGUOUS":
            result["terminal"] = {
                "event": "AMBIGUOUS",
                "ambiguous": True,
                "first_terminal_utc": stamp,
                "candle": candle_anatomy(candle),
            }
            result["status"] = "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS"
            result["reason"] = (
                f"Ista 1m sveća {stamp} dodiruje i TG1 i SL; iz OHLC podataka se ne može "
                "utvrditi koji je nivo prvi dodirnut."
            )
            result["reason_short"] = "TG1/SL dodirnuti u istoj 1m sveći; redosled nije poznat."
            return result
        if event == "TARGET":
            result["terminal"] = {
                "event": "TARGET",
                "ambiguous": False,
                "first_terminal_utc": stamp,
                "candle": candle_anatomy(candle),
            }
            result["status"] = "TARGET_REACHED_BEFORE_ENTRY"
            result["reason"] = f"TG1 ({setup.tg1:.2f}) je dodirnut u 1m sveći {stamp}; setup je završio pre nove entry odluke."
            result["reason_short"] = f"TG1 dodirnut u {stamp}."
            result["entry_touched_before_terminal"] = bool(result["entry"]["touched"])
            return result
        if event == "STOP":
            result["terminal"] = {
                "event": "STOP",
                "ambiguous": False,
                "first_terminal_utc": stamp,
                "candle": candle_anatomy(candle),
            }
            result["status"] = "SETUP_INVALIDATED"
            result["reason"] = f"SL ({setup.sl:.2f}) je dodirnut u 1m sveći {stamp}; setup je poništen."
            result["reason_short"] = f"SL dodirnut u {stamp}."
            result["entry_touched_before_terminal"] = bool(result["entry"]["touched"])
            return result

    result["status"] = "ACTIVE_WATCH"
    result["reason"] = (
        "Od D do sada nisu dodirnuti ni TG1 ni SL. Setup ostaje aktivan; prethodni "
        "dodir ENTRY nije terminalan."
    )
    result["reason_short"] = "TG1 i SL nisu dodirnuti od D; setup je aktivan."
    result["entry_touched_before_terminal"] = bool(result["entry"]["touched"])
    return result


def _leg(
    from_key: str,
    to_key: str,
    from_point: dict[str, Any],
    to_point: dict[str, Any],
) -> dict[str, Any] | None:
    price_from = from_point.get("swing_price")
    price_to = to_point.get("swing_price")
    if price_from is None or price_to is None:
        return None
    from_index = from_point.get("candle_index")
    to_index = to_point.get("candle_index")
    duration_bars = None
    duration_ms = None
    if from_index is not None and to_index is not None:
        duration_bars = max(0, int(to_index) - int(from_index))
    if from_point.get("timestamp_ms") is not None and to_point.get("timestamp_ms") is not None:
        duration_ms = int(to_point["timestamp_ms"]) - int(from_point["timestamp_ms"])
    return {
        "from": from_key.upper(),
        "to": to_key.upper(),
        "price_from": price_from,
        "price_to": price_to,
        "delta": _round(price_to - price_from),
        "abs_delta": _round(abs(price_to - price_from)),
        "pct_change": _round(_pct_change(price_from, price_to)),
        "direction": "up" if price_to > price_from else "down" if price_to < price_from else "flat",
        "duration_bars": duration_bars,
        "duration_minutes": _round(duration_ms / 60_000) if duration_ms is not None else None,
        "duration_hours": _round(duration_ms / 3_600_000) if duration_ms is not None else None,
    }


def _leg_volume_stats(candles: list[dict[str, Any]], start_index: int | None, end_index: int | None) -> dict[str, Any] | None:
    if start_index is None or end_index is None:
        return None
    segment = candles[min(start_index, end_index):max(start_index, end_index) + 1]
    if not segment:
        return None
    volumes = [float(row.get("volume") or 0) for row in segment]
    quotes = [float(row.get("quote_volume") or 0) for row in segment if row.get("quote_volume") is not None]
    taker = [float(row.get("taker_buy_base") or 0) for row in segment if row.get("taker_buy_base") is not None]
    return {
        "bars": len(segment),
        "volume_sum": _round(sum(volumes)),
        "volume_avg_per_bar": _round(sum(volumes) / len(volumes)) if volumes else None,
        "quote_volume_sum": _round(sum(quotes)) if quotes else None,
        "taker_buy_ratio": _round(sum(taker) / sum(volumes)) if taker and sum(volumes) > 0 else None,
    }


def analyze_pattern(setup: Setup, primary_candles: list[dict[str, Any]]) -> dict[str, Any]:
    interval = setup.main_interval
    rows = _sorted(primary_candles)
    closed_rows = _closed(rows)
    swing_kind = {
        "x": "low" if setup.is_long else "high",
        "a": "high" if setup.is_long else "low",
        "b": "low" if setup.is_long else "high",
        "c": "high" if setup.is_long else "low",
        "d": "low" if setup.is_long else "high",
    }
    points: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for key in ("x", "a", "b", "c", "d"):
        ts_ms = setup.timestamps_ms[key]
        index = candle_index_at(rows, ts_ms, interval)
        candle = rows[index] if index is not None else None
        label = key.upper()
        if candle is None:
            warnings.append(f"Nema {interval} sveće za {label} ({utc_iso(ts_ms)}).")
            points[key] = {
                "timestamp_ms": ts_ms,
                "timestamp_utc": utc_iso(ts_ms),
                "swing_kind": swing_kind[key],
                "swing_price": None,
                "candle_index": None,
                "candle": None,
                "missing": True,
                "timestamp_aligned_to_candle_open": False,
            }
            continue
        previous_close = float(rows[index - 1]["close"]) if index > 0 else None
        swing_price = float(candle["high"] if swing_kind[key] == "high" else candle["low"])
        points[key] = {
            "timestamp_ms": ts_ms,
            "timestamp_utc": utc_iso(ts_ms),
            "swing_kind": swing_kind[key],
            "swing_price": swing_price,
            "candle_index": index,
            "candle": candle_anatomy(candle, previous_close),
            "missing": False,
            "timestamp_aligned_to_candle_open": int(candle["open_time_ms"]) == int(ts_ms),
        }
        if int(candle["open_time_ms"]) != int(ts_ms):
            warnings.append(f"{label} timestamp nije na otvaranju {interval} sveće; korišćena je sveća koja ga sadrži.")

    legs = {
        "xa": _leg("x", "a", points["x"], points["a"]),
        "ab": _leg("a", "b", points["a"], points["b"]),
        "bc": _leg("b", "c", points["b"], points["c"]),
        "cd": _leg("c", "d", points["c"], points["d"]),
        "ad": _leg("a", "d", points["a"], points["d"]),
    }
    xa = legs.get("xa")
    ab = legs.get("ab")
    bc = legs.get("bc")
    cd = legs.get("cd")
    ad = legs.get("ad")
    ratios = {
        "ab_over_xa": _round(_ratio(ab["abs_delta"] if ab else None, xa["abs_delta"] if xa else None)),
        "bc_over_ab": _round(_ratio(bc["abs_delta"] if bc else None, ab["abs_delta"] if ab else None)),
        "cd_over_bc": _round(_ratio(cd["abs_delta"] if cd else None, bc["abs_delta"] if bc else None)),
        "ad_over_xa": _round(_ratio(ad["abs_delta"] if ad else None, xa["abs_delta"] if xa else None)),
    }
    ratios["ab_retracement_of_xa_pct"] = _round(ratios["ab_over_xa"] * 100) if ratios["ab_over_xa"] is not None else None
    ratios["bc_retracement_of_ab_pct"] = _round(ratios["bc_over_ab"] * 100) if ratios["bc_over_ab"] is not None else None
    ratios["cd_extension_of_bc_pct"] = _round(ratios["cd_over_bc"] * 100) if ratios["cd_over_bc"] is not None else None
    ratios["ad_extension_of_xa_pct"] = _round(ratios["ad_over_xa"] * 100) if ratios["ad_over_xa"] is not None else None

    prices = {key: points[key].get("swing_price") for key in points}
    geometry = {
        "expected_for_direction": {
            "xa": "up" if setup.is_long else "down",
            "ab": "down" if setup.is_long else "up",
            "bc": "up" if setup.is_long else "down",
            "cd": "down" if setup.is_long else "up",
        },
        "leg_directions": {
            key: (leg or {}).get("direction") for key, leg in legs.items()
        },
        "x_below_a": prices["x"] is not None and prices["a"] is not None and ((prices["x"] < prices["a"]) if setup.is_long else (prices["x"] > prices["a"])),
        "b_below_a": prices["b"] is not None and prices["a"] is not None and ((prices["b"] < prices["a"]) if setup.is_long else (prices["b"] > prices["a"])),
        "c_above_b": prices["c"] is not None and prices["b"] is not None and ((prices["c"] > prices["b"]) if setup.is_long else (prices["c"] < prices["b"])),
        "d_below_c": prices["d"] is not None and prices["c"] is not None and ((prices["d"] < prices["c"]) if setup.is_long else (prices["d"] > prices["c"])),
        "d_beyond_x": prices["d"] is not None and prices["x"] is not None and ((prices["d"] < prices["x"]) if setup.is_long else (prices["d"] > prices["x"])),
        "d_beyond_b": prices["d"] is not None and prices["b"] is not None and ((prices["d"] < prices["b"]) if setup.is_long else (prices["d"] > prices["b"])),
        "d_vs_b_delta": _round(prices["d"] - prices["b"]) if prices["d"] is not None and prices["b"] is not None else None,
        "d_vs_x_delta": _round(prices["d"] - prices["x"]) if prices["d"] is not None and prices["x"] is not None else None,
        "levels_inside_leg": {
            "entry_between_c_and_d": (
                prices["c"] is not None and prices["d"] is not None
                and min(prices["c"], prices["d"]) < setup.entry < max(prices["c"], prices["d"])
            ),
        },
    }

    return {
        "timeframe": interval,
        "points": {key.upper(): value for key, value in points.items()},
        "legs": legs,
        "ratios": ratios,
        "geometry": geometry,
        "warnings": warnings,
    }


def analyze_cd_leg(
    setup: Setup,
    primary_candles: list[dict[str, Any]],
    pattern: dict[str, Any],
) -> dict[str, Any]:
    rows = _sorted(primary_candles)
    points = pattern.get("points", {})
    c_point = points.get("C", {})
    d_point = points.get("D", {})
    c_index = c_point.get("candle_index")
    d_index = d_point.get("candle_index")
    cd_leg = pattern.get("legs", {}).get("cd")
    if c_index is None or d_index is None or cd_leg is None:
        return {"available": False, "reason": "C ili D candle nije dostupna; CD leg se ne može izračunati."}

    start = min(c_index, d_index)
    end = max(c_index, d_index)
    segment = rows[start:end + 1]
    if not segment:
        return {"available": False, "reason": "CD segment je prazan."}
    prior_candles = rows[:start]
    atr_at_d = atr(rows[:end + 1], 14)
    prior_atr = atr(prior_candles, 14) if len(prior_candles) >= 15 else None
    segment_high = max(float(row["high"]) for row in segment)
    segment_low = min(float(row["low"]) for row in segment)
    segment_range = segment_high - segment_low
    volumes = [float(row.get("volume") or 0) for row in segment]
    up_bars = sum(1 for row in segment if float(row["close"]) > float(row["open"]))
    down_bars = sum(1 for row in segment if float(row["close"]) < float(row["open"]))
    body_sum = sum(abs(float(row["close"]) - float(row["open"])) for row in segment)
    range_sum = sum(float(row["high"]) - float(row["low"]) for row in segment)
    upper_wick_sum = sum(float(row["high"]) - max(float(row["open"]), float(row["close"])) for row in segment)
    lower_wick_sum = sum(min(float(row["open"]), float(row["close"])) - float(row["low"]) for row in segment)
    taker_base = sum(float(row.get("taker_buy_base") or 0) for row in segment if row.get("taker_buy_base") is not None)
    has_taker = any(row.get("taker_buy_base") is not None for row in segment)

    prior_legs = []
    for key in ("xa", "ab", "bc"):
        leg = pattern.get("legs", {}).get(key)
        if not leg or leg.get("duration_bars") is None:
            continue
        point_a = {"x": "X", "a": "A", "b": "B"}[key[0]]
        point_b = {"a": "A", "b": "B", "c": "C"}[key[1]]
        start_index = points.get(point_a, {}).get("candle_index")
        end_index = points.get(point_b, {}).get("candle_index")
        stats = _leg_volume_stats(rows, start_index, end_index)
        if stats:
            prior_legs.append(stats)
    prior_avg_volume = (
        sum(item["volume_avg_per_bar"] for item in prior_legs if item["volume_avg_per_bar"] is not None) / len(prior_legs)
        if prior_legs else None
    )
    avg_volume = sum(volumes) / len(volumes) if volumes else None
    duration_hours = cd_leg.get("duration_hours")
    momentum_per_hour = _ratio(cd_leg.get("pct_change"), duration_hours)

    d_anatomy = d_point.get("candle")
    c_anatomy = c_point.get("candle")
    return {
        "available": True,
        "price_change_pct": cd_leg.get("pct_change"),
        "abs_price_change": cd_leg.get("abs_delta"),
        "directional_price_change": cd_leg.get("delta"),
        "duration_bars": cd_leg.get("duration_bars"),
        "duration_minutes": cd_leg.get("duration_minutes"),
        "duration_hours": duration_hours,
        "momentum_pct_per_bar": _round(_ratio(cd_leg.get("pct_change"), cd_leg.get("duration_bars"))),
        "momentum_pct_per_hour": _round(momentum_per_hour),
        "range_price": _round(segment_range),
        "range_atr_ratio": _round(_ratio(segment_range, atr_at_d["value"] if atr_at_d else None)),
        "atr14_at_d": _round(atr_at_d["value"] if atr_at_d else None),
        "atr14_before_cd": _round(prior_atr["value"] if prior_atr else None),
        "up_bars": up_bars,
        "down_bars": down_bars,
        "body_to_range_ratio": _round(_ratio(body_sum, range_sum)),
        "upper_wick_to_range_ratio": _round(_ratio(upper_wick_sum, range_sum)),
        "lower_wick_to_range_ratio": _round(_ratio(lower_wick_sum, range_sum)),
        "volume_sum": _round(sum(volumes)),
        "volume_avg_per_bar": _round(avg_volume),
        "volume_ratio_vs_prior_legs": _round(_ratio(avg_volume, prior_avg_volume)),
        "prior_legs_volume": prior_legs,
        "taker_buy_ratio": _round(_ratio(taker_base, sum(volumes))) if has_taker and sum(volumes) > 0 else None,
        "d_candle": d_anatomy,
        "c_candle": c_anatomy,
        "d_relative_to_b": {
            "b_swing": points.get("B", {}).get("swing_price"),
            "d_swing": points.get("D", {}).get("swing_price"),
            "d_beyond_b": pattern.get("geometry", {}).get("d_beyond_b"),
            "delta": pattern.get("geometry", {}).get("d_vs_b_delta"),
        },
        "d_relative_to_x": {
            "x_swing": points.get("X", {}).get("swing_price"),
            "d_swing": points.get("D", {}).get("swing_price"),
            "d_beyond_x": pattern.get("geometry", {}).get("d_beyond_x"),
            "delta": pattern.get("geometry", {}).get("d_vs_x_delta"),
        },
    }


def indicators_primary(setup: Setup, primary_candles: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _closed(primary_candles)
    return {
        "timeframe": setup.main_interval,
        "closed_bars": len(rows),
        "last_closed_at_utc": rows[-1].get("open_time_utc") if rows else None,
        "atr14": atr(rows, 14),
        "rsi14": rsi(rows, 14),
        "ema20": ema_block(rows, 20, 5),
        "ema50": ema_block(rows, 50, 10),
        "relative_volume": relative_volume(rows, 20),
        "recent_swings": {
            "highs": find_pivots(rows, "high"),
            "lows": find_pivots(rows, "low"),
        },
        "market_structure": market_structure(rows),
    }


def post_d_reaction(
    setup: Setup,
    primary_candles: list[dict[str, Any]],
    candles_5m: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    live_price: float | None,
) -> dict[str, Any]:
    primary_after = [row for row in _sorted(primary_candles) if int(row["open_time_ms"]) > setup.d_ms]
    minutes_after = [row for row in _sorted(candles_1m) if int(row["open_time_ms"]) >= setup.d_ms]
    five_after = [row for row in _sorted(candles_5m) if int(row["open_time_ms"]) >= setup.d_ms]
    closed_minutes = [row for row in minutes_after if bool(row.get("closed", True))]
    d_low = None
    d_high = None

    def extreme(rows: list[dict[str, Any]], key: str, find_max: bool) -> dict[str, Any] | None:
        if not rows:
            return None
        row = max(rows, key=lambda item: float(item[key])) if find_max else min(rows, key=lambda item: float(item[key]))
        return {"price": float(row[key]), "open_time_utc": row.get("open_time_utc")}

    post_high = extreme(closed_minutes, "high", True)
    post_low = extreme(closed_minutes, "low", False)
    d_swing = None
    d_candle = candle_at(primary_candles, setup.d_ms, setup.main_interval)
    if d_candle:
        d_swing = float(d_candle["low"] if setup.is_long else d_candle["high"])
        d_low = float(d_candle["low"])
        d_high = float(d_candle["high"])
    if live_price is not None and d_swing:
        live_vs_d_pct = _pct_change(d_swing, live_price)
    else:
        live_vs_d_pct = None

    last_minutes = closed_minutes[-30:]
    up_closes = sum(1 for row in last_minutes if float(row["close"]) > float(row["open"]))
    volume = sum(float(row.get("volume") or 0) for row in last_minutes)
    taker = sum(float(row.get("taker_buy_base") or 0) for row in last_minutes if row.get("taker_buy_base") is not None)
    higher_lows = None
    if len(last_minutes) >= 3:
        lows = [float(row["low"]) for row in last_minutes[-3:]]
        higher_lows = lows[-1] > lows[-2] > lows[-3]
    first_hour = [row for row in closed_minutes if int(row["open_time_ms"]) < setup.d_ms + 3_600_000]

    return {
        "bars_1m_since_d": len(closed_minutes),
        "bars_5m_since_d": len([row for row in five_after if bool(row.get("closed", True))]),
        "primary_bars_after_d": len([row for row in primary_after if bool(row.get("closed", True))]),
        "post_d_high": post_high,
        "post_d_low": post_low,
        "first_hour_after_d": {
            "bars": len(first_hour),
            "high": _round(extreme(first_hour, "high", True)["price"]) if first_hour else None,
            "low": _round(extreme(first_hour, "low", False)["price"]) if first_hour else None,
            "change_pct": _round(_pct_change(float(first_hour[0]["open"]), float(first_hour[-1]["close"]))) if first_hour else None,
        },
        "d_swing_price": _round(d_swing),
        "d_candle_high": _round(d_high),
        "d_candle_low": _round(d_low),
        "live_vs_d_swing_pct": _round(live_vs_d_pct),
        "d_swing_broken": (
            (post_low is not None and d_swing is not None and post_low["price"] < d_swing)
            if setup.is_long
            else (post_high is not None and d_swing is not None and post_high["price"] > d_swing)
        ),
        "last_30_closed_1m": {
            "up_closes": up_closes,
            "down_closes": len(last_minutes) - up_closes,
            "up_close_ratio": _round(_ratio(up_closes, len(last_minutes))),
            "volume": _round(volume),
            "taker_buy_ratio": _round(_ratio(taker, volume)) if taker and volume > 0 else None,
            "higher_lows_last_3": higher_lows,
        },
    }


def live_flow(recent_trades: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not recent_trades:
        return None
    buys = 0.0
    sells = 0.0
    for trade in recent_trades:
        qty = _num(trade.get("qty"), 0.0) or 0.0
        if bool(trade.get("buyer_is_maker")):
            sells += qty
        else:
            buys += qty
    total = buys + sells
    return {
        "trades": len(recent_trades),
        "buy_base": _round(buys),
        "sell_base": _round(sells),
        "taker_buy_ratio": _round(_ratio(buys, total)) if total else None,
        "delta_base": _round(buys - sells),
    }


def build_derived_features(
    setup: Setup,
    *,
    primary_candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]],
    candles_5m: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    live_price: float | None,
    live_price_ms: int | None,
    now_ms: int,
    recent_trades: list[dict[str, Any]] | None = None,
    candles_1m_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    primary_rows = _sorted(primary_candles)
    pattern = analyze_pattern(setup, primary_rows)
    touches = historical_touches(
        setup,
        candles_1m_history if candles_1m_history is not None else candles_1m,
    )
    context = {
        timeframe: timeframe_context(
            setup,
            candles_1h if timeframe == "1h" else candles_5m if timeframe == "5m" else candles_1m,
            timeframe,
            live_price,
        )
        for timeframe in ("1h", "5m", "1m")
    }
    warnings = list(pattern.get("warnings", []))
    if not _closed(candles_1m):
        warnings.append("Nema zatvorenih 1m sveća.")
    if not primary_rows:
        warnings.append("Nema primarnih sveća.")
    if live_price is None:
        warnings.append("Tekuća cena nije dostupna.")
    forming = {}
    for timeframe, rows in (("1m", candles_1m), ("5m", candles_5m), ("1h", candles_1h)):
        ordered = _sorted(rows)
        if ordered and not bool(ordered[-1].get("closed", True)):
            forming[timeframe] = candle_anatomy(ordered[-1])
    return {
        "as_of_utc": utc_iso(now_ms),
        "live_price": live_price,
        "live_price_timestamp_utc": utc_iso(live_price_ms) if live_price_ms else None,
        "setup_id": setup.setup_id,
        "setup": {
            "symbol": setup.symbol,
            "direction": setup.direction,
            "main_interval": setup.main_interval,
            "entry": setup.entry,
            "tg1": setup.tg1,
            "sl": setup.sl,
            "timezone_offset_minutes": setup.timezone_offset_minutes,
            "local_times": setup.local_times(),
        },
        "xabcd": pattern,
        "cd_leg": analyze_cd_leg(setup, primary_rows, pattern),
        "indicators_primary": indicators_primary(setup, primary_rows),
        "multi_timeframe": context,
        "historical_touches": touches,
        "post_d_reaction": post_d_reaction(setup, primary_rows, candles_5m, candles_1m, live_price),
        "recent_live_flow": live_flow(recent_trades),
        "forming_candles": forming,
        "data_quality": {
            "primary_bars": len(primary_rows),
            "primary_closed_bars": len(_closed(primary_rows)),
            "candles_1h": len(_sorted(candles_1h)),
            "candles_5m": len(_sorted(candles_5m)),
            "candles_1m": len(_sorted(candles_1m)),
            "closed_1m_since_d": touches.get("bars_since_d"),
            "warnings": warnings,
        },
    }
