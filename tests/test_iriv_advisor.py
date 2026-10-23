"""IRIV Edge AI advisor: read-only scoring and dashboard display, on temporary databases."""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

import app
from vds_dashboard.monitoring import load_ai_findings

ADVISOR_DIR = Path(__file__).resolve().parent.parent / "iriv-edge"
sys.path.insert(0, str(ADVISOR_DIR))
spec = importlib.util.spec_from_file_location("ai_advisor", ADVISOR_DIR / "ai_advisor.py")
advisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(advisor)

NOW = datetime(2026, 10, 1, 12, 0, 0)
VELOCITY = {"unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS"}


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.directory.name, "vibration_trends.db")
        self.patch = patch.object(app, "DB_FILE", self.database)
        self.patch.start()
        app.setup_database()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def add_metric(self, history, latest, rpm=1200, latest_rpm=None, metadata=VELOCITY, latest_metadata=None):
        rng = np.random.default_rng(1)
        with closing(app.get_db_connection()) as conn, conn:
            for index, value in enumerate(history + rng.normal(0, 0.05, len(history))):
                stamp = advisor.db_time(NOW - timedelta(minutes=index + 1))
                conn.execute("INSERT INTO trends (timestamp,gateway_id,channel,metric_name,value,rpm,context) VALUES (?,?,?,?,?,?,?)",
                             (stamp, "GW1", "1", "Velocity", float(value), rpm, json.dumps(dict(metadata, timestamp_ms=index))))
            context = json.dumps(dict(latest_metadata or metadata, timestamp_ms=0))
            conn.execute("INSERT INTO trends_latest (gateway_id,channel,metric_name,value,rpm,timestamp,context) VALUES (?,?,?,?,?,?,?)",
                         ("GW1", "1", "Velocity", latest, latest_rpm or rpm, advisor.db_time(NOW), context))

    def add_spectra(self, count, latest_peak):
        rng = np.random.default_rng(2)
        context = json.dumps({"sensor_type": "Acceleration(g)", "sensitivity": 100, "sampling_freq": 2560, "window": "hann"})
        with closing(app.get_db_connection()) as conn, conn:
            for index in range(count + 1):
                values = 1e-4 * (1 + 0.1 * rng.random(801))
                values[100] = 1e-2  # running-speed peak present in every recording
                if index == 0 and latest_peak:
                    values[400] = 5e-2  # new peak near 500 Hz in the newest recording
                conn.execute("INSERT INTO raw_spectrum (gateway_id,channel,timestamp,spectrum_data,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?)",
                             ("GW1", "2", advisor.db_time(NOW - timedelta(minutes=10 * index)),
                              np.asarray(values, "<f8").tobytes(), 1000, 800, context))

    def test_metric_jump_is_reported_with_context(self):
        self.add_metric(np.full(40, 2.5), latest=8.0)
        report = advisor.analyse(self.database, now=NOW)
        self.assertEqual(report["scored"]["metrics"], 1)
        [finding] = report["findings"]
        self.assertEqual(finding["severity"], "warning")
        self.assertIn("GW1 CH1 Velocity", finding["message"])
        self.assertIn("above", finding["message"])

    def test_normal_reading_is_quiet(self):
        self.add_metric(np.full(40, 2.5), latest=2.52)
        self.assertEqual(advisor.analyse(self.database, now=NOW)["findings"], [])

    def test_changed_measurement_settings_are_not_compared(self):
        changed = dict(VELOCITY, filter="2Hz-1kHz")
        self.add_metric(np.full(40, 2.5), latest=8.0, latest_metadata=changed)
        report = advisor.analyse(self.database, now=NOW)
        self.assertEqual((report["scored"]["metrics"], report["findings"]), (0, []))

    def test_different_speed_is_not_compared(self):
        self.add_metric(np.full(40, 2.5), latest=8.0, rpm=1200, latest_rpm=1800)
        self.assertEqual(advisor.analyse(self.database, now=NOW)["scored"]["metrics"], 0)

    def test_new_spectral_peak_is_located(self):
        self.add_spectra(15, latest_peak=True)
        report = advisor.analyse(self.database, now=NOW)
        self.assertEqual(report["scored"]["spectra"], 1)
        [finding] = report["findings"]
        self.assertIn("GW1 CH2 spectrum", finding["message"])
        self.assertRegex(finding["message"], r"near 49\d Hz|near 50\d Hz")

    def test_steady_spectrum_is_quiet(self):
        self.add_spectra(15, latest_peak=False)
        self.assertEqual(advisor.analyse(self.database, now=NOW)["findings"], [])

    def test_model_backend_uses_reconstruction_error(self):
        class FakeModel:
            name = "test"

            def reconstruction_error(self, features):
                return np.r_[np.full(len(features) - 1, 1.0) + np.linspace(0, 0.01, len(features) - 1), 9.0]

        self.add_spectra(15, latest_peak=False)
        [finding] = advisor.analyse(self.database, now=NOW, model=FakeModel())["findings"]
        self.assertIn("test model", finding["message"])

    def test_historian_connection_is_read_only(self):
        with closing(advisor.connect_readonly(self.database)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM trends")

    def test_waits_for_dashboard_without_creating_database_files(self):
        data = os.path.join(self.directory.name, "empty")
        advisor.main(["--data-dir", data, "--once"])
        self.assertEqual(sorted(os.listdir(data)), ["ai"])
        report = json.loads(Path(data, "ai", "findings.json").read_text())
        self.assertIn("Waiting for the dashboard", report["error"])


class DashboardDisplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def write(self, report):
        advisor.write_report(self.data, report)

    def test_missing_report_shows_nothing(self):
        self.assertEqual(load_ai_findings(self.data), [])

    def test_findings_are_capped_at_warning_and_truncated(self):
        self.write({"generated_at": time.time(), "findings": [
            {"severity": "danger", "message": "x" * 1000}, {"severity": "warning", "message": "check CH1"}]})
        findings = load_ai_findings(self.data)
        self.assertEqual([f[0] for f in findings], ["info", "warning"])
        self.assertEqual(len(findings[0][2]), 400)

    def test_quiet_report_says_what_was_compared(self):
        self.write({"generated_at": time.time(), "backend": "hailo", "findings": [],
                    "scored": {"metrics": 3, "spectra": 1}})
        [(severity, agent, message)] = load_ai_findings(self.data)
        self.assertEqual((severity, agent), ("success", "AI advisor"))
        self.assertIn("AI HAT+; 3 metrics and 1 spectra", message)

    def test_stale_or_broken_report_is_flagged(self):
        self.write({"generated_at": time.time() - 3600, "findings": []})
        self.assertIn("No AI advisor update", load_ai_findings(self.data)[0][2])
        Path(self.data, "ai", "findings.json").write_text("{not json")
        self.assertEqual(load_ai_findings(self.data)[0][0], "warning")


if __name__ == "__main__":
    unittest.main()
