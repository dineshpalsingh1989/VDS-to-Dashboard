import base64
import io
import json
import os
import pickle
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import app
from vds_dashboard.runtime import decode_config
from vds_dashboard.monitoring import assess_health


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.directory.name, "test.db")
        self.patch = patch.object(app, "DB_FILE", self.database)
        self.patch.start()
        app.setup_database()
        app.data_storage.clear()
        app.active_jobs.clear()
        self.client = app.app.server.test_client()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_layout_and_callback_registration(self):
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            for route in (
                "/",
                "/_dash-layout",
                "/_dash-dependencies",
                "/assets/dashboard.css",
            ):
                with self.client.get(route) as response:
                    self.assertEqual(response.status_code, 200, route)

    def test_remote_access_requires_credentials(self):
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            self.assertEqual(
                self.client.get(
                    "/", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}
                ).status_code,
                403,
            )
        with patch.dict(
            os.environ,
            {"VDS_ADMIN_PASSWORD": "test-password", "VDS_ADMIN_USER": "admin"},
        ):
            self.assertEqual(self.client.get("/").status_code, 401)
            token = base64.b64encode(b"admin:test-password").decode()
            self.assertEqual(
                self.client.get(
                    "/", headers={"Authorization": "Basic " + token}
                ).status_code,
                200,
            )

    def test_first_message_saves_metrics_and_device_atomically(self):
        payload = {
            "gateway_id": "GW1",
            "sensor_id": "sensor_1",
            "timestamp": 1700000000000,
            "Time waveform": [0.0, 0.5, -0.5],
            "process_data": {
                "RPM": 1200,
                **{
                    key: {
                        "value": 1.0,
                        "unit": "test",
                        "filter": "test",
                        "detection": "test",
                    }
                    for key in (
                        "Acceleration",
                        "Velocity",
                        "Displacement",
                        "CrestFactor",
                        "TruePeak",
                        "StandardDeviation",
                    )
                },
            },
        }
        app.on_message(
            None, None, SimpleNamespace(payload=json.dumps(payload).encode())
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trends_latest").fetchone()[0],
                6,
            )
            self.assertEqual(
                connection.execute("SELECT config_data FROM config").fetchone()[0],
                b"{}",
            )

    def test_viewer_cannot_change_settings(self):
        with patch.dict(
            os.environ,
            {
                "VDS_ADMIN_PASSWORD": "admin-secret",
                "VDS_VIEWER_PASSWORD": "viewer-secret",
            },
        ):
            token = base64.b64encode(b"viewer:viewer-secret").decode()
            response = self.client.post(
                "/_dash-update-component",
                json={"output": "mqtt-save-status.is_open"},
                headers={"Authorization": "Basic " + token},
            )
            self.assertEqual(response.status_code, 403)

    def ai_auth(self, user):
        return {"Authorization": "Basic " + base64.b64encode(user.encode()).decode()}

    def test_ai_setup_in_asset_registry_is_admin_only(self):
        from vds_dashboard import ai_views, historian

        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO channel_state VALUES (?,?,?)", ("g", "1", json.dumps({"rpm": 1480})))
        viewer, admin = self.ai_auth("viewer:viewer-test"), self.ai_auth("admin:admin-test")
        channel = {"id": "asset-channel", "property": "value", "value": json.dumps(["g", "1"])}
        form = {"machine_type": "motor", "bearing": "6205-2RS", "speed_source": "vds", "fixed_rpm": None,
                "line_frequency_hz": 50, "gear_teeth": None, "blades": None, "iso_class": "group2_rigid"}
        state = [channel, {"id": "ai-gw-enabled", "property": "value", "value": True}] + [
            {"id": "ai-" + k, "property": "value", "value": form[k]} for k in ai_views.SETUP_KEYS]
        save = [{"id": "ai-save", "property": "n_clicks", "value": 1}]
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "admin-test", "VDS_VIEWER_PASSWORD": "viewer-test",
                                     "VDS_EDGE_AI": "1"}):
            response = self.callback_request("ai_load_setup", [channel], headers=viewer)
            self.assertIn("reports 1480 RPM", response.get_data(as_text=True))
            preview = self.callback_request("ai_bearing_preview", [
                {"id": "ai-bearing", "property": "value", "value": "6205ZZ"},
                {"id": "ai-speed_source", "property": "value", "value": "vds"},
                {"id": "ai-fixed_rpm", "property": "value", "value": None},
                {"id": "ai-bearing-result", "property": "children", "value": None}], [channel], headers=viewer)
            self.assertIn("BPFO 88.4 Hz", preview.get_data(as_text=True))
            self.assertEqual(self.callback_request("ai_save_setup", save, state, headers=viewer).status_code, 403)
            self.assertIn("AI setup saved", self.callback_request("ai_save_setup", save, state, headers=admin)
                          .get_data(as_text=True))
        with closing(app.get_db_connection()) as conn:
            self.assertTrue(historian.ai_enabled(conn, "g"))
            self.assertEqual(historian.ai_channel_settings(conn, "g", "1")["bearing"], "6205-2RS")

    def test_ai_features_exist_only_on_iriv_edge_installs(self):
        assets = [{"id": "url", "property": "pathname", "value": "/assets"}]
        old_page = [{"id": "url", "property": "pathname", "value": "/ai"}]
        save = [{"id": "ai-save", "property": "n_clicks", "value": 1}]
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "0"}):
            self.assertNotIn("ai-gw-enabled", self.callback_request("display_page", assets).get_data(as_text=True))
            from vds_dashboard import ai_views
            refused = self.callback_request("ai_save_setup", save, [
                {"id": "asset-channel", "property": "value", "value": json.dumps(["g", "1"])},
                {"id": "ai-gw-enabled", "property": "value", "value": True}] + [
                {"id": "ai-" + k, "property": "value", "value": None} for k in ai_views.SETUP_KEYS])
            self.assertIn("only available on IRIV Edge AI", refused.get_data(as_text=True))
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "1"}):
            self.assertIn("ai-gw-enabled", self.callback_request("display_page", assets).get_data(as_text=True))
            self.assertNotIn("ai-gw-enabled", self.callback_request("display_page", old_page).get_data(as_text=True))

    def test_dashboard_renders_overview_cards_and_ai_badges(self):
        import time as clock

        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "vds130_messages.json")
        with open(fixture, encoding="utf-8") as stream:
            for message in json.load(stream):
                message["timestamp"] = int(clock.time() * 1000)
                app.on_message(None, None, SimpleNamespace(payload=json.dumps(message).encode()))
        os.makedirs(os.path.join(self.directory.name, "ai"), exist_ok=True)
        with open(os.path.join(self.directory.name, "ai", "findings.json"), "w", encoding="utf-8") as stream:
            json.dump({"generated_at": clock.time(), "findings": [{"gateway_id": "VDS-DEMO", "channel": "1",
                       "title": "Pump: likely Bearing outer race defect", "message": "x", "severity": "warning"}]},
                      stream)
        inputs = [{"id": "live-update-trigger", "property": "data", "value": 1},
                  {"id": "device-config-store", "property": "data", "value": {}}, []]
        state = [{"id": "store-accordion-state", "property": "data", "value": ["item-VDS-DEMO"]}]
        with patch.object(app, "DATA_FOLDER", self.directory.name), \
                patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "1"}):
            response = self.callback_request("update_dynamic_dashboard", inputs, state,
                                             changed=["live-update-trigger.data"])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        body = response.get_data(as_text=True)
        for text in ("overview-strip", "Sensors live", "AI findings", "hero-value", "metric-grid", "Sensor type: ",
                     "Bearing outer race defect", "ai-finding"):
            self.assertIn(text, body)

    def test_spectrum_popup_draws_fault_lines_for_the_bearing(self):
        import numpy as np

        values = np.full(801, 1e-4)
        values[20] = values[71] = 1e-2  # 1x at 25 Hz and a peak near BPFO
        app.data_storage["g"] = {"rpm": 990, "channels": {"1": {  # another machine on this VDS runs at 990
            "timestamp": "now", "rpm": 1480, "spectrum_context": {"bandwidth": 1000, "lines": 800},
            "spectrum_data": values.tolist()}}}
        target = {"id": "modal-target", "property": "data", "value": {"gid": "g", "ch": "1", "type": "spec"}}
        inputs = [target, {"id": "modal-bearing", "property": "value", "value": "6205"},
                  {"id": "modal-families", "property": "value", "value": ["speed", "bearing"]},
                  {"id": "modal-harmonics", "property": "value", "value": 2},
                  {"id": "modal-scale", "property": "value", "value": "log"}]
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "admin-test", "VDS_VIEWER_PASSWORD": "viewer-test",
                                     "VDS_EDGE_AI": "1"}):
            response = self.callback_request("render_modal_graph", inputs, headers=self.ai_auth("viewer:viewer-test"))
            self.assertEqual(response.status_code, 200)
            body = json.loads(response.get_data(as_text=True))["response"]
        figure = body["modal-graph"]["figure"]
        texts = [re.sub("<[^>]+>", "", a["text"]) for a in figure["layout"]["annotations"]]
        for name in ("1x", "2x", "BPFO", "BPFI", "FTF"):
            self.assertIn(name, texts)  # coloured label tags
        self.assertTrue(any("BPFO 88.4 Hz" in text for text in texts), "colour key lists each line with Hz")
        self.assertTrue(any(shape["type"] == "rect" for shape in figure["layout"]["shapes"]), "shaded bands")
        lines = next(trace for trace in figure["data"] if trace.get("name") == "Fault lines")
        self.assertIn("2xBPFO", lines["text"])  # harmonics are drawn and named on hover
        self.assertEqual(body["modal-graph"]["figure"]["layout"]["yaxis"]["type"], "log")
        self.assertIn("Speed: 1480 RPM", body["modal-info"]["children"])
        self.assertIn("Bearing 6205-2RS: BPFO 88.4 Hz", body["modal-info"]["children"])
        self.assertIn("BPFO", json.dumps(body["modal-peak-table-container"]))
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "0"}):
            plain = json.loads(self.callback_request("render_modal_graph", inputs).get_data(as_text=True))
        plain_texts = " ".join(a["text"] for a in plain["response"]["modal-graph"]["figure"]["layout"]["annotations"])
        self.assertIn("1x", plain_texts)
        self.assertNotIn("BPFO", plain_texts)

    def test_cross_origin_action_rejected(self):
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "admin-secret"}):
            token = base64.b64encode(b"admin:admin-secret").decode()
            response = self.client.post(
                "/_dash-update-component",
                json={},
                headers={
                    "Authorization": "Basic " + token,
                    "Origin": "https://untrusted.example",
                },
            )
            self.assertEqual(response.status_code, 403)

    def test_recording_start_stop_and_restart_recovery(self):
        app.start_recording_job(1, 1, "GW1")
        self.assertIn("GW1", app.active_jobs)
        app.start_recording_job(2, 1, "GW1")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM recording_jobs").fetchone()[0],
                1,
            )
        app.active_jobs.clear()
        app.load_data_from_db_on_startup()
        self.assertIn("GW1", app.active_jobs)
        app.stop_recording_job(1, "GW1")
        self.assertNotIn("GW1", app.active_jobs)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM recording_jobs").fetchone()[0],
                "completed",
            )

    def test_recording_persists_float64_waveforms_and_spectrum(self):
        app.start_recording_job(1, 1, "GW1")
        for values in ({"Time waveform": [1, 2, 3]}, {"Power spectrum": [4, 5, 6]}):
            payload = dict(
                gateway_id="GW1",
                sensor_id="sensor_1",
                timestamp=1700000000000,
                **values,
            )
            app.on_message(
                None, None, SimpleNamespace(payload=json.dumps(payload).encode())
            )
        with closing(sqlite3.connect(self.database)) as connection:
            for table, column in (
                ("raw_waveforms", "waveform_data"),
                ("raw_spectrum", "spectrum_data"),
            ):
                blob = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                self.assertEqual(len(blob), 3 * 8)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trends").fetchone()[0], 0
            )

    def test_invalid_message_does_not_create_device(self):
        app.on_message(None, None, SimpleNamespace(payload=b'{"gateway_id": "bad"}'))
        self.assertEqual(app.data_storage, {})

    def test_download_is_complete_sqlite_snapshot(self):
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            response = self.client.get("/download/test.db")
        self.assertEqual(response.status_code, 200)
        snapshot = os.path.join(self.directory.name, "download.db")
        with open(snapshot, "wb") as stream:
            stream.write(response.data)
        with closing(sqlite3.connect(snapshot)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='trends_latest'"
                ).fetchone()
            )

    def test_legacy_config_preserved_without_executable_objects(self):
        value = {"labels": {"1": "Motor bearing"}, "alarms": {"1": {"enabled": True}}}
        self.assertEqual(decode_config(pickle.dumps(value)), value)
        with self.assertRaises(pickle.UnpicklingError):
            decode_config(pickle.dumps(os.system))

    def test_health_agents_report_connectivity_and_storage(self):
        findings = assess_health({}, "disconnected", 5)
        self.assertEqual(
            {finding[1] for finding in findings},
            {"Connectivity agent", "Storage agent", "Sensor agent"},
        )

    def callback_request(self, name, inputs, state=None, headers=None, changed=None):
        key, callback = next(
            (key, value)
            for key, value in app.app.callback_map.items()
            if getattr(value.get("callback"), "__name__", "") == name
        )
        outputs = callback["output"]

        def encode(output):
            return {"id": output.component_id, "property": output.component_property}

        outputs = (
            [encode(output) for output in outputs]
            if isinstance(outputs, list)
            else encode(outputs)
        )
        return self.client.post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs,
                "inputs": inputs,
                "state": state or [],
                "changedPropIds": changed
                or [inputs[0]["id"] + "." + inputs[0]["property"]],
            },
            headers=headers or {},
        )

    def test_operations_pages_render_and_asset_save_persists(self):
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            for page in ("/assets", "/alarms", "/diagnostics", "/reports"):
                response = self.callback_request(
                    "display_page",
                    [{"id": "url", "property": "pathname", "value": page}],
                )
                self.assertEqual(response.status_code, 200, response.data)
            state = [
                {
                    "id": "asset-channel",
                    "property": "value",
                    "value": json.dumps(["GW1", "1"]),
                }
            ]
            state += [
                {"id": "asset-" + key, "property": "value", "value": value}
                for key, value in (
                    ("site", "Plant A"),
                    ("machine", "Pump 3"),
                    ("location", "Drive end"),
                    ("direction", "Horizontal"),
                    ("interval", 120),
                )
            ]
            response = self.callback_request(
                "save_asset_form",
                [{"id": "asset-save", "property": "n_clicks", "value": 1}],
                state,
            )
            self.assertEqual(response.status_code, 200, response.data)
            with closing(sqlite3.connect(self.database)) as conn:
                self.assertIn(
                    "Pump 3",
                    conn.execute("SELECT settings FROM asset_settings").fetchone()[0],
                )

    def test_viewer_cannot_save_new_rules_or_acknowledge_events(self):
        with patch.dict(
            os.environ,
            {
                "VDS_ADMIN_PASSWORD": "admin-secret",
                "VDS_VIEWER_PASSWORD": "viewer-secret",
            },
        ):
            token = base64.b64encode(b"viewer:viewer-secret").decode()
            for name, identity in (
                ("save_alarm_rule", "rule-save"),
                ("acknowledge_alarm", "alarm-ack"),
                ("capture_baseline", "baseline-save"),
                ("download_backup", "backup-download"),
            ):
                response = self.callback_request(
                    name,
                    [{"id": identity, "property": "n_clicks", "value": 1}],
                    headers={"Authorization": "Basic " + token},
                )
                self.assertEqual(response.status_code, 403, name)

    def test_status_poll_cannot_reenable_a_stopped_opc_service(self):
        with (
            patch.dict(
                os.environ,
                {
                    "VDS_ADMIN_PASSWORD": "admin-secret",
                    "VDS_VIEWER_PASSWORD": "viewer-secret",
                },
            ),
            patch.object(app, "opcua_enabled_flag", False),
        ):
            token = base64.b64encode(b"viewer:viewer-secret").decode()
            response = self.callback_request(
                "toggle_opc_status",
                [
                    {"id": "opc-server-switch", "property": "value", "value": True},
                    {
                        "id": "system_info_interval",
                        "property": "n_intervals",
                        "value": 1,
                    },
                ],
                headers={"Authorization": "Basic " + token},
                changed=["system_info_interval.n_intervals"],
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertFalse(app.opcua_enabled_flag)


if __name__ == "__main__":
    unittest.main()
