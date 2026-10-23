import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app
from vds_dashboard import historian
from vds_dashboard.diagnostics import history_figure, spectrum_figure, waterfall_figure
from vds_dashboard.monitoring import assess_health
from vds_dashboard.vds import advance_alarm, freshness, normalize_packet, protocol_value, spectrum_xy

MESSAGES = json.loads(
    (Path(__file__).parent / "fixtures" / "vds130_messages.json").read_text(
        encoding="utf-8"
    )
)
NOW = MESSAGES[0]["timestamp"] / 1000 + 1
META = MESSAGES[0]["process_data"]["Velocity"]


class VdsTests(unittest.TestCase):
    def test_documented_messages_keep_units_and_supplied_standard_deviation(self):
        packet = normalize_packet(MESSAGES[0], NOW)
        self.assertEqual(packet["metrics"]["Std Deviation"], 0.25)
        self.assertEqual(packet["metadata"]["Velocity"]["unit"], "mm/s")
        self.assertEqual(packet["quality_issues"], [])
        self.assertEqual(normalize_packet(MESSAGES[1], NOW)["metrics"], {})

    def test_freshness_follows_three_publishing_intervals(self):
        state = dict(
            timestamp_ms=int(NOW * 1000), received_at=NOW, publish_interval=120
        )
        self.assertEqual(freshness(state, NOW + 30)["status"], "fresh")
        self.assertEqual(freshness(state, NOW + 359)["status"], "fresh")
        self.assertEqual(freshness(state, NOW + 361)["status"], "stale")
        self.assertEqual(freshness(state, NOW - 200)["status"], "clock error")

    def test_bad_lengths_are_flagged_without_losing_scalar_measurements(self):
        payload = copy.deepcopy(MESSAGES[0])
        payload["duration"] = 2
        packet = normalize_packet(payload, NOW)
        self.assertIn("Velocity", packet["metrics"])
        self.assertTrue(packet["quality_issues"])
        spectrum = normalize_packet(MESSAGES[1], NOW)
        spectrum["spectrum_data"] = [0, 1]
        with self.assertRaises(ValueError):
            spectrum_xy(spectrum)

    def test_alarm_persistence_hysteresis_and_metadata_changes(self):
        rule = dict(warn=3, alert=6, hysteresis=0.5, persistence=2, metadata=META)
        state = advance_alarm({}, 7, META, rule)
        self.assertEqual(state["severity"], "normal")
        state = advance_alarm(state, 7, META, rule)
        self.assertEqual(state["severity"], "alert")
        self.assertEqual(advance_alarm(state, 5.7, META, rule)["severity"], "alert")
        state = advance_alarm(state, 2, META, rule)
        self.assertEqual(advance_alarm(state, 2, META, rule)["severity"], "normal")
        changed = dict(META, unit="in/s")
        self.assertEqual(advance_alarm({}, 7, changed, rule)["severity"], "unknown")

    def test_spectrum_axes_baseline_and_waterfall(self):
        current = normalize_packet(MESSAGES[1], NOW)
        x, _ = spectrum_xy(current)
        self.assertEqual(x.tolist(), [0, 1, 2, 3, 4])
        self.assertEqual(len(spectrum_figure(current, current).data), 2)
        self.assertEqual(len(waterfall_figure([current]).data), 1)
        with self.assertRaises(ValueError):
            spectrum_figure(current, dict(current, bandwidth=8))
        with self.assertRaises(ValueError):
            spectrum_xy(dict(current, rpm=0), orders=True)

    def test_protocol_quality_is_not_a_stale_numeric_reading(self):
        state = dict(
            timestamp_ms=int(NOW * 1000), received_at=NOW, metrics={"Velocity": 4}
        )
        device = {"channels": {"1": state}}
        self.assertEqual(protocol_value(device, "1", "Data Age", now=NOW + 181), 181)
        self.assertEqual(protocol_value(device, "1", "Data Valid", now=NOW + 181), 0)
        self.assertEqual(
            protocol_value(
                device, "1", "Communication Status", connected=False, now=NOW
            ),
            0,
        )

    def test_history_separates_incompatible_units_and_baseline_agent_explains_changes(
        self,
    ):
        import pandas as pd

        frame = pd.DataFrame(
            [
                dict(
                    timestamp="2026-10-18",
                    value=2,
                    context=json.dumps(
                        dict(unit="mm/s", filter="10Hz-1kHz", detection="RMS")
                    ),
                ),
                dict(
                    timestamp="2026-10-18",
                    value=0.1,
                    context=json.dumps(
                        dict(unit="in/s", filter="10Hz-1kHz", detection="RMS")
                    ),
                ),
            ]
        )
        self.assertEqual(len(history_figure(frame, "Velocity").data), 2)
        current = dict(
            timestamp_ms=int(NOW * 1000),
            received_at=NOW,
            metrics={"Velocity": 6},
            metadata={"Velocity": META},
        )
        baseline = dict(metrics={"Velocity": 2}, metadata={"Velocity": META})
        findings = assess_health(
            {"GW": {"channels": {"1": current}}},
            "connected",
            50,
            now=NOW,
            baselines={("GW", "1"): baseline},
        )
        self.assertTrue(
            any(
                agent == "Baseline agent" and "+200.0%" in text
                for _, agent, text in findings
            )
        )


class HistorianTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.directory.name, "test.db")
        self.patch = patch.object(app, "DB_FILE", self.db)
        self.patch.start()
        app.setup_database()
        self.conn = app.get_db_connection()

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.directory.cleanup()

    def packet(self, timestamp_offset=0, value=2.5):
        payload = copy.deepcopy(MESSAGES[0])
        payload["timestamp"] += timestamp_offset * 1000
        payload["process_data"]["Velocity"]["value"] = value
        return normalize_packet(payload, NOW + timestamp_offset)

    def test_scalar_history_is_continuous_and_old_packets_do_not_replace_latest(self):
        historian.store_packet(self.conn, self.packet(60, 4))
        historian.store_packet(self.conn, self.packet(0, 2))
        self.conn.commit()
        latest = self.conn.execute(
            "SELECT value,context FROM trends_latest WHERE metric_name='Velocity'"
        ).fetchone()
        self.assertEqual(latest[0], 4)
        self.assertEqual(json.loads(latest[1])["unit"], "mm/s")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM trends").fetchone()[0], 12
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_waveforms").fetchone()[0], 0
        )

    def test_alarm_events_are_durable_and_duplicate_packets_do_not_advance(self):
        historian.save_rule(
            self.conn,
            "VDS-DEMO",
            "1",
            dict(
                enabled=True,
                metric="Velocity",
                warn=3,
                alert=6,
                persistence=2,
                hysteresis=0.3,
                metadata=META,
            ),
        )
        packet = self.packet(0, 7)
        historian.store_packet(self.conn, packet)
        historian.store_packet(self.conn, packet)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM alarm_events").fetchone()[0], 0
        )
        historian.store_packet(self.conn, self.packet(60, 7))
        self.assertEqual(
            self.conn.execute("SELECT severity FROM alarm_events").fetchone()[0],
            "alert",
        )
        self.conn.commit()
        with closing(sqlite3.connect(self.db)) as conn:
            state = historian.get_json(conn, "alarm_state", "state", "VDS-DEMO", "1")
            self.assertEqual(state["severity"], "alert")

    def test_backup_restores_context_and_retention_preserves_active_jobs(self):
        historian.store_packet(self.conn, self.packet(), recording=True)
        self.conn.commit()
        destination = os.path.join(self.directory.name, "backup.db")
        historian.backup_database(self.conn, destination)
        with closing(sqlite3.connect(destination)) as restored:
            self.assertEqual(
                restored.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                restored.execute("SELECT COUNT(*) FROM trends").fetchone()[0], 6
            )
            self.assertEqual(
                historian.get_json(restored, "channel_state", "state", "VDS-DEMO", "1")[
                    "metadata"
                ]["Velocity"]["unit"],
                "mm/s",
            )
        start = datetime.fromtimestamp(NOW) - timedelta(hours=1)
        end = start + timedelta(hours=2)
        self.conn.execute(
            "INSERT INTO recording_jobs VALUES (?,?,?,?,?,?)",
            ("job", "VDS-DEMO", start, end, 2, "active"),
        )
        historian.prune_history(self.conn, 1, 1, now=end + timedelta(days=3))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_waveforms").fetchone()[0], 1
        )
        self.conn.execute("UPDATE recording_jobs SET status='completed'")
        historian.prune_history(self.conn, 1, 1, now=end + timedelta(days=3))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_waveforms").fetchone()[0], 0
        )

    def test_daily_report_and_backup_schedule(self):
        historian.store_packet(self.conn, self.packet())
        self.conn.commit()
        day = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
        report = historian.daily_report(self.conn, day)
        self.assertIn("Velocity", report)
        self.assertIn("mm/s", report)
        with patch.dict(
            os.environ,
            {
                "VDS_TREND_RETENTION_DAYS": "0",
                "VDS_RAW_RETENTION_DAYS": "0",
                "VDS_ALARM_RETENTION_DAYS": "0",
            },
        ):
            self.assertTrue(historian.maintenance_once(self.conn, self.directory.name))
            self.assertFalse(historian.maintenance_once(self.conn, self.directory.name))


if __name__ == "__main__":
    unittest.main()
