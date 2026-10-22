import unittest
from datetime import datetime, timezone

from run_monitor import ENTRY, SL, TG1, SETUP_D_MS, history_status


def bar(minutes, *, open_price, high, low, close=None):
    open_ms = SETUP_D_MS + minutes * 60_000
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
        bars = [bar(0, open_price=82239, high=82300, low=82000, close=82100)]
        state, reason = history_status(bars)
        self.assertEqual(state, "ACTIVE_WATCH")
        self.assertIn("ENTRY", reason)

    def test_entry_cross_then_return_to_zone_remains_active(self):
        bars = [
            bar(0, open_price=81500, high=82250, low=81300, close=82100),
            bar(1, open_price=82100, high=82500, low=81400, close=81600),
            bar(2, open_price=81600, high=81800, low=81450, close=81476),
        ]
        state, _ = history_status(bars)
        self.assertEqual(state, "ACTIVE_WATCH")

    def test_tg1_touch_terminates_current_setup(self):
        bars = [
            bar(0, open_price=81500, high=82000, low=81400),
            bar(1, open_price=82000, high=TG1, low=81900),
            bar(2, open_price=81700, high=81800, low=81400),
        ]
        state, reason = history_status(bars)
        self.assertEqual(state, "TARGET_REACHED_BEFORE_ENTRY")
        self.assertIn("TG1", reason)

    def test_sl_touch_invalidates_setup_even_if_price_recovers(self):
        bars = [
            bar(0, open_price=81500, high=82000, low=SL),
            bar(1, open_price=81000, high=81800, low=80900),
        ]
        state, reason = history_status(bars)
        self.assertEqual(state, "SETUP_INVALIDATED")
        self.assertIn("SL", reason)

    def test_same_minute_touch_of_tg1_and_sl_is_ambiguous_and_not_active(self):
        bars = [bar(0, open_price=82100, high=TG1 + 1, low=SL - 1)]
        state, reason = history_status(bars)
        self.assertEqual(state, "OUTCOME_LEVELS_TOUCHED_AMBIGUOUS")
        self.assertIn("redosledom", reason)

    def test_no_history_is_invalid(self):
        state, _ = history_status([])
        self.assertEqual(state, "DATA_INVALID")


if __name__ == "__main__":
    unittest.main()
