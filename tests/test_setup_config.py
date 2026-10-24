import unittest

from setup_config import (
    SetupValidationError,
    normalize_setup_payload,
    offset_label,
    parse_offset_minutes,
    setup_from_config,
)


def base_payload(**overrides):
    payload = {
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "btcusdt",
        "direction": "long",
        "main_interval": "1h",
        "timezone_offset_minutes": "+02:00",
        "setup_x_local": "2026-10-20T15:00",
        "setup_a_local": "2026-10-20T16:00",
        "setup_b_local": "2026-10-20T21:00",
        "setup_c_local": "2026-10-21T18:00",
        "setup_d_local": "2026-10-22T10:00",
        "entry": 82147,
        "tg1": 83731,
        "sl": 80261,
    }
    payload.update(overrides)
    return payload


def short_payload(**overrides):
    payload = base_payload(direction="short", entry=82147, tg1=80261, sl=83731)
    payload.update(overrides)
    return payload


class OffsetParsingTests(unittest.TestCase):
    def test_accepts_common_offset_formats(self):
        self.assertEqual(parse_offset_minutes(120), 120)
        self.assertEqual(parse_offset_minutes("+02:00"), 120)
        self.assertEqual(parse_offset_minutes("UTC+2"), 120)
        self.assertEqual(parse_offset_minutes("-0530"), -330)
        self.assertEqual(parse_offset_minutes("UTC-03:15"), -195)

    def test_rejects_out_of_range_and_boolean_offsets(self):
        with self.assertRaises(SetupValidationError):
            parse_offset_minutes("+15:00")
        with self.assertRaises(SetupValidationError):
            parse_offset_minutes(True)
        with self.assertRaises(SetupValidationError):
            parse_offset_minutes("nepoznato")

    def test_label(self):
        self.assertEqual(offset_label(120), "UTC+02:00")
        self.assertEqual(offset_label(-330), "UTC-05:30")
        self.assertEqual(offset_label(0), "UTC+00:00")


class SetupNormalizationTests(unittest.TestCase):
    def test_local_times_are_normalized_to_utc_and_setup_id_is_deterministic(self):
        setup = normalize_setup_payload(base_payload())
        self.assertEqual(setup.symbol, "BTCUSDT")
        self.assertEqual(setup.timezone_offset_minutes, 120)
        self.assertEqual(setup.local_times()["x"], "2026-10-20T15:00+02:00")
        self.assertEqual(setup.local_inputs()["d"], "2026-10-22T10:00")
        self.assertEqual(setup.public()["X"], "2026-10-20T13:00:00+00:00")
        self.assertEqual(setup.public()["D"], "2026-10-22T08:00:00+00:00")
        self.assertTrue(setup.setup_id.startswith("btcusdt-long-"))
        self.assertEqual(len(setup.setup_id.rsplit("-", 1)[1]), 12)
        self.assertEqual(setup.setup_id, normalize_setup_payload(base_payload()).setup_id)

    def test_setup_id_changes_when_a_material_field_changes(self):
        original = normalize_setup_payload(base_payload())
        changed_level = normalize_setup_payload(base_payload(entry=82148))
        changed_time = normalize_setup_payload(base_payload(setup_d_local="2026-10-22T11:00"))
        self.assertNotEqual(original.setup_id, changed_level.setup_id)
        self.assertNotEqual(original.setup_id, changed_time.setup_id)

    def test_long_and_short_level_ordering(self):
        long_setup = normalize_setup_payload(base_payload())
        short_setup = normalize_setup_payload(short_payload())
        self.assertLess(long_setup.sl, long_setup.entry)
        self.assertLess(long_setup.entry, long_setup.tg1)
        self.assertLess(short_setup.tg1, short_setup.entry)
        self.assertLess(short_setup.entry, short_setup.sl)
        self.assertEqual(long_setup.order_details()["side"], "BUY")
        self.assertEqual(short_setup.order_details()["side"], "SELL")
        self.assertEqual(long_setup.order_details()["stopPrice"], long_setup.entry)
        self.assertEqual(long_setup.order_details()["price"], long_setup.entry)

    def test_rejects_wrong_level_order_for_direction(self):
        with self.assertRaises(SetupValidationError) as long_ctx:
            normalize_setup_payload(base_payload(sl=83000))
        self.assertIn("SL < ENTRY < TG1", " ".join(long_ctx.exception.errors))
        with self.assertRaises(SetupValidationError) as short_ctx:
            normalize_setup_payload(short_payload(sl=80000))
        self.assertIn("TG1 < ENTRY < SL", " ".join(short_ctx.exception.errors))

    def test_rejects_chronology_timezone_and_unknown_interval(self):
        with self.assertRaises(SetupValidationError) as ctx:
            normalize_setup_payload(base_payload(setup_b_local="2026-10-20T14:30"))
        self.assertIn("X < A < B < C < D", " ".join(ctx.exception.errors))
        with self.assertRaises(SetupValidationError) as tz_ctx:
            normalize_setup_payload(base_payload(timezone_offset_minutes=None))
        self.assertIn("vremenska zona", " ".join(tz_ctx.exception.errors).lower())
        with self.assertRaises(SetupValidationError) as interval_ctx:
            normalize_setup_payload(base_payload(main_interval="2h"))
        self.assertIn("15m", " ".join(interval_ctx.exception.errors))
        with self.assertRaises(SetupValidationError) as symbol_ctx:
            normalize_setup_payload(base_payload(symbol="BTC USDT"))
        self.assertIn("Simbol", " ".join(symbol_ctx.exception.errors))

    def test_accepts_utc_timestamps_without_timezone_conversion(self):
        payload = {
            "exchange": "binance",
            "market_type": "futures",
            "symbol": "ETHUSDT",
            "direction": "short",
            "main_interval": "4h",
            "timezone_offset_minutes": "UTC-05:00",
            "setup_x_timestamp_utc": "2026-10-20T13:00:00Z",
            "setup_a_timestamp_utc": "2026-10-20T14:00:00Z",
            "setup_b_timestamp_utc": "2026-10-20T19:00:00Z",
            "setup_c_timestamp_utc": "2026-10-21T16:00:00Z",
            "setup_d_timestamp_utc": "2026-10-22T08:00:00Z",
            "entry": 3000,
            "tg1": 2800,
            "sl": 3200,
        }
        setup = normalize_setup_payload(payload)
        self.assertEqual(setup.market_type, "futures")
        self.assertEqual(setup.main_interval, "4h")
        self.assertEqual(setup.public()["X"], "2026-10-20T13:00:00+00:00")
        self.assertEqual(setup.local_times()["x"], "2026-10-20T08:00-05:00")

    def test_setup_from_config_preserves_confirmation(self):
        setup = normalize_setup_payload(base_payload())
        config = setup.to_config({"auto_trade": True, "time_in_force": "GTC"})
        loaded = setup_from_config(config)
        self.assertEqual(loaded.setup_id, setup.setup_id)
        self.assertEqual(loaded.confirmed_at_utc, setup.confirmed_at_utc)
        self.assertEqual(loaded.timezone_label, "UTC+02:00")


class DirectionalHelperTests(unittest.TestCase):
    def test_long_helpers(self):
        setup = normalize_setup_payload(base_payload())
        self.assertTrue(setup.in_pre_entry_zone(81000))
        self.assertFalse(setup.in_pre_entry_zone(setup.entry))
        self.assertFalse(setup.in_pre_entry_zone(82500))
        self.assertTrue(setup.past_entry_before_target(82500))
        self.assertFalse(setup.past_entry_before_target(setup.tg1))
        self.assertTrue(setup.target_hit(setup.tg1))
        self.assertTrue(setup.stop_hit(setup.sl))
        self.assertTrue(setup.entry_crossed(82000, 82200))
        self.assertFalse(setup.entry_crossed(82200, 82000))
        candle = {"open": 81000, "high": 82200, "low": 80900, "close": 82000}
        self.assertTrue(setup.entry_touched(candle))
        self.assertFalse(setup.target_touched(candle))
        self.assertFalse(setup.stop_touched(candle))

    def test_short_helpers(self):
        setup = normalize_setup_payload(short_payload())
        self.assertTrue(setup.in_pre_entry_zone(83000))
        self.assertFalse(setup.in_pre_entry_zone(setup.entry))
        self.assertFalse(setup.in_pre_entry_zone(81000))
        self.assertTrue(setup.past_entry_before_target(81000))
        self.assertTrue(setup.target_hit(setup.tg1))
        self.assertTrue(setup.stop_hit(setup.sl))
        self.assertTrue(setup.entry_crossed(83000, 82000))
        candle = {"open": 83000, "high": 83100, "low": 82000, "close": 82500}
        self.assertTrue(setup.entry_touched(candle))
        self.assertFalse(setup.target_touched(candle))
        self.assertFalse(setup.stop_touched(candle))
        self.assertEqual(setup.trigger_description, "padaju\u0107i prelazak kroz ENTRY odozgo")

    def test_terminal_candle_classification_uses_open_side(self):
        setup = normalize_setup_payload(base_payload())
        self.assertEqual(
            setup.classify_terminal_candle({"open": setup.sl - 1, "high": setup.sl + 1, "low": setup.sl - 2, "close": setup.sl + 1}),
            "STOP",
        )
        self.assertEqual(
            setup.classify_terminal_candle({"open": setup.tg1 + 1, "high": setup.tg1 + 2, "low": setup.tg1 - 1, "close": setup.tg1}),
            "TARGET",
        )
        self.assertEqual(
            setup.classify_terminal_candle({"open": setup.entry, "high": setup.tg1 + 1, "low": setup.sl - 1, "close": setup.entry}),
            "AMBIGUOUS",
        )


if __name__ == "__main__":
    unittest.main()
