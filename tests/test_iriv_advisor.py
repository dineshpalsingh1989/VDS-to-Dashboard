"""IRIV Edge AI self-learning advisor, on temporary databases only."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

import app
from vds_dashboard import historian
from vds_dashboard.monitoring import load_ai_findings

ADVISOR_DIR = Path(__file__).resolve().parent.parent / "iriv-edge"
sys.path.insert(0, str(ADVISOR_DIR))
import ai_advisor  # noqa: E402
import bearing_tool  # noqa: E402
from symptoms import bearing_orders, load_bearings  # noqa: E402

RPM = 1500                      # 25 Hz shaft speed
BANDWIDTH, LINES = 1000.0, 800  # 1.25 Hz resolution
START_MS = 1_790_000_000_000
STEP_MS = 600_000               # one learning sample every 10 minutes
CONTEXT = json.dumps({"sensor_type": "Acceleration(g)", "sensitivity": 100, "window": "hann"})
BPFO_6205 = 3.5848


def spectrum(rng, fault=False, loose=False):
    values = 1e-4 * (1 + 0.2 * rng.random(LINES + 1))
    line = lambda hz: int(round(hz / (BANDWIDTH / LINES)))  # noqa: E731
    values[line(25)] = 1e-2
    values[line(50)] = 2e-3
    if fault:
        values[line(BPFO_6205 * 25)] = 6e-3
        values[line(2 * BPFO_6205 * 25)] = 2e-3
    if loose:
        for k in range(3, 9):
            values[line(25 * k)] = 3e-3
        values[line(37.5)] = values[line(62.5)] = 1.5e-3
    return values


def waveform(rng, impacts=False):
    t = np.arange(4096) / 4096
    x = np.sin(2 * np.pi * 25 * t) + 0.2 * rng.standard_normal(t.size)
    if impacts:
        x[::200] += 8
    return x


class AdvisorBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name)
        self.patch = patch.object(app, "DB_FILE", str(self.data / "vibration_trends.db"))
        self.patch.start()
        app.setup_database()
        self.knowledge = self.data / "knowledge"
        shutil.copytree(ADVISOR_DIR / "knowledge", self.knowledge)
        self.machines({"GW1/1": {"name": "Fan F-1 motor DE", "machine_type": "motor", "bearing": "6205-2RS",
                                 "direction": "radial", "iso_class": "group2_rigid"}})
        self.rng = np.random.default_rng(7)
        self.ms = START_MS
        self.opened = []
        self.mounts = self.data / "mounts"           # stands in for /proc/mounts
        self.mounts.write_text("/dev/mmcblk0p2 / ext4 rw 0 0\n")
        self.sys_block = self.data / "sys-block"     # stands in for /sys/block
        self.sys_block.mkdir()
        self.switch("GW1", True)

    def tearDown(self):
        for advisor in self.opened:
            if advisor.store:
                advisor.store.conn.close()
        self.patch.stop()
        self.directory.cleanup()

    def machines(self, channels):
        (self.knowledge / "machines.json").write_text(json.dumps({"defaults": {"line_frequency_hz": 50},
                                                                  "channels": channels}))

    def switch(self, gid, enabled):
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_ai_gateway(conn, gid, enabled)

    def advisor(self):
        advisor = ai_advisor.Advisor(self.data, self.knowledge, learn_samples=10, learn_hours=1,
                                     mounts_file=self.mounts, sys_block=self.sys_block)
        self.opened.append(advisor)
        return advisor

    def learned(self, **settings):
        """An advisor that has finished learning GW1 CH1 on normal running."""
        if settings:
            with closing(app.get_db_connection()) as conn, conn:
                historian.save_ai_channel(conn, "GW1", "1", settings)
        advisor = self.advisor()
        self.run_samples(15, rpm=settings.get("_rpm", RPM))
        self.cycle(advisor)
        return advisor

    def add(self, kind, values, gid="GW1", ch="1", rpm=RPM):
        with closing(app.get_db_connection()) as conn, conn:
            conn.execute(
                "INSERT INTO learning_samples (gateway_id,channel,kind,timestamp_ms,received_ms,rpm,data,"
                "sampling_freq,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (gid, ch, kind, self.ms, self.ms, rpm, np.asarray(values, "<f4").tobytes(), 4096,
                 BANDWIDTH, LINES, CONTEXT))

    def run_samples(self, count, rpm=RPM, **kinds):
        for _ in range(count):
            self.add("spectrum", spectrum(self.rng, kinds.get("fault"), kinds.get("loose")), rpm=rpm)
            self.add("waveform", waveform(self.rng, kinds.get("impacts")), rpm=rpm)
            self.ms += STEP_MS

    def cycle(self, advisor):
        return advisor.cycle(now_ms=self.ms)


class AdvisorTests(AdvisorBase):
    def test_learns_first_and_stays_quiet_on_normal_running(self):
        advisor = self.advisor()
        self.run_samples(5)
        report = self.cycle(advisor)
        self.assertEqual(report["channels"], {"monitoring": 0, "learning": 1})
        self.assertEqual(report["findings"], [])
        self.run_samples(20)
        report = self.cycle(advisor)
        self.assertEqual(report["channels"], {"monitoring": 1, "learning": 0})
        self.assertEqual(report["findings"], [])
        self.assertIn("Monitoring 1 channels", report["summary"])

    def test_outer_race_fault_is_found_and_named_with_bearing(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(1, fault=True)
        self.assertEqual(self.cycle(advisor)["findings"], [], "one changed sample is not enough")
        self.run_samples(1, fault=True)
        [finding] = self.cycle(advisor)["findings"]
        self.assertEqual(finding["kind"], "spectrum")
        self.assertIn("Fan F-1 motor DE (GW1 CH1)", finding["message"])
        self.assertIn("Likely Bearing outer race defect (6205-2RS)", finding["message"])
        self.assertIn("peak at 90.0 Hz (3.60x)", finding["message"])
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(finding["detail"]["matches"][0]["id"], "bearing_outer_race")

    def test_fault_is_not_learned_as_normal(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(30, fault=True)
        self.assertEqual(len(self.cycle(advisor)["findings"]), 1)

    def test_unknown_bearing_still_flags_non_synchronous_peak(self):
        self.machines({})
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(2, fault=True)
        [finding] = self.cycle(advisor)["findings"]
        self.assertIn("Possible bearing defect (bearing model unknown)", finding["message"])
        self.assertIn("machines.json", finding["message"])

    def test_bearing_number_typed_in_asset_registry_is_used(self):
        self.machines({})
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_asset(conn, "GW1", "1", {"machine": "Fan F-1", "location": "DE bearing 6205-2RS",
                                                    "direction": "radial", "publish_interval": 60})
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(2, fault=True)
        [finding] = self.cycle(advisor)["findings"]
        self.assertIn("Bearing outer race defect (6205-2RS)", finding["message"])

    def test_looseness_pattern_is_recognised(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(2, loose=True)
        [finding] = self.cycle(advisor)["findings"]
        self.assertIn("Likely Mechanical looseness", finding["message"])

    def test_waveform_impacts_are_reported(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(2, impacts=True)
        findings = self.cycle(advisor)["findings"]
        waveform_finding = next(f for f in findings if f["kind"] == "waveform")
        self.assertIn("kurtosis", waveform_finding["message"])
        self.assertIn("bearing damage, looseness or rubbing", waveform_finding["message"])

    def test_change_clears_after_recovery_and_relearn_forgets(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.cycle(advisor)
        self.run_samples(2, fault=True)
        self.assertTrue(self.cycle(advisor)["findings"])
        self.run_samples(3)
        self.assertEqual(self.cycle(advisor)["findings"], [])
        advisor.store.forget("GW1", "1")
        self.run_samples(1)
        self.assertEqual(self.cycle(advisor)["channels"], {"monitoring": 0, "learning": 1})

    def test_overall_metric_jump_and_iso_zone(self):
        advisor = self.advisor()
        meta = json.dumps({"unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS", "timestamp_ms": 0})
        with closing(app.get_db_connection()) as conn, conn:
            for i in range(30):
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((START_MS + i * STEP_MS) / 1000))
                value = 5.0 if i >= 28 else 1.2 + 0.02 * (i % 3)
                conn.execute("INSERT INTO trends (timestamp,gateway_id,channel,metric_name,value,rpm,context) "
                             "VALUES (?,?,?,?,?,?,?)", (stamp, "GW1", "1", "Velocity", value, RPM, meta))
            conn.execute("INSERT INTO trends_latest (gateway_id,channel,metric_name,value,rpm,timestamp,context) "
                         "VALUES (?,?,?,?,?,?,?)", ("GW1", "1", "Velocity", 5.0, RPM, stamp, meta))
        self.ms = START_MS + 29 * STEP_MS
        [finding] = self.cycle(advisor)["findings"]
        self.assertIn("Velocity is 5 mm/s", finding["message"])
        self.assertIn("ISO zone D", finding["message"])
        self.assertEqual(finding["severity"], "warning")

    def test_work_is_incremental_and_resumes_after_restart(self):
        advisor = self.advisor()
        self.run_samples(15)
        self.assertEqual(self.cycle(advisor)["summary"].split(";")[1].split()[0], "30")
        self.assertIn("; 0 new samples", self.cycle(advisor)["summary"])
        advisor.store.close()
        self.run_samples(1)
        self.assertIn("; 2 new samples", self.cycle(self.advisor())["summary"])

    def test_historian_is_opened_read_only(self):
        with closing(ai_advisor.connect_readonly(self.data / "vibration_trends.db")) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM trends")

    def test_waits_for_dashboard_without_creating_database_files(self):
        empty = self.data / "empty"
        ai_advisor.main(["--data-dir", str(empty), "--knowledge", str(self.knowledge), "--once"])
        self.assertEqual(sorted(os.listdir(empty)), ["ai"])
        report = json.loads((empty / "ai" / "findings.json").read_text())
        self.assertIn("Waiting for the dashboard", report["error"])


class AdvisorFeatureTests(AdvisorBase):
    """AI switch, user-entered sensor details, report folders and SSD storage."""

    def test_switched_off_vds_is_not_analysed(self):
        self.switch("GW1", False)
        advisor = self.advisor()
        self.run_samples(15)
        report = self.cycle(advisor)
        self.assertEqual(report["channels"], {"monitoring": 0, "learning": 0})
        self.assertIn("AI analysis is off", report["summary"])
        self.assertEqual(load_ai_findings_from(report, self.data)[0][0], "info")

    def test_fixed_speed_entered_by_user_replaces_missing_speed_sensor(self):
        self.machines({})
        advisor = self.learned(machine="Fan F-1", location="Motor DE", bearing="6205-2RS",
                               speed_source="fixed", fixed_rpm=RPM, _rpm=None)
        self.run_samples(2, rpm=None, fault=True)
        [finding] = self.cycle(advisor)["findings"]
        self.assertIn("Fan F-1 / Motor DE (GW1 CH1)", finding["message"])
        self.assertIn("Bearing outer race defect (6205-2RS)", finding["message"])

    def test_missing_speed_and_unknown_bearing_are_explained(self):
        self.machines({})
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_ai_channel(conn, "GW1", "1", {"bearing": "9999-XX"})
        advisor = self.advisor()
        self.run_samples(3, rpm=None)
        [row] = self.cycle(advisor)["progress"]
        self.assertIn("Bearing 9999-XX is not in the bearing library", row["notes"][0])
        self.assertIn("reports no speed", row["notes"][1])

    def test_each_new_finding_gets_an_evidence_folder(self):
        advisor = self.learned()
        self.run_samples(2, fault=True)
        [finding] = self.cycle(advisor)["findings"]
        folder = Path(finding["report"])
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder.parent.parent, self.data / "ai" / "reports")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["finding.json", "report.md", "spectrum.csv"])
        self.assertIn("## Likely causes", (folder / "report.md").read_text(encoding="utf-8"))
        self.assertEqual((folder / "spectrum.csv").read_text().splitlines()[0],
                         "frequency_hz,current_db,normal_db,change_db")
        [entry] = json.loads((self.data / "ai" / "index.json").read_text())
        self.assertIn("likely Bearing outer race defect", entry["title"])
        log = (folder.parent / "activity.log").read_text(encoding="utf-8")
        self.assertIn("New finding, report in", log)
        self.run_samples(3)
        self.cycle(advisor)
        self.assertIn("is back to its learned normal", (folder.parent / "activity.log").read_text(encoding="utf-8"))

    def test_relearn_requested_from_dashboard(self):
        advisor = self.learned()
        with closing(app.get_db_connection()) as conn, conn:
            historian.request_ai_relearn(conn, "GW1", "1", now=time.time())
        self.run_samples(1)
        self.assertEqual(self.cycle(advisor)["channels"], {"monitoring": 0, "learning": 1})

    def test_scan_rate_is_capped(self):
        advisor = self.advisor()
        advisor.max_rate = 100
        self.run_samples(10)
        started = time.monotonic()
        self.cycle(advisor)
        self.assertGreaterEqual(time.monotonic() - started, 0.18)  # 20 samples at 100 per second

    def test_unmounted_ssd_is_announced(self):
        (self.sys_block / "nvme0n1").mkdir()
        report = self.cycle(self.advisor())
        self.assertEqual(report["storage"]["state"], "ssd_unmounted")
        self.assertIn("setup-ssd.sh", report["storage"]["message"])

    def test_ai_data_moves_to_mounted_ssd_after_announcement(self):
        ssd = self.data / "ssd"
        ssd.mkdir()
        advisor = self.learned()
        self.run_samples(2, fault=True)
        self.cycle(advisor)
        self.mounts.write_text(f"/dev/mmcblk0p2 / ext4 rw 0 0\n/dev/nvme0n1p1 {ssd} ext4 rw 0 0\n")
        self.run_samples(1)
        report = self.cycle(advisor)
        self.assertEqual(report["storage"]["state"], "pending")
        self.assertIn("will transfer its data to the SSD", report["storage"]["message"])
        self.assertTrue((self.data / "ai" / "learning.db").exists())
        self.run_samples(1)
        report = self.cycle(advisor)
        self.assertEqual(report["storage"]["state"], "ssd")
        self.assertTrue((ssd / "vds-ai" / "learning.db").exists())
        self.assertFalse((self.data / "ai" / "learning.db").exists())
        self.assertTrue(any((ssd / "vds-ai" / "reports").rglob("report.md")))
        self.assertEqual(report["channels"], {"monitoring": 1, "learning": 0}, "learning survived the move")
        advisor.close()
        self.opened.remove(advisor)
        restarted = self.advisor()
        self.assertEqual(self.cycle(restarted)["storage"]["state"], "ssd")
        restarted.close()
        self.opened.remove(restarted)
        shutil.rmtree(ssd)
        missing = self.advisor()
        report = self.cycle(missing)
        self.assertEqual(report["storage"]["state"], "ssd_missing")
        self.assertEqual(load_ai_findings_from(report, self.data)[0][:2], ("warning", "AI storage"))


def load_ai_findings_from(report, data):
    ai_advisor.write_report(data, report)
    return load_ai_findings(str(data))


class AiSettingsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        historian.setup_schema(self.conn)

    def test_fixed_speed_needs_rpm_and_choices_are_checked(self):
        with self.assertRaises(ValueError):
            historian.save_ai_channel(self.conn, "GW1", "1", {"speed_source": "fixed"})
        with self.assertRaises(ValueError):
            historian.save_ai_channel(self.conn, "GW1", "1", {"machine_type": "rocket"})
        with self.assertRaises(ValueError):
            historian.save_ai_channel(self.conn, "GW1", "5", {})
        saved = historian.save_ai_channel(self.conn, "GW1", "1", {
            "machine": " Pump P-1 ", "speed_source": "fixed", "fixed_rpm": "1480", "gear_teeth": "23"})
        self.assertEqual((saved["machine"], saved["fixed_rpm"], saved["gear_teeth"]), ("Pump P-1", 1480.0, 23))

    def test_relearn_request_survives_later_edits(self):
        historian.request_ai_relearn(self.conn, "GW1", "1", now=100)
        historian.save_ai_channel(self.conn, "GW1", "1", {"machine": "Fan"})
        self.assertEqual(historian.ai_channel_settings(self.conn, "GW1", "1")["relearn_at"], 100)

    def test_switch_defaults_off(self):
        self.assertFalse(historian.ai_enabled(self.conn, "GW1"))
        historian.save_ai_gateway(self.conn, "GW1", True)
        self.assertTrue(historian.ai_enabled(self.conn, "GW1"))


class LearningSampleTests(unittest.TestCase):
    """The dashboard keeps a bounded number of samples for the advisor."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        historian.setup_schema(self.conn)

    def packet(self, received_at, timestamp_ms):
        return {"gateway_id": "GW1", "channel": "1", "timestamp_ms": timestamp_ms, "received_at": received_at,
                "rpm": RPM, "spectrum_data": [0.0, 1.0, 2.0], "waveform_data": [0.0, 1.0, -1.0],
                "sampling_freq": 4, "bandwidth": 2, "lines": 2}

    def count(self):
        return self.conn.execute("SELECT kind, COUNT(*) FROM learning_samples GROUP BY kind").fetchall()

    def test_one_sample_per_interval_per_kind(self):
        for second in (0, 60, 300, 599):
            historian.store_learning_samples(self.conn, self.packet(1000 + second, second), "{}", 600)
        self.assertEqual(self.count(), [("spectrum", 1), ("waveform", 1)])
        historian.store_learning_samples(self.conn, self.packet(1600, 600_000), "{}", 600)
        self.assertEqual(self.count(), [("spectrum", 2), ("waveform", 2)])

    def test_each_channel_keeps_only_its_retention_window(self):
        for step in range(50):
            historian.store_learning_samples(self.conn, self.packet(step * 600, step), "{}", 600, retention_hours=6)
        self.assertEqual(self.count(), [("spectrum", 37), ("waveform", 37)])

    def test_interval_defaults_to_ten_minutes_and_zero_turns_it_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VDS_LEARNING_INTERVAL_S", None)
            self.assertEqual(historian.learning_interval(), 600)
        with patch.dict(os.environ, {"VDS_LEARNING_INTERVAL_S": "0"}):
            self.assertEqual(historian.learning_interval(), 0)

    def test_samples_are_only_kept_for_vds_units_with_ai_switched_on(self):
        from vds_dashboard.vds import normalize_packet
        fixture = Path(__file__).parent / "fixtures" / "vds130_messages.json"
        messages = json.loads(fixture.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "DB_FILE", os.path.join(folder, "t.db")):
            app.setup_database()
            with closing(app.get_db_connection()) as conn:
                for enabled, expected in ((False, []), (True, [("spectrum", 1), ("waveform", 1)])):
                    with conn:
                        historian.save_ai_gateway(conn, "VDS-DEMO", enabled)
                        for message in messages:
                            historian.store_packet(conn, normalize_packet(message, received_at=START_MS / 1000))
                    self.assertEqual(conn.execute("SELECT kind, COUNT(*) FROM learning_samples GROUP BY kind")
                                     .fetchall(), expected)

    def test_old_samples_are_pruned(self):
        received = START_MS / 1000
        historian.store_learning_samples(self.conn, self.packet(received, 1), "{}", 600)
        from datetime import datetime, timedelta
        counts = historian.prune_history(self.conn, now=datetime.fromtimestamp(received) + timedelta(hours=7),
                                         learning_hours=6)
        self.assertEqual(counts["learning_samples"], 2)


class KnowledgeTests(unittest.TestCase):
    def test_bearing_formula_matches_published_frequencies(self):
        orders = bearing_orders(9, 7.94, 39.04)
        published = load_bearings(ADVISOR_DIR / "knowledge" / "bearings.csv")["6205"]
        for key in ("bpfo", "bpfi", "bsf", "ftf"):
            self.assertAlmostEqual(orders[key], published[key], places=2)

    def test_bearing_tool_adds_a_bearing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "bearings.csv")
            shutil.copy(ADVISOR_DIR / "knowledge" / "bearings.csv", path)
            bearing_tool.main(["TEST-1", "--orders", "3.1", "4.9", "2.0", "0.4", "--file", str(path)])
            self.assertAlmostEqual(load_bearings(path)["TEST"]["bpfi"], 4.9)
            with self.assertRaises(SystemExit):
                bearing_tool.main(["TEST-1", "--orders", "3.1", "4.9", "2.0", "0.4", "--file", str(path)])

    def test_every_fault_rule_uses_known_evidence(self):
        faults = json.loads((ADVISOR_DIR / "knowledge" / "faults.json").read_text())["faults"]
        known = {"rise", "present", "ratio", "harmonics", "sidebands", "floor_rise", "nonsync_rise",
                 "waveform", "waveform_rise", "direction"}
        for fault in faults:
            self.assertTrue(any(e.get("key") for e in fault["evidence"]), fault["id"])
            for item in fault["evidence"]:
                self.assertIn(item["type"], known, fault["id"])


class DashboardDisplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_report_shows_nothing(self):
        self.assertEqual(load_ai_findings(self.data), [])

    def test_findings_are_capped_at_warning_and_truncated(self):
        ai_advisor.write_report(self.data, {"generated_at": time.time(), "findings": [
            {"severity": "danger", "message": "x" * 1000}, {"severity": "warning", "message": "check CH1"}]})
        findings = load_ai_findings(self.data)
        self.assertEqual([f[0] for f in findings], ["info", "warning"])
        self.assertEqual(len(findings[0][2]), 700)

    def test_quiet_report_shows_learning_summary(self):
        ai_advisor.write_report(self.data, {"generated_at": time.time(), "findings": [],
                                            "summary": "Monitoring 3 channels, still learning 1"})
        [(severity, agent, message)] = load_ai_findings(self.data)
        self.assertEqual((severity, agent), ("success", "AI advisor"))
        self.assertIn("Monitoring 3 channels, still learning 1", message)

    def test_stale_or_broken_report_is_flagged(self):
        ai_advisor.write_report(self.data, {"generated_at": time.time() - 3600, "findings": []})
        self.assertIn("No AI advisor update", load_ai_findings(self.data)[0][2])
        Path(self.data, "ai", "findings.json").write_text("{not json")
        self.assertEqual(load_ai_findings(self.data)[0][0], "warning")


if __name__ == "__main__":
    unittest.main()
