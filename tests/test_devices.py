"""VDS130 page: a new VDS waits for approval; nothing it sends is stored until it is allowed."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import app
from vds_dashboard import devices_view, historian


def message(gid, value=1.5):
    return SimpleNamespace(payload=json.dumps({
        "gateway_id": gid, "sensor_id": f"{gid}_1", "timestamp": 1_790_000_000_000,
        "process_data": {"RPM": 1480, "SensorType": "Acceleration(g)",
                         "Velocity": {"value": value, "unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS"}}}).encode())


class DeviceApprovalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = os.path.join(folder.name, "test.db")
        patcher = patch.object(app, "DB_FILE", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.setup_database()
        app.data_storage.clear()
        self.addCleanup(app.data_storage.clear)
        self.client = app.app.server.test_client()

    def rows(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_new_vds_waits_until_allowed(self):
        app.on_message(None, None, message("VDS130-NEW"))
        self.assertEqual(app.data_storage, {})
        self.assertEqual(self.rows("trends_latest"), 0)
        with closing(app.get_db_connection()) as conn:
            [device] = historian.devices(conn)
            self.assertEqual((device["status"], device["info"]["channels"], device["info"]["rpm"]),
                             ("pending", ["1"], 1480))
            banner = json.dumps(devices_view.pending_banner(conn).to_plotly_json(), default=str)
            self.assertIn("New VDS130 found: VDS130-NEW", banner)
        self.request_action("VDS130-NEW", "allowed")
        app.on_message(None, None, message("VDS130-NEW"))
        self.assertIn("VDS130-NEW", app.data_storage)
        self.assertEqual(self.rows("trends_latest"), 1)

    def test_blocked_vds_is_ignored_and_viewers_cannot_allow(self):
        with closing(app.get_db_connection()) as conn, conn:
            historian.set_device_status(conn, "VDS130-BAD", "blocked")
        app.on_message(None, None, message("VDS130-BAD"))
        self.assertEqual(app.data_storage, {})
        with closing(app.get_db_connection()) as conn:
            self.assertEqual(historian.device_status(conn, "VDS130-BAD"), "blocked")
        response = self.request_action("VDS130-BAD", "allowed", viewer=True)
        self.assertEqual(response.status_code, 403)

    def test_units_that_already_sent_data_keep_running(self):
        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("DELETE FROM devices")
            conn.execute("INSERT INTO channel_state VALUES ('OLD-VDS', '1', '{}')")
            historian.setup_schema(conn)  # what an update does on start-up
            self.assertEqual(historian.device_status(conn, "OLD-VDS"), "allowed")

    def test_page_lists_waiting_and_allowed_units(self):
        app.on_message(None, None, message("VDS130-NEW"))
        with closing(app.get_db_connection()) as conn, conn:
            historian.set_device_status(conn, "VDS130-OK", "allowed")
        key = next(k for k, v in app.app.callback_map.items()
                   if getattr(v.get("callback"), "__name__", "") == "devices_list")
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            response = self.client.post("/_dash-update-component", json={
                "output": key, "outputs": [{"id": "devices-pending", "property": "children"},
                                           {"id": "devices-table", "property": "children"}],
                "inputs": [{"id": "devices-interval", "property": "n_intervals", "value": 1},
                           {"id": "devices-result", "property": "children", "value": None}],
                "changedPropIds": ["devices-interval.n_intervals"]})
        body = response.get_data(as_text=True)
        for text in ("New VDS130 waiting for approval", "VDS130-NEW", "Allow", "VDS130-OK", "Offline", "Block"):
            self.assertIn(text, body)

    def request_action(self, gid, action, viewer=False):
        ident = {"type": "device-action", "gid": gid, "action": action}
        key = next(k for k, v in app.app.callback_map.items()
                   if getattr(v.get("callback"), "__name__", "") == "device_action")
        env = {"VDS_ADMIN_PASSWORD": "a-test", "VDS_VIEWER_PASSWORD": "v-test"} if viewer else {"VDS_ADMIN_PASSWORD": ""}
        headers = {"Authorization": "Basic dmlld2VyOnYtdGVzdA=="} if viewer else {}
        with patch.dict(os.environ, env):
            return self.client.post("/_dash-update-component", headers=headers, json={
                "output": key, "outputs": {"id": "devices-result", "property": "children"},
                "inputs": [[{"id": ident, "property": "n_clicks", "value": 1}]],
                "changedPropIds": [json.dumps(ident, separators=(",", ":"), sort_keys=True) + ".n_clicks"]})


if __name__ == "__main__":
    unittest.main()
