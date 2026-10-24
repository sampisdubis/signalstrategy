import unittest

from setup_store import load_raw_config, load_setup


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load_raw_config()
        cls.setup = load_setup()

    def test_confirmed_setup_is_valid_and_configured(self):
        self.assertEqual(self.setup.exchange, "binance")
        self.assertIn(self.setup.market_type, ("spot", "futures"))
        self.assertIn(self.setup.direction, ("long", "short"))
        self.assertIn(self.setup.main_interval, ("15m", "30m", "1h", "4h"))
        self.assertTrue(self.setup.symbol)
        self.assertEqual(self.raw["setup_id"], self.setup.setup_id)
        self.assertFalse(self.raw.get("auto_trade"))

    def test_levels_match_direction(self):
        if self.setup.direction == "long":
            self.assertLess(self.setup.sl, self.setup.entry)
            self.assertLess(self.setup.entry, self.setup.tg1)
        else:
            self.assertLess(self.setup.tg1, self.setup.entry)
            self.assertLess(self.setup.entry, self.setup.sl)
        order = self.setup.order_details()
        self.assertEqual(order["stopPrice"], self.setup.entry)
        self.assertEqual(order["price"], self.setup.entry)
        self.assertEqual(order["side"], "BUY" if self.setup.direction == "long" else "SELL")

    def test_setup_timestamps_are_chronological(self):
        stamps = [self.setup.x_ms, self.setup.a_ms, self.setup.b_ms, self.setup.c_ms, self.setup.d_ms]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 5)

    def test_timezone_roundtrip(self):
        imported = {
            key: value
            for key, value in self.setup.local_inputs().items()
        }
        rebuilt = type(self.setup)(
            exchange=self.setup.exchange,
            market_type=self.setup.market_type,
            symbol=self.setup.symbol,
            direction=self.setup.direction,
            main_interval=self.setup.main_interval,
            timezone_offset_minutes=self.setup.timezone_offset_minutes,
            entry=self.setup.entry,
            tg1=self.setup.tg1,
            sl=self.setup.sl,
            **{f"{key}_ms": self.setup.timestamps_ms[key] for key in ("x", "a", "b", "c", "d")},
        )
        self.assertEqual(rebuilt.setup_id, self.setup.setup_id)
        self.assertEqual(rebuilt.local_inputs(), imported)
        self.assertEqual(rebuilt.timezone_label, self.setup.timezone_label)


if __name__ == "__main__":
    unittest.main()
