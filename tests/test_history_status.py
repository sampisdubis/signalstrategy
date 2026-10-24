import unittest
from datetime import datetime, timezone

from analysis_features import historical_touches
from setup_config import normalize_setup_payload


def setup(direction="long"):
    payload = {
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "direction": direction,
        "main_interval": "1h",
        "timezone_offset_minutes": 0,
        "setup_x_timestamp_utc": "2026-10-20T13:00:00Z",
        "setup_a_timestamp_utc": "2026-10-20T14:00:00Z",
        "setup_b_timestamp_utc": "2026-10-20T19:00:00Z",
        "setup_c_timestamp_utc": "2026-10-21T16:00:00Z",
        "setup_d_timestamp_utc": "2026-10-22T08:00:00Z",
        "entry": 82147,
        "tg1": 83731,
        "sl": 80261,
    }
    if direction == "short":
        payload.update({"entry": 82147, "tg1": 80261, "sl": 83731})
    return normalize_setup_payload(payload)


def bar(setup_obj, minutes, *, open_price, high, low, close=None):
    open_ms = setup_obj.d_ms + minutes * 60_000
    return {
        "open_time_ms": open_ms,
        "open_time_utc": datetime.fromtimestamp(open_ms / 1000, timezone.utc).isoformat(),
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close if close is not None else open_price),
        "volume": 1.0,
        "closed": True,
    }


class HistoryStatusTests(unittest.TestCase):
    def test_open_above_entry_does_not_mean_entry_missed(self):
        s = setup()
        bars = [bar(s, 0, open_price=82239, high=82300, low=82000, close=82100)]
        result = historical_touches(s, bars)
        self.assertEqual(result["status"], "ACTIVE_WATCH")
        self.assertTrue(result["entry"]["touched"])
        self.assertIn("ENTRY", result["reason"])

    def test_entry_cross_then_return_to_zone_remains_active(self):
        s = setup()
        bars = [
            bar(s, 0, open_price=81500, high=82250, low=81300, close=82100),
            bar(s, 1, open_price=82100, high=82500, low=81400, close=81600),
            bar(s, 2, open_price=81600, high=81800, low=81450, close=81476),
        ]
        result = historical_touches(s, bars)
        self.assertEqual(result["status"], "ACTIVE_WATCH")
        self.assertTrue(result["entry"]["touched"])
        self.assertTrue(result["entry_touched_before_terminal"])

    def test_tg1_touch_terminates_current_setup(self):
        s = setup()
        bars = [
            bar(s, 0, open_price=81500, high=82000, low=81400),
            bar(s, 1, open_price=82000, high=s.tg1, low=81900),
            bar(s, 2, open_price=81700, high=81800, low=81400),
        ]
        result = historical_touches(s, bars)
        self.assertEqual(result["status"], "TARGET_REACHED_BEFORE_ENTRY")
        self.assertIn("TG1", result["reason"])

    def test_sl_touch_invalidates_setup_even_if_price_recovers(self):
        s = setup()
        bars = [
            bar(s, 0, open_price=81500, high=82000, low=s.sl),
            bar(s, 1, open_price=81000, high=81800, low=80900),
        ]
        result = historical_touches(s, bars)
        self.assertEqual(result["status"], "SETUP_INVALIDATED")
        self.assertIn("SL", result["reason"])

    def test_same_minute_touch_of_tg1_and_sl_is_ambiguous_and_not_active(self):
        s = setup()
        result = historical_touches(s, [bar(s, 0, open_price=82100, high=s.tg1 + 1, low=s.sl - 1)])
        self.assertEqual(result["status"], "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS")
        self.assertIn("redosled", result["reason_short"].lower())

    def test_no_history_is_invalid(self):
        result = historical_touches(setup(), [])
        self.assertEqual(result["status"], "DATA_INVALID")
        self.assertIn("1m", result["reason"])

    def test_short_setup_target_and_stop_direction(self):
        s = setup("short")
        target = historical_touches(s, [bar(s, 0, open_price=s.tg1 + 100, high=s.tg1 + 200, low=s.tg1 - 1)])
        self.assertEqual(target["status"], "TARGET_REACHED_BEFORE_ENTRY")
        stop = historical_touches(s, [bar(s, 0, open_price=s.sl - 100, high=s.sl + 1, low=s.sl - 200)])
        self.assertEqual(stop["status"], "SETUP_INVALIDATED")

    def test_entry_touch_after_prior_touch_is_not_terminal(self):
        s = setup()
        result = historical_touches(
            s,
            [
                bar(s, 0, open_price=82100, high=82200, low=82000),
                bar(s, 1, open_price=82100, high=82300, low=82050),
            ],
        )
        self.assertEqual(result["status"], "ACTIVE_WATCH")
        self.assertEqual(result["entry"]["touch_count"], 2)
        self.assertEqual(result["entry_touched_before_terminal"], True)


if __name__ == "__main__":
    unittest.main()
