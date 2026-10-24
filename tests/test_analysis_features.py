import unittest
from datetime import datetime, timezone

from analysis_features import (
    analyze_cd_leg,
    analyze_pattern,
    build_derived_features,
    indicators_primary,
    post_d_reaction,
    timeframe_context,
)
from setup_config import normalize_setup_payload


def ms(day, hour, minute=0):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def setup(**overrides):
    payload = {
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "direction": "long",
        "main_interval": "1h",
        "timezone_offset_minutes": 0,
        "setup_x_timestamp_utc": "2026-10-20T13:00:00Z",
        "setup_a_timestamp_utc": "2026-10-20T14:00:00Z",
        "setup_b_timestamp_utc": "2026-10-20T15:00:00Z",
        "setup_c_timestamp_utc": "2026-10-20T16:00:00Z",
        "setup_d_timestamp_utc": "2026-10-20T17:00:00Z",
        "entry": 105,
        "tg1": 115,
        "sl": 95,
    }
    payload.update(overrides)
    return normalize_setup_payload(payload)


def candle(open_ms, opened, high, low, close, *, volume=10.0, taker=None, closed=True):
    taker_base = volume * 0.5 if taker is None else taker
    return {
        "open_time_ms": int(open_ms),
        "open_time_utc": datetime.fromtimestamp(open_ms / 1000, timezone.utc).isoformat(),
        "open": float(opened),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "quote_volume": float(volume) * float(close),
        "trades": 10,
        "taker_buy_base": float(taker_base),
        "taker_buy_quote": float(taker_base) * float(close),
        "closed": closed,
    }


def pattern_candles():
    return [
        candle(ms(20, 12), 106, 107, 105, 106),
        candle(ms(20, 13), 106, 107, 100, 106),
        candle(ms(20, 14), 106, 110, 105, 109),
        candle(ms(20, 15), 109, 110, 104, 105),
        candle(ms(20, 16), 105, 108, 104, 107),
        candle(ms(20, 17), 107, 108, 101, 102),
        candle(ms(20, 18), 102, 104, 100, 103),
        candle(ms(20, 19), 103, 106, 102, 105),
    ]


def trend_candles(count=80, start=100.0, step=0.8):
    rows = []
    for index in range(count):
        base = start + index * step
        wave = 2.0 if (index // 3) % 2 == 0 else -2.0
        opened = base + wave
        close = opened + 0.8
        rows.append(
            candle(
                ms(18, 0) + index * 3_600_000,
                opened,
                max(opened, close) + 0.2,
                min(opened, close) - 0.2,
                close,
                volume=5 + index % 7,
            )
        )
    return rows


class PatternTests(unittest.TestCase):
    def test_xabcd_points_ratios_and_geometry(self):
        s = setup()
        result = analyze_pattern(s, pattern_candles())
        self.assertEqual(result["timeframe"], "1h")
        self.assertEqual(result["points"]["X"]["swing_price"], 100.0)
        self.assertEqual(result["points"]["A"]["swing_price"], 110.0)
        self.assertEqual(result["points"]["B"]["swing_price"], 104.0)
        self.assertEqual(result["points"]["C"]["swing_price"], 108.0)
        self.assertEqual(result["points"]["D"]["swing_price"], 101.0)
        self.assertAlmostEqual(result["ratios"]["ab_over_xa"], 0.6, places=5)
        self.assertAlmostEqual(result["ratios"]["bc_over_ab"], 4 / 6, places=5)
        self.assertAlmostEqual(result["ratios"]["cd_over_bc"], 7 / 4, places=5)
        self.assertEqual(result["geometry"]["d_beyond_b"], True)
        self.assertEqual(result["geometry"]["d_beyond_x"], False)
        self.assertEqual(result["geometry"]["levels_inside_leg"]["entry_between_c_and_d"], True)

    def test_cd_leg_statistics(self):
        s = setup()
        pattern = analyze_pattern(s, pattern_candles())
        result = analyze_cd_leg(s, pattern_candles(), pattern)
        self.assertTrue(result["available"])
        self.assertEqual(result["duration_bars"], 1)
        self.assertEqual(result["up_bars"], 1)
        self.assertEqual(result["down_bars"], 1)
        self.assertAlmostEqual(result["price_change_pct"], ((101 - 108) / 108) * 100, places=5)
        self.assertEqual(result["d_candle"]["low"], 101.0)
        self.assertEqual(result["d_relative_to_b"]["d_beyond_b"], True)

    def test_missing_point_warns_but_does_not_crash(self):
        s = setup()
        rows_without_b = [row for row in pattern_candles() if int(row["open_time_ms"]) != s.b_ms]
        result = analyze_pattern(s, rows_without_b)
        self.assertTrue(any("B" in warning for warning in result["warnings"]))
        self.assertIsNone(result["points"]["B"]["swing_price"])
        self.assertIsNone(analyze_cd_leg(s, rows_without_b, result)["d_relative_to_b"]["b_swing"])
        rows_without_d = [row for row in pattern_candles() if int(row["open_time_ms"]) != s.d_ms]
        missing_d = analyze_pattern(s, rows_without_d)
        self.assertFalse(analyze_cd_leg(s, rows_without_d, missing_d)["available"])


class IndicatorTests(unittest.TestCase):
    def test_indicators_and_context(self):
        s = setup()
        rows = trend_candles()
        indicators = indicators_primary(s, rows)
        self.assertEqual(indicators["timeframe"], "1h")
        self.assertEqual(indicators["closed_bars"], len(rows))
        self.assertGreater(indicators["rsi14"]["value"], 70)
        self.assertGreater(indicators["atr14"]["value"], 0)
        self.assertGreater(indicators["ema20"]["value"], indicators["ema50"]["value"])
        self.assertIsNotNone(indicators["relative_volume"])
        self.assertEqual(indicators["market_structure"]["label"], "bullish_higher_highs_higher_lows")

    def test_timeframe_context_reports_distance_and_structure(self):
        s = setup()
        rows = trend_candles()
        price = 250.0
        context = timeframe_context(s, rows, "1h", price)
        self.assertEqual(context["timeframe"], "1h")
        self.assertEqual(context["market_structure"]["label"], "bullish_higher_highs_higher_lows")
        self.assertIsNotNone(context["levels"]["distance_price_to_entry_pct"])
        self.assertIsNotNone(context["levels"]["approach_reference"]["gap_now_pct"])
        self.assertGreater(context["closed_bars"], 20)


class PostDTests(unittest.TestCase):
    def test_short_uses_post_high_to_detect_broken_swing(self):
        s = setup(
            direction="short",
            entry=102,
            tg1=90,
            sl=112,
            setup_x_timestamp_utc="2026-10-20T13:00:00Z",
            setup_a_timestamp_utc="2026-10-20T14:00:00Z",
            setup_b_timestamp_utc="2026-10-20T15:00:00Z",
            setup_c_timestamp_utc="2026-10-20T16:00:00Z",
            setup_d_timestamp_utc="2026-10-20T17:00:00Z",
        )
        primary = [candle(s.d_ms, 106, 110, 103, 104)]
        minutes = [
            candle(s.d_ms, 104, 106, 102, 103),
            candle(s.d_ms + 60_000, 103, 111, 102, 109),
        ]
        result = post_d_reaction(s, primary, [], minutes, 109)
        self.assertEqual(result["d_swing_price"], 110.0)
        self.assertTrue(result["d_swing_broken"])
        self.assertLess(result["live_vs_d_swing_pct"], 0)

    def test_long_uses_post_low_to_detect_broken_swing(self):
        s = setup()
        primary = [candle(s.d_ms, 106, 108, 101, 102)]
        minutes = [
            candle(s.d_ms, 102, 104, 100, 103),
            candle(s.d_ms + 60_000, 103, 104, 99, 100),
        ]
        result = post_d_reaction(s, primary, [], minutes, 99.5)
        self.assertEqual(result["d_swing_price"], 101.0)
        self.assertTrue(result["d_swing_broken"])
        self.assertLess(result["live_vs_d_swing_pct"], 0)


class DerivedFeaturesTests(unittest.TestCase):
    def test_builds_complete_snapshot_features(self):
        s = setup()
        primary = pattern_candles()
        rows_1m = [
            candle(s.d_ms + index * 60_000, 102 + (index % 2), 104 + (index % 3), 100 - (index % 2), 103)
            for index in range(30)
        ]
        rows_5m = [
            candle(s.d_ms + index * 300_000, 102, 104, 100, 103)
            for index in range(12)
        ]
        rows_1h = primary
        derived = build_derived_features(
            s,
            primary_candles=primary,
            candles_1h=rows_1h,
            candles_5m=rows_5m,
            candles_1m=rows_1m,
            live_price=103.0,
            live_price_ms=s.d_ms + 30 * 60_000,
            now_ms=s.d_ms + 31 * 60_000,
            recent_trades=[
                {"price": 103.0, "qty": 2.0, "buyer_is_maker": False, "time": s.d_ms},
                {"price": 102.9, "qty": 1.0, "buyer_is_maker": True, "time": s.d_ms + 1000},
            ],
        )
        self.assertEqual(derived["setup_id"], s.setup_id)
        self.assertEqual(derived["setup"]["symbol"], "BTCUSDT")
        self.assertEqual(derived["xabcd"]["points"]["D"]["swing_price"], 101.0)
        self.assertEqual(derived["historical_touches"]["status"], "ACTIVE_WATCH")
        self.assertIn("1h", derived["multi_timeframe"])
        self.assertIn("5m", derived["multi_timeframe"])
        self.assertIn("1m", derived["multi_timeframe"])
        self.assertTrue(derived["data_quality"]["primary_bars"] > 0)
        self.assertAlmostEqual(derived["recent_live_flow"]["taker_buy_ratio"], 2 / 3, places=5)


if __name__ == "__main__":
    unittest.main()
