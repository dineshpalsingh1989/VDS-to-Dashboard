"""Home redraws only when something changed (it used to redraw every card every 2 s)."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class LiveRefreshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, value in (("DB_FILE", os.path.join(folder.name, "test.db")), ("DATA_FOLDER", folder.name)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.setup_database()
        app.data_storage.clear()
        self.addCleanup(app.data_storage.clear)
        self.client = app.app.server.test_client()

    def tick(self, n, previous):
        key = next(k for k, v in app.app.callback_map.items()
                   if getattr(v.get("callback"), "__name__", "") == "refresh_live_data")
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": ""}):
            response = self.client.post("/_dash-update-component", json={
                "output": "live-update-trigger.data", "outputs": {"id": "live-update-trigger", "property": "data"},
                "inputs": [{"id": "live-refresh", "property": "n_intervals", "value": n}],
                "state": [{"id": "live-update-trigger", "property": "data", "value": previous}],
                "changedPropIds": ["live-refresh.n_intervals"]})
        self.assertIn(response.status_code, (200, 204), key)
        body = None if response.status_code == 204 else json.loads(response.get_data(as_text=True)).get("response")
        return body or None  # Dash answers no_update with an empty response

    @patch("time.time", return_value=1_790_000_000.0)  # keep the 30 s redraw bucket fixed
    def test_redraws_only_on_new_data(self, _clock):
        app.data_storage["GW1"] = {"timestamp_ms": 1000, "channels": {"1": {}}}
        first = self.tick(1, None)["live-update-trigger"]["data"]
        self.assertIsNone(self.tick(2, first), "nothing changed: no redraw")
        app.data_storage["GW1"]["timestamp_ms"] = 2000
        self.assertIsNotNone(self.tick(3, first), "a new packet: redraw")


if __name__ == "__main__":
    unittest.main()
