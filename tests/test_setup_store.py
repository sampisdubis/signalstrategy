import json
import tempfile
import unittest
from pathlib import Path

from setup_config import normalize_setup_payload
from setup_store import (
    append_event,
    archive_setup_artifacts,
    config_file_changed,
    initialize_state_for_setup,
    load_raw_config,
    load_setup,
    load_state_for_setup,
    read_armed_flag,
    read_json,
    read_state_raw,
    save_setup_payload,
    write_armed_flag,
)


def payload(entry=82147, tg1=83731, sl=80261, direction="long"):
    data = {
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "direction": direction,
        "main_interval": "1h",
        "timezone_offset_minutes": 120,
        "setup_x_timestamp_utc": "2026-10-20T13:00:00Z",
        "setup_a_timestamp_utc": "2026-10-20T14:00:00Z",
        "setup_b_timestamp_utc": "2026-10-20T19:00:00Z",
        "setup_c_timestamp_utc": "2026-10-21T16:00:00Z",
        "setup_d_timestamp_utc": "2026-10-22T08:00:00Z",
        "entry": entry,
        "tg1": tg1,
        "sl": sl,
    }
    return data


def event_types(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rows.append(json.loads(line))
    return [row["type"] for row in rows]


class SetupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.runtime = self.root / "runtime"

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, **overrides):
        return save_setup_payload(
            payload(**overrides),
            config_path=self.config_path,
            runtime=self.runtime,
        )

    def test_save_persists_config_and_initializes_state(self):
        setup, config = self.save()
        self.assertEqual(config["setup_id"], setup.setup_id)
        self.assertFalse(config["auto_trade"])
        self.assertEqual(config["timezone_offset_minutes"], 120)
        raw = load_raw_config(self.config_path)
        self.assertEqual(load_setup(self.config_path).setup_id, setup.setup_id)
        state = read_state_raw(runtime=self.runtime)
        self.assertEqual(state["setup_id"], setup.setup_id)
        self.assertEqual(state["decision_state"], "BOOTSTRAPPING")
        self.assertEqual(event_types(self.runtime / "events.jsonl")[-1], "setup_confirmed")
        self.assertFalse((self.config_path.parent / "config.json.tmp").exists())

    def test_replacing_setup_archives_old_state_but_preserves_events(self):
        first, _ = self.save()
        write_armed_flag(first.setup_id, runtime=self.runtime)
        first_events = (self.runtime / "events.jsonl").read_text(encoding="utf-8").splitlines()
        second, _ = self.save(entry=82148)
        self.assertNotEqual(first.setup_id, second.setup_id)
        archives = list((self.runtime / "archive").iterdir())
        self.assertTrue(archives)
        archived_names = {path.name for path in archives[0].iterdir()}
        self.assertIn("state.json", archived_names)
        self.assertIn("entry_order_armed.flag", archived_names)
        self.assertEqual(read_state_raw(runtime=self.runtime)["setup_id"], second.setup_id)
        exists, content = read_armed_flag(runtime=self.runtime)
        self.assertFalse(exists)
        self.assertEqual(content, "")
        all_lines = (self.runtime / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(all_lines[: len(first_events)], first_events)
        types = event_types(self.runtime / "events.jsonl")
        self.assertIn("setup_confirmed", types)
        self.assertIn("setup_replaced", types)

    def test_loading_state_for_another_setup_archives_and_reinitializes(self):
        first, _ = self.save()
        second = normalize_setup_payload(payload(entry=82148))
        state = load_state_for_setup(second, runtime=self.runtime)
        self.assertEqual(state["setup_id"], second.setup_id)
        self.assertEqual(state["decision_state"], "BOOTSTRAPPING")
        archives = list((self.runtime / "archive").iterdir())
        self.assertTrue(archives)
        self.assertIn("state.json", {path.name for path in archives[0].iterdir()})
        self.assertNotEqual(first.setup_id, second.setup_id)

    def test_config_file_change_is_detected(self):
        setup, _ = self.save()
        self.assertFalse(config_file_changed(setup, self.config_path))
        config = load_raw_config(self.config_path)
        config["entry"] = 82148
        config["tg1"] = 83732
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertTrue(config_file_changed(setup, self.config_path))

    def test_invalid_config_is_detected_as_changed(self):
        setup, _ = self.save()
        self.config_path.write_text("{nije json", encoding="utf-8")
        self.assertTrue(config_file_changed(setup, self.config_path))

    def test_events_are_append_only(self):
        self.save()
        before = (self.runtime / "events.jsonl").read_text(encoding="utf-8")
        append_event({"type": "custom", "setup_id": "x"}, runtime=self.runtime)
        after = (self.runtime / "events.jsonl").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertEqual(event_types(self.runtime / "events.jsonl")[-1], "custom")

    def test_resaving_same_setup_clears_old_order_flag_and_snapshot(self):
        setup, _ = self.save()
        write_armed_flag(setup.setup_id, runtime=self.runtime)
        (self.runtime / "ai_snapshot.json").write_text(json.dumps({"setup_id": setup.setup_id}), encoding="utf-8")
        self.save()
        self.assertFalse(read_armed_flag(runtime=self.runtime)[0])
        self.assertEqual(read_json(self.runtime / "ai_snapshot.json", {}), {})
        self.assertFalse((self.runtime / "archive").exists())

    def test_reinitializing_same_setup_does_not_archive(self):
        setup, _ = self.save()
        initialize_state_for_setup(setup, runtime=self.runtime, append_event_first=True)
        self.assertFalse((self.runtime / "archive").exists())

    def test_archive_returns_none_when_nothing_to_move(self):
        self.assertIsNone(archive_setup_artifacts("missing", runtime=self.runtime))

    def test_snapshot_and_state_are_isolated_by_setup(self):
        first, _ = self.save()
        (self.runtime / "ai_snapshot.json").write_text(json.dumps({"setup_id": first.setup_id}), encoding="utf-8")
        second, _ = self.save(entry=82149)
        self.assertEqual(read_json(self.runtime / "ai_snapshot.json", {}), {})
        self.assertEqual(read_state_raw(runtime=self.runtime)["setup_id"], second.setup_id)


if __name__ == "__main__":
    unittest.main()
