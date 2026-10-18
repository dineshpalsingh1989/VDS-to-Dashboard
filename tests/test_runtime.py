import json
import os
import tempfile
import unittest
from runtime import atomic_json_write, normalize_event, validate_payload


class RuntimeTests(unittest.TestCase):
    def test_events_accept_json_and_dict(self):
        event = {"type": "data_update"}
        self.assertEqual(normalize_event(event), event)
        self.assertEqual(normalize_event(json.dumps(event)), event)
        for invalid in (None, "bad", "[]", 3):
            self.assertEqual(normalize_event(invalid), {})

    def test_atomic_write_preserves_previous_file_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            atomic_json_write(path, {"enabled": True})
            with self.assertRaises(ValueError):
                atomic_json_write(path, {"invalid": float("nan")})
            with open(path) as stream:
                self.assertEqual(json.load(stream), {"enabled": True})
            self.assertEqual(os.listdir(directory), ["settings.json"])

    def test_payload_validation(self):
        valid = {
            "gateway_id": "GW1",
            "sensor_id": "sensor_1",
            "timestamp": 1700000000000,
            "Time waveform": [0.1, 0.2],
            "sampling_frequency": 12800,
        }
        self.assertEqual(validate_payload(valid), valid)
        for override in (
            {"timestamp": 0},
            {"sampling_frequency": 0},
            {"Time waveform": ["bad"]},
            {"Power spectrum": [float("nan")]},
            {"process_data": []},
            {"gateway_id": 123},
            {"process_data": {"RPM": "fast"}},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_payload(dict(valid, **override))


if __name__ == "__main__":
    unittest.main()
