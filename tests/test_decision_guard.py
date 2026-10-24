import unittest

from decision_guard import guard_ai_result
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
        "entry": 100,
        "tg1": 110,
        "sl": 90,
    }
    if direction == "short":
        payload.update({"entry": 100, "tg1": 90, "sl": 110})
    return normalize_setup_payload(payload)


def result(s, decision="PLACE_ENTRY_ORDER_NOW", **overrides):
    payload = {
        "decision": decision,
        "setup_id": s.setup_id,
        "confidence": "medium",
        "reason": "Test razlog.",
        "supporting_evidence": ["dokaz"],
        "contradicting_evidence": [],
        "entry_conditions_passed": ["uslov"],
        "entry_conditions_missing": [],
        "tg1_vs_sl_assessment": {"tg1_likelihood": "moderate", "sl_likelihood": "moderate", "summary": "test"},
        "next_trigger": "sledeci dogadjaj",
        "data_timestamp_utc": "2026-10-23T00:00:00+00:00",
        "order": {"type": "STOP_LOSS_LIMIT", "side": "BUY", "stopPrice": 1, "price": 1, "timeInForce": "GTC"},
    }
    payload.update(overrides)
    return payload


def guard(s, ai, **kwargs):
    params = {
        "live_price": 95.0,
        "live_price_age_seconds": 1.0,
        "max_data_age_seconds": 15.0,
        "snapshot_price": 95.0,
        "snapshot_time_utc": "2026-10-23T00:00:00+00:00",
        "target_touched_since_d": False,
        "stop_touched_since_d": False,
    }
    params.update(kwargs)
    return guard_ai_result(ai, setup=s, **params)


class DecisionGuardTests(unittest.TestCase):
    def test_fresh_place_is_kept_and_order_is_canonical(self):
        s = setup()
        ai = result(s, order={"type": "WRONG", "side": "WRONG", "stopPrice": 1})
        guarded = guard(s, ai)
        self.assertEqual(guarded["decision"], "PLACE_ENTRY_ORDER_NOW")
        self.assertEqual(guarded["order"], s.order_details())
        self.assertFalse(guarded["stale_guard"]["changed"])

    def test_not_place_forces_null_order(self):
        s = setup()
        guarded = guard(s, result(s, decision="WAIT_FOR_CONFIRMATION"), live_price=95.0)
        self.assertEqual(guarded["decision"], "WAIT_FOR_CONFIRMATION")
        self.assertIsNone(guarded["order"])

    def test_setup_id_mismatch_is_rejected(self):
        s = setup()
        guarded = guard(s, result(s, setup_id="btcusdt-long-drugi"))
        self.assertEqual(guarded["decision"], "DATA_INVALID")
        self.assertTrue(guarded["stale_guard"]["changed"])

    def test_unknown_decision_is_rejected(self):
        s = setup()
        guarded = guard(s, result(s, decision="ENTRY_MISSED"))
        self.assertEqual(guarded["decision"], "DATA_INVALID")

    def test_place_after_entry_is_downgraded_to_wait_for_zone(self):
        s = setup()
        guarded = guard(s, result(s), live_price=105.0, snapshot_price=105.0)
        self.assertEqual(guarded["decision"], "WAIT_FOR_ENTRY_ZONE")
        self.assertIsNone(guarded["order"])

    def test_place_below_stop_is_terminal_override(self):
        s = setup()
        guarded = guard(s, result(s), live_price=89.0, snapshot_price=89.0)
        self.assertEqual(guarded["decision"], "SETUP_INVALIDATED")

    def test_place_above_target_is_target_reached(self):
        s = setup()
        guarded = guard(s, result(s), live_price=110.0, snapshot_price=110.0)
        self.assertEqual(guarded["decision"], "TARGET_REACHED_BEFORE_ENTRY")

    def test_target_with_armed_order_asks_to_check_order(self):
        s = setup()
        guarded = guard(s, result(s), live_price=110.0, snapshot_price=110.0, order_armed=True)
        self.assertEqual(guarded["decision"], "TARGET_REACHED_CHECK_ORDER")

    def test_place_with_armed_order_becomes_armed(self):
        s = setup()
        guarded = guard(s, result(s), order_armed=True)
        self.assertEqual(guarded["decision"], "ENTRY_ORDER_ARMED")
        self.assertIsNone(guarded["order"])

    def test_stale_market_data_is_rejected(self):
        s = setup()
        guarded = guard(s, result(s), live_price_age_seconds=16.0)
        self.assertEqual(guarded["decision"], "DATA_INVALID")

    def test_price_moved_after_snapshot_downgrades_place(self):
        s = setup()
        guarded = guard(s, result(s), live_price=96.0, snapshot_price=95.0)
        self.assertEqual(guarded["decision"], "WAIT_FOR_CONFIRMATION")
        self.assertTrue(guarded["stale_guard"]["changed"])
        self.assertIn("original_ai_decision", guarded)

    def test_terminal_state_overrides_ai(self):
        s = setup()
        guarded = guard(s, result(s), terminal_state="SETUP_INVALIDATED", terminal_reason="SL je dostignut.")
        self.assertEqual(guarded["decision"], "SETUP_INVALIDATED")
        self.assertEqual(guarded["reason"], "SL je dostignut.")

    def test_ambiguous_terminal_state_maps_to_data_invalid(self):
        s = setup()
        guarded = guard(s, result(s), terminal_state="OUTCOME_LEVELS_TOUCHED_AMBIGUOUS")
        self.assertEqual(guarded["decision"], "DATA_INVALID")

    def test_unsubstantiated_target_claim_is_reverted(self):
        s = setup()
        guarded = guard(
            s,
            result(s, decision="TARGET_REACHED_BEFORE_ENTRY"),
            target_touched_since_d=False,
        )
        self.assertEqual(guarded["decision"], "WAIT_FOR_CONFIRMATION")
        self.assertEqual(guarded["original_ai_decision"], "TARGET_REACHED_BEFORE_ENTRY")

    def test_short_setup_place_and_zone(self):
        s = setup("short")
        fresh = guard(s, result(s), live_price=105.0, snapshot_price=105.0)
        self.assertEqual(fresh["decision"], "PLACE_ENTRY_ORDER_NOW")
        self.assertEqual(fresh["order"]["side"], "SELL")
        stale = guard(s, result(s), live_price=98.0, snapshot_price=98.0)
        self.assertEqual(stale["decision"], "WAIT_FOR_ENTRY_ZONE")

    def test_missing_result_is_data_invalid(self):
        s = setup()
        guarded = guard(s, None)
        self.assertEqual(guarded["decision"], "DATA_INVALID")
        self.assertIsNone(guarded["order"])


if __name__ == "__main__":
    unittest.main()
