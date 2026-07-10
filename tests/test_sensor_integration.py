import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "smart_fridge_runtime"
sys.path.insert(0, str(RUNTIME_DIR))

import fridge_pipeline
import fridge_sensor
import fridge_web


SAMPLE_FRAME = {
    "v": 2,
    "seq": 819,
    "uptime_ms": 819178,
    "ambient_temp_c": 30.86,
    "ambient_temp_k": 304.01,
    "humidity_pct": 38.9,
    "ntc_temp_c": 31.66,
    "ntc_temp_k": 304.81,
    "ntc_estimated": True,
    "ntc_overtemp": False,
    "door_open": False,
    "door_state": "closed",
    "door_open_count": 0,
    "aht_ok": True,
    "ntc_ok": True,
    "door_sensor_ok": True,
}


class SensorNormalizationTests(unittest.TestCase):
    def test_inverted_door_mapping_uses_actual_physical_state(self):
        state = fridge_sensor.normalize_sensor_frame(
            SAMPLE_FRAME,
            received_at="2026-07-10T13:16:41Z",
            device="/dev/ttyUSB0",
            door_inverted=True,
        )

        self.assertTrue(state["data"]["door_open"])
        self.assertEqual(state["data"]["door_state"], "open")
        self.assertFalse(state["data"]["reported_door_open"])
        self.assertEqual(state["data"]["reported_door_state"], "closed")
        self.assertEqual(state["data"]["reported_door_open_count"], 0)
        self.assertEqual(state["raw"]["door_state"], "closed")

    def test_inverted_open_report_becomes_actual_closed(self):
        frame = dict(SAMPLE_FRAME, door_open=True, door_state="open")
        state = fridge_sensor.normalize_sensor_frame(frame, door_inverted=True)

        self.assertFalse(state["data"]["door_open"])
        self.assertEqual(state["data"]["door_state"], "closed")

    def test_snapshot_freshness_and_ai_context_keep_all_measurements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "sensor_state.json"
            state = fridge_sensor.normalize_sensor_frame(
                SAMPLE_FRAME,
                received_at="2026-07-10T13:16:41Z",
                door_inverted=True,
            )
            fridge_sensor.write_json_atomic(state_path, state)
            loaded = fridge_sensor.read_sensor_state(
                state_path,
                stale_after_seconds=10,
                now=datetime(2026, 7, 10, 13, 16, 46, tzinfo=timezone.utc),
            )
            context = fridge_sensor.sensor_ai_context(loaded)

        self.assertTrue(loaded["fresh"])
        self.assertEqual(loaded["age_seconds"], 5.0)
        self.assertEqual(context["ambient_temp_c"], 30.86)
        self.assertEqual(context["ambient_temp_k"], 304.01)
        self.assertEqual(context["humidity_pct"], 38.9)
        self.assertEqual(context["ntc_temp_c"], 31.66)
        self.assertEqual(context["ntc_temp_k"], 304.81)
        self.assertEqual(context["protocol_version"], 2)
        self.assertTrue(context["door_open"])


class SensorAiAndWebTests(unittest.TestCase):
    def setUp(self):
        normalized = fridge_sensor.normalize_sensor_frame(
            SAMPLE_FRAME,
            received_at="2026-07-10T13:16:41Z",
            door_inverted=True,
        )
        normalized.update({"available": True, "fresh": True, "age_seconds": 1.0})
        self.context = fridge_sensor.sensor_ai_context(normalized)

    def test_vlm_prompt_contains_corrected_sensor_snapshot(self):
        text = fridge_pipeline.build_vlm_user_text(
            {"class_name": "egg", "confidence": 0.9, "box": {"x1": 1}},
            self.context,
        )

        self.assertIn('"door_state": "open"', text)
        self.assertIn('"ambient_temp_c": 30.86', text)
        self.assertIn("corrected physical values", text)

    def test_cloud_payload_contains_same_sensor_snapshot(self):
        payload = fridge_pipeline.build_cloud_advice_user_payload(
            [],
            {"captured_at": "now", "active_count": 0},
            self.context,
        )

        self.assertEqual(payload["sensor_snapshot"], self.context)
        self.assertEqual(payload["sensor_snapshot"]["door_state"], "open")

    def test_web_overview_exposes_sensor_and_page_has_readable_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "data" / "sensor_state.json"
            sensor_state = fridge_sensor.normalize_sensor_frame(
                SAMPLE_FRAME,
                received_at=fridge_sensor.utc_now(),
                door_inverted=True,
            )
            fridge_sensor.write_json_atomic(state_path, sensor_state)
            with patch.dict(
                os.environ,
                {
                    "SMART_FRIDGE_ROOT": str(root),
                    "SMART_FRIDGE_SENSOR_STATE_PATH": str(state_path),
                },
                clear=False,
            ):
                overview = fridge_web.SmartFridgeStore().overview()

        self.assertEqual(overview["sensor"]["data"]["door_state"], "open")
        self.assertIn('id="doorState"', fridge_web.INDEX_HTML)
        self.assertIn('id="ambientTemp"', fridge_web.INDEX_HTML)
        self.assertIn('id="humidity"', fridge_web.INDEX_HTML)
        self.assertIn('id="probeTemp"', fridge_web.INDEX_HTML)
        self.assertIn("冰箱环境", fridge_web.INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
