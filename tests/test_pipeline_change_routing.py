import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "smart_fridge_runtime"
sys.path.insert(0, str(RUNTIME_DIR))

import fridge_pipeline


BOX = {"x1": 0, "y1": 0, "x2": 64, "y2": 64}


def detection(label, confidence, fingerprint=None):
    item = {
        "class_id": 1,
        "class_name": label,
        "confidence": confidence,
        "box": dict(BOX),
    }
    if fingerprint is not None:
        item["change_fingerprint"] = fingerprint
    return item


class YoloChangeCandidateTests(unittest.TestCase):
    def test_change_threshold_routes_candidate_before_semantic_vlm(self):
        payload = {
            "detections": [
                detection("cabbage", 0.48),
                detection("unknown", 0.30),
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "SMART_FRIDGE_YOLO_MOCK_JSON": json.dumps(payload),
                "SMART_FRIDGE_YOLO_MIN_CONFIDENCE": "0.65",
                "SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE": "0.45",
            },
            clear=False,
        ):
            output_path = Path(temp_dir) / "yolo.json"
            result, candidates = fridge_pipeline.run_yolo(
                "/tmp/input.jpg",
                output_path,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["class_name"], "cabbage")
        self.assertEqual(result["pipeline_role"], "change_candidates")
        self.assertEqual(result["pipeline_semantic_authority"], "vlm")
        self.assertEqual(result["pipeline_change_min_confidence"], 0.45)
        self.assertEqual(result["pipeline_filtered_count"], 1)

    def test_matching_ignores_yolo_label_when_visual_region_is_unchanged(self):
        previous = [
            {
                "food_id": "food-1",
                "yolo_label": "cabbage",
                "box": dict(BOX),
                "change_fingerprint": "0000000000000000",
            }
        ]
        current = [detection("lettuce", 0.51, "0000000000000000")]

        matches, added, removed = fridge_pipeline.match_detections(
            previous,
            current,
            threshold=0.35,
            max_hash_distance=16,
        )

        self.assertEqual(matches, {0: (0, 1.0, 0)})
        self.assertEqual(added, [])
        self.assertEqual(removed, [])

    def test_same_box_with_changed_pixels_is_a_new_candidate(self):
        previous = [
            {
                "food_id": "food-1",
                "yolo_label": "cabbage",
                "box": dict(BOX),
                "change_fingerprint": "0000000000000000",
            }
        ]
        current = [detection("cabbage", 0.51, "ffffffffffffffff")]

        matches, added, removed = fridge_pipeline.match_detections(
            previous,
            current,
            threshold=0.35,
            max_hash_distance=16,
        )

        self.assertEqual(matches, {})
        self.assertEqual(added, [0])
        self.assertEqual(removed, [0])

    def test_rejected_background_is_not_sent_to_vlm_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "scene.jpg"
            image = Image.new("RGB", (64, 64), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((4, 8, 58, 50), fill="gray")
            image.save(image_path)

            mock_yolo = json.dumps({"detections": [detection("ceiling", 0.52)]})
            non_food = fridge_pipeline.normalize_vlm_result(
                {
                    "is_food": False,
                    "food_name": "unknown_food",
                    "category": "unknown",
                    "freshness": "unknown",
                    "risk_level": "unknown",
                }
            )
            environment = {
                "SMART_FRIDGE_ROOT": str(root),
                "SMART_FRIDGE_STATE_PATH": str(root / "data" / "pipeline_state.json"),
                "SMART_FRIDGE_TMP_DIR": str(root / "tmp"),
                "SMART_FRIDGE_YOLO_MOCK_JSON": mock_yolo,
                "SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE": "0.45",
                "SMART_FRIDGE_CHANGE_HASH_MAX_DISTANCE": "16",
                "SMART_FRIDGE_CLOUD_ADVICE_ENABLED": "0",
                "SMART_FRIDGE_VLM_MOCK_JSON": "",
            }
            args = argparse.Namespace(once=True, image=str(image_path))
            with patch.dict(os.environ, environment, clear=False), patch.object(
                fridge_pipeline,
                "call_vlm",
                return_value=non_food,
            ) as call_vlm:
                first = fridge_pipeline.run_once(args)
                second = fridge_pipeline.run_once(args)

            state = fridge_pipeline.read_json(environment["SMART_FRIDGE_STATE_PATH"])

        self.assertEqual(call_vlm.call_count, 1)
        self.assertEqual(first["rejected_candidate_count"], 1)
        self.assertEqual(second["added"], [])
        self.assertEqual(len(second["suppressed_candidates"]), 1)
        self.assertEqual(second["suppressed_candidates"][0]["reason"], "known_vlm_non_food")
        self.assertEqual(state["active_objects"], [])
        self.assertEqual(len(state["rejected_candidates"]), 1)

    def test_vlm_prompt_treats_yolo_label_as_routing_metadata(self):
        text = fridge_pipeline.build_vlm_user_text(
            detection("cabbage", 0.48),
            {"available": False},
        )

        self.assertIn("not food identity evidence", text)
        self.assertIn("identify it independently from the pixels", text)

    def test_vlm_error_does_not_ingest_yolo_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "scene.jpg"
            Image.new("RGB", (64, 64), "white").save(image_path)
            environment = {
                "SMART_FRIDGE_ROOT": str(root),
                "SMART_FRIDGE_STATE_PATH": str(root / "data" / "pipeline_state.json"),
                "SMART_FRIDGE_TMP_DIR": str(root / "tmp"),
                "SMART_FRIDGE_YOLO_MOCK_JSON": json.dumps(
                    {"detections": [detection("cabbage", 0.52)]}
                ),
                "SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE": "0.45",
                "SMART_FRIDGE_WRITE_FALLBACK_ON_VLM_ERROR": "0",
                "SMART_FRIDGE_CLOUD_ADVICE_ENABLED": "0",
            }
            args = argparse.Namespace(once=True, image=str(image_path))
            with patch.dict(os.environ, environment, clear=False), patch.object(
                fridge_pipeline,
                "call_vlm",
                side_effect=RuntimeError("timeout"),
            ) as call_vlm, patch.object(
                fridge_pipeline,
                "ingest_added_detection",
            ) as ingest:
                first = fridge_pipeline.run_once(args)
                second = fridge_pipeline.run_once(args)

            state = fridge_pipeline.read_json(environment["SMART_FRIDGE_STATE_PATH"])

        self.assertEqual(call_vlm.call_count, 2)
        ingest.assert_not_called()
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(first["added"], [])
        self.assertEqual(state["active_objects"], [])
        self.assertFalse(first["errors"][0]["fallback"])

    def test_opt_in_fallback_never_adopts_yolo_label(self):
        fallback = fridge_pipeline.fallback_vlm_result(
            detection("cabbage", 0.52),
            "timeout",
        )

        self.assertEqual(fallback["food_name"], "unknown_food")
        self.assertEqual(fallback["composition"], [])
        self.assertEqual(fallback["confidence"], 0.0)
        self.assertEqual(fallback["identification_status"], "pending_vlm")

    def test_invalid_vlm_enum_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "freshness must be one of"):
            fridge_pipeline.normalize_vlm_result(
                {
                    "is_food": True,
                    "food_name": "大白菜",
                    "category": "vegetable",
                    "composition": ["叶菜"],
                    "freshness": "attention|danger|unknown",
                    "risk_level": "normal",
                }
            )

    def test_invalid_vlm_name_option_list_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid option separator"):
            fridge_pipeline.normalize_vlm_result(
                {
                    "is_food": True,
                    "food_name": "大白菜|n缨菜",
                    "category": "vegetable",
                    "composition": ["叶菜"],
                    "freshness": "attention",
                    "risk_level": "normal",
                }
            )

    def test_vlm_response_schema_constrains_semantic_fields(self):
        schema = fridge_pipeline.VLM_RESPONSE_SCHEMA

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["freshness"]["enum"],
            list(fridge_pipeline.VLM_STATE_VALUES),
        )
        self.assertEqual(
            schema["properties"]["risk_level"]["enum"],
            list(fridge_pipeline.VLM_STATE_VALUES),
        )
        self.assertEqual(
            schema["properties"]["category"]["enum"],
            list(fridge_pipeline.VLM_CATEGORY_VALUES),
        )


if __name__ == "__main__":
    unittest.main()
