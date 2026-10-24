import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from run_monitor import EntryMonitor, exchange_config, extract_json_from_agent_output
from setup_config import normalize_setup_payload, utc_iso


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


def candle(open_ms, opened, high, low, close, volume=10.0):
    return {
        "open_time_ms": int(open_ms),
        "open_time_utc": utc_iso(open_ms),
        "open": float(opened),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "quote_volume": float(volume) * float(close),
        "trades": 10,
        "taker_buy_base": float(volume) * 0.5,
        "taker_buy_quote": float(volume) * 0.5 * float(close),
        "closed": True,
    }


class MonitorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.runtime = self.root / "runtime"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        s = setup()
        monitor = EntryMonitor(s, {}, runtime=self.runtime, config_path=self.config_path)
        base = s.x_ms - 2 * 3_600_000
        monitor.primary = [
            candle(base, 106, 107, 105, 106),
            candle(base + 3_600_000, 106, 107, 100, 106),
            candle(s.x_ms, 106, 107, 100, 106),
            candle(s.a_ms, 106, 110, 105, 109),
            candle(s.b_ms, 109, 110, 104, 105),
            candle(s.c_ms, 105, 108, 104, 107),
            candle(s.d_ms, 107, 108, 101, 102),
            candle(s.d_ms + 3_600_000, 102, 104, 100, 103),
        ]
        monitor.k1h = monitor.primary
        monitor.k5 = [
            candle(s.d_ms + index * 300_000, 102, 104, 100, 103)
            for index in range(12)
        ]
        monitor.k1m = [
            candle(s.d_ms + index * 60_000, 102 + index % 2, 104, 100, 103)
            for index in range(30)
        ]
        monitor.history_minutes = list(monitor.k1m)
        monitor.price = 103.0
        monitor.price_event_ms = int(time.time() * 1000) - 5_000
        monitor.last_market_msg_monotonic = time.monotonic()
        monitor.last_price_samples.append((time.time(), 102.0))
        return s, monitor

    def test_snapshot_contains_v2_sections_and_compatible_candles(self):
        s, monitor = self.build()
        snapshot = monitor.snapshot("unit_test")
        self.assertEqual(snapshot["snapshot_version"], 2)
        self.assertEqual(snapshot["setup_id"], s.setup_id)
        self.assertEqual(snapshot["fixed_levels"], {"ENTRY": 105.0, "TG1": 115.0, "SL": 95.0})
        self.assertEqual(snapshot["setup"]["order"]["side"], "BUY")
        self.assertEqual(snapshot["setup"]["order"]["stopPrice"], 105.0)
        self.assertIn("context_before_x", snapshot["history"])
        self.assertIn("pattern_x_to_d", snapshot["history"])
        self.assertIn("post_d", snapshot["history"])
        self.assertIn("current_live", snapshot["history"])
        self.assertEqual(snapshot["history"]["pattern_x_to_d"]["timeframe"], "1h")
        self.assertEqual(snapshot["derived_features"]["setup_id"], s.setup_id)
        self.assertEqual(snapshot["derived_features"]["xabcd"]["points"]["D"]["swing_price"], 101.0)
        self.assertEqual(snapshot["setup_status"]["status"], "ACTIVE_WATCH")
        self.assertTrue(snapshot["candles_1h"])
        self.assertTrue(snapshot["candles_5m"])
        self.assertTrue(snapshot["candles_1m"])
        self.assertGreaterEqual(snapshot["price_age_seconds"], 5.0)
        self.assertLess(snapshot["price_age_seconds"], 15.0)

    def test_pattern_context_starts_before_x_for_old_setup(self):
        s, monitor = self.build()
        calls = []

        def fake_fetch(interval, start, end):
            calls.append((interval, start, end))
            return []

        monitor.fetch_klines_since = fake_fetch
        monitor.load_pattern_context(s.x_ms + 2000 * 3_600_000)
        self.assertLessEqual(calls[0][1], s.x_ms)

    def test_snapshot_uses_full_history_for_terminal_touches(self):
        s, monitor = self.build()
        target_ms = s.d_ms + 300 * 60_000
        monitor.history_minutes.append(candle(target_ms, 114, 116, 113, 115))
        monitor.k1m = monitor.history_minutes[-2:]
        snapshot = monitor.snapshot("unit_test")
        self.assertEqual(snapshot["setup_status"]["status"], "TARGET_REACHED_BEFORE_ENTRY")
        self.assertTrue(snapshot["tg1_touched_since_D"])

    def test_direction_and_market_endpoints(self):
        s = setup(direction="short", entry=105, tg1=95, sl=115)
        self.assertEqual(exchange_config(s)["rest"], "https://api.binance.com")
        futures = setup(market_type="futures")
        self.assertEqual(exchange_config(futures)["rest"], "https://fapi.binance.com")
        monitor = EntryMonitor(s, {}, runtime=self.runtime, config_path=self.config_path)
        self.assertEqual(monitor.setup.order_details()["side"], "SELL")

    def test_armed_history_target_maps_to_check_order(self):
        s, monitor = self.build()
        monitor.state["entry_order_armed"] = True
        status, reason = monitor.resolved_history_status("TARGET_REACHED_BEFORE_ENTRY", "TG1")
        self.assertEqual(status, "TARGET_REACHED_CHECK_ORDER")
        self.assertIn("berzi", reason)
        status, reason = monitor.resolved_history_status("SETUP_INVALIDATED", "SL")
        self.assertEqual(status, "SETUP_INVALIDATED")
        self.assertIn("otkazi", reason)

    def test_history_status_uses_setup_state(self):
        s, monitor = self.build()
        status, reason = monitor.history_status(monitor.k1m)
        self.assertEqual(status, "ACTIVE_WATCH")
        self.assertIn("TG1", reason)
        self.assertEqual(monitor.history_status([])[0], "DATA_INVALID")


class AgentOutputTests(unittest.TestCase):
    def test_extracts_plain_json_and_fenced_json(self):
        payload = '{"decision": "WAIT_FOR_CONFIRMATION", "setup_id": "x"}'
        self.assertEqual(extract_json_from_agent_output(payload)["decision"], "WAIT_FOR_CONFIRMATION")
        fenced = "Evo rezultata:\n```json\n" + payload + "\n```\nKraj."
        self.assertEqual(extract_json_from_agent_output(fenced)["decision"], "WAIT_FOR_CONFIRMATION")

    def test_prefers_decision_object_from_event_stream(self):
        stream = (
            '{"type":"text","part":{"text":"razmisljanje"}}\n'
            '{"type":"text","text":"{\\"decision\\": \\"SETUP_INVALIDATED\\"}"}\n'
        )
        self.assertEqual(extract_json_from_agent_output(stream)["decision"], "SETUP_INVALIDATED")

    def test_rejects_output_without_json(self):
        with self.assertRaises(ValueError):
            extract_json_from_agent_output("nema odluke")


if __name__ == "__main__":
    unittest.main()
