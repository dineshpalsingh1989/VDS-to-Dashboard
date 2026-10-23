import base64
import io
import json
import os
import pickle
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

    def test_ai_page_viewer_reads_but_only_admin_changes_ai_settings(self):
        from vds_dashboard import ai_views, historian

        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO channel_state VALUES (?,?,?)", ("g", "1", json.dumps({"rpm": 1480})))
        auth = lambda user: {"Authorization": "Basic " + base64.b64encode(user.encode()).decode()}  # noqa: E731
        viewer, admin = auth("viewer:viewer-test"), auth("admin:admin-test")
        channel = {"id": "ai-channel", "property": "value", "value": json.dumps(["g", "1"])}
        form = {"machine": "Fan F-1", "location": "Motor DE", "machine_type": "motor", "direction": "radial",
                "bearing": "6205-2RS", "speed_source": "vds", "fixed_rpm": None, "line_frequency_hz": 50,
                "gear_teeth": None, "blades": None, "iso_class": "group2_rigid"}
        state = [channel] + [{"id": "ai-" + k, "property": "value", "value": form[k]} for k in ai_views.FORM_KEYS]
        switch = {"id": {"type": "ai-gw-switch", "gid": "g"}, "property": "value", "value": True}
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "admin-test", "VDS_VIEWER_PASSWORD": "viewer-test",
                                     "VDS_EDGE_AI": "1"}):
            response = self.callback_request("ai_load_channel", [channel], headers=viewer)
            self.assertEqual(response.status_code, 200)
            self.assertIn("reports 1480 RPM", response.get_data(as_text=True))
            rendered = self.callback_request("ai_gateway_list", [
                {"id": "url", "property": "pathname", "value": "/ai"},
                {"id": "ai-gateway-result", "property": "children", "value": None}], headers=viewer)
            self.assertIn('"disabled":true', rendered.get_data(as_text=True).replace(" ", ""))
            save = [{"id": "ai-save", "property": "n_clicks", "value": 1}]
            self.assertEqual(self.callback_request("ai_save_channel", save, state, headers=viewer).status_code, 403)
            toggle = ([[switch]], None, ['{"gid":"g","type":"ai-gw-switch"}.value'])
            self.assertEqual(self.callback_request("ai_toggle_gateway", toggle[0], headers=viewer,
                                                   changed=toggle[2]).status_code, 403)
            self.assertEqual(self.callback_request("ai_save_channel", save, state, headers=admin).status_code, 200)
            response = self.callback_request("ai_toggle_gateway", toggle[0], headers=admin, changed=toggle[2])
            self.assertIn("AI analysis for g is on", response.get_data(as_text=True))
        with closing(app.get_db_connection()) as conn:
            self.assertTrue(historian.ai_enabled(conn, "g"))
            self.assertEqual(historian.ai_channel_settings(conn, "g", "1")["bearing"], "6205-2RS")

    def test_ai_page_exists_only_on_iriv_edge_installs(self):
        route = [{"id": "url", "property": "pathname", "value": "/ai"}]
        switch = [[{"id": {"type": "ai-gw-switch", "gid": "g"}, "property": "value", "value": True}]]
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "0"}):
            self.assertNotIn("ai-gateways", self.callback_request("display_page", route).get_data(as_text=True))
            refused = self.callback_request("ai_toggle_gateway", switch,
                                            changed=['{"gid":"g","type":"ai-gw-switch"}.value'])
            self.assertIn("only available on IRIV Edge AI", refused.get_data(as_text=True))
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "", "VDS_EDGE_AI": "1"}):
            self.assertIn("ai-gateways", self.callback_request("display_page", route).get_data(as_text=True))

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

    def test_diagnostics_read_callbacks_and_recorded_selection(self):
        import numpy as np
        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO raw_spectrum (gateway_id,channel,timestamp,spectrum_data,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?)",
                         ("g", "1", "2026-01-01 00:00:00", np.array([0, 3, 0], dtype="<f8").tobytes(), 40, 2,
                          json.dumps(dict(bandwidth=40, lines=2, timestamp_ms=1767225600000))))
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "admin-test", "VDS_VIEWER_PASSWORD": "viewer-test"}):
            token = base64.b64encode(b"viewer:viewer-test").decode()
            headers = {"Authorization": "Basic " + token}
            response = self.callback_request("render_diagnostics", [
                {"id": "diag-channel", "property": "value", "value": json.dumps(["g", "1"])},
                {"id": "diag-orders", "property": "value", "value": ["orders"]},
                {"id": "diag-rpm", "property": "value", "value": 1200},
                {"id": "diag-capture", "property": "value", "value": "2026-01-01 00:00:00"},
                {"id": "live-update-trigger", "property": "data", "value": 1}], headers=headers)
            self.assertEqual(response.status_code, 200)
            result = response.json["response"]
            self.assertEqual(result["diag-peaks"]["data"][0]["frequency"], 20)
            self.assertEqual(result["diag-peaks"]["data"][0]["order"], 1)
            self.assertFalse(result["diag-waveform"]["figure"].get("data"))
            response = self.callback_request("inspect_spectrum_point", [
                {"id": "diag-spectrum", "property": "clickData", "value": {"points": [{"customdata": [20, 1], "y": 3}]}},
                {"id": "diag-spectrum", "property": "figure", "value": {}}],
                headers=headers, changed=["diag-spectrum.clickData"])
            self.assertEqual(response.status_code, 200)
            self.assertIn("20.000 Hz", response.json["response"]["diag-cursor"]["children"])

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
