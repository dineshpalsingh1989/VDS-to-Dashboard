"""Settings > Database & files shows where data lives, how big it is and how long it is kept."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class StoragePageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        for name, value in (("DB_FILE", os.path.join(folder.name, "test.db")), ("DATA_FOLDER", folder.name)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.setup_database()
        self.client = app.app.server.test_client()

    def render(self, ai="0"):
        key = next(k for k, v in app.app.callback_map.items()
                   if getattr(v.get("callback"), "__name__", "") == "display_page")
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "a", "VDS_VIEWER_PASSWORD": "v", "VDS_EDGE_AI": ai}):
            response = self.client.post("/_dash-update-component", headers={"Authorization": "Basic dmlld2VyOnY="}, json={
                "output": key, "outputs": {"id": "page-content", "property": "children"},
                "inputs": [{"id": "url", "property": "pathname", "value": "/database"}],
                "changedPropIds": ["url.pathname"]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:200])
        return response.get_data(as_text=True)

    def test_viewer_sees_database_tables_retention_and_folders(self):
        body = self.render()
        for text in ("Database &amp; files", "SQLite, journal mode WAL", "snapshots", "How long data is kept",
                     "kept 90 days", "Backups", "Daily reports"):
            self.assertIn(text.replace("&amp;", "\u0026"), body.replace("&amp;", "\u0026"))
        self.assertNotIn("AI files", body)

    def test_ai_files_on_iriv(self):
        os.makedirs(os.path.join(self.folder, "ai"))
        with open(os.path.join(self.folder, "ai", "findings.json"), "w", encoding="utf-8") as stream:
            json.dump({"generated_at": 0, "neural": "Neural model running on the CPU.",
                       "storage": {"path": os.path.join(self.folder, "ai"), "message": "AI data is on the eMMC."}}, stream)
        body = self.render(ai="1")
        for text in ("AI files (IRIV Edge AI)", "learning.db", "Neural model running on the CPU.", "vibration_brain.npz"):
            self.assertIn(text, body)


if __name__ == "__main__":
    unittest.main()
