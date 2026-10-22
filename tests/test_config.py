import unittest
from datetime import datetime

from run_monitor import CFG, ENTRY, SL, TG1


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ConfigTests(unittest.TestCase):
    def test_fixed_levels_are_ordered(self):
        self.assertLess(SL, ENTRY)
        self.assertLess(ENTRY, TG1)

    def test_setup_timestamps_are_chronological(self):
        stamps = [
            parse(CFG["setup_x_timestamp_utc"]),
            parse(CFG["setup_a_timestamp_utc"]),
            parse(CFG["setup_b_timestamp_utc"]),
            parse(CFG["setup_c_timestamp_utc"]),
            parse(CFG["setup_d_timestamp_utc"]),
        ]
        self.assertEqual(stamps, sorted(stamps))

    def test_order_price_matches_entry(self):
        self.assertEqual(float(CFG["order_price"]), ENTRY)
        self.assertEqual(float(CFG["order_stop_price"]), ENTRY)


if __name__ == "__main__":
    unittest.main()
