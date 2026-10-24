"""AI on each channel card: required setup, brain/gear/bell icons and the plain-language explanation."""

import base64
import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import app
from vds_dashboard import ai_channel, historian

FINDING = {"gateway_id": "GW1", "channel": "1", "kind": "spectrum", "severity": "warning", "since_ms": 1_790_000_000_000,
           "fault_id": "bearing_outer_race", "evidence": "GW1_1_spectrum",
           "title": "Pump P-1 (GW1 CH1): likely Bearing outer race defect (6205-2RS)",
           "message": "Pump P-1 (GW1 CH1): spectrum changed from learned normal.",
           "detail": {"rpm": 1485, "peaks": [[88.75, 14.2]], "floor_db": {"all": 0.5, "upper": 4.0},
                      "matches": [{"id": "bearing_outer_race", "name": "Bearing outer race defect", "confidence": 0.86,
                                   "evidence": ["peak at 3.59x running speed matches BPFO of 6205-2RS"],
                                   "advice": "Check the bearing at the next stop and plan a replacement."},
                                  {"id": "looseness", "name": "Mechanical looseness", "confidence": 0.4,
                                   "evidence": [], "advice": ""}]}}


class SetupRuleTests(unittest.TestCase):
    def test_iso_class_from_power_and_foundation(self):
        self.assertEqual(historian.iso_class_for(7.5, "rigid"), "class1_small")
        self.assertEqual(historian.iso_class_for(22, "rigid"), "group2_rigid")
        self.assertEqual(historian.iso_class_for(300, "flexible"), "group2_flexible")
        self.assertEqual(historian.iso_class_for(450, "rigid"), "group1_rigid")
        self.assertEqual(historian.iso_class_for(None, "rigid"), "")

    def test_ai_needs_machine_type_iso_class_and_speed(self):
        self.assertEqual(historian.ai_setup_missing({}, None), [
            "machine type", "machine power and foundation (for the ISO class)",
            "running speed: the VDS reports none, so enter a fixed RPM"])
        ready = {"machine_type": "pump", "iso_class": "group2_rigid"}
        self.assertEqual(historian.ai_setup_missing(ready, 1480), [])
        self.assertEqual(historian.ai_setup_missing(dict(ready, speed_source="fixed"), 1480), ["fixed speed in RPM"])
        self.assertEqual(historian.ai_setup_missing(dict(ready, speed_source="fixed", fixed_rpm=990), None), [])

    def test_channel_status(self):
        ready = {"machine_type": "pump", "iso_class": "group2_rigid"}
        self.assertEqual(ai_channel.channel_status(ready, False, 1480, None, None)[0], "off")
        self.assertEqual(ai_channel.channel_status({"ai_on": False, **ready}, True, 1480, None, None)[0], "off")
        state, label, tip = ai_channel.channel_status({}, True, None, None, None)
        self.assertEqual((state, label), ("setup", "Needs setup"))
        self.assertIn("machine type", tip)
        learning = {"phase": "learning", "samples": 12, "needed_samples": 48}
        self.assertEqual(ai_channel.channel_status(ready, True, 1480, learning, None)[1], "Learning 25%")
        self.assertEqual(ai_channel.channel_status(ready, True, 1480, {"phase": "monitoring"}, None)[0], "watching")
        state, label, _tip = ai_channel.channel_status(ready, True, 1480, {"phase": "monitoring"}, [FINDING])
        self.assertEqual((state, label), ("finding-warning", "Bearing outer race defect (6205-2RS)"))

    def test_plain_words(self):
        self.assertEqual(ai_channel.headline(FINDING, "6205-2RS"), "The AI thinks the outer ring (outer race) of bearing "
                         "6205-2RS is starting to wear: very likely (86%).")
        lines = ai_channel.what_changed(FINDING)
        self.assertIn("A peak at 88.8 Hz (5,325 CPM, 3.59x running speed) is 14 dB above normal", lines[0])
        self.assertIn("about 5 times stronger than usual", lines[0])
        self.assertIn("background noise at high frequency rose 4 dB", lines[1])


class ChannelCardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for target, value in ((app, "DB_FILE"), (app, "DATA_FOLDER")):
            patcher = patch.object(target, value, os.path.join(self.directory.name, "test.db")
                                   if value == "DB_FILE" else self.directory.name)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"VDS_EDGE_AI": "1"})
        env.start()
        self.addCleanup(env.stop)
        app.setup_database()
        app.data_storage.clear()
        app.data_storage["GW1"] = {"rpm": None, "channels": {"1": {"rpm": 1485}, "2": {}}}
        self.addCleanup(app.data_storage.clear)
        self.client = app.app.server.test_client()
        self.ai_folder = os.path.join(self.directory.name, "ai")
        os.makedirs(os.path.join(self.ai_folder, "evidence"))

    def write_report(self, findings=(), progress=()):
        with open(os.path.join(self.ai_folder, "findings.json"), "w", encoding="utf-8") as stream:
            json.dump({"generated_at": time.time(), "findings": list(findings), "progress": list(progress)}, stream)

    def request(self, name, inputs, state=None, viewer=False, changed=None):
        key, callback = next((k, v) for k, v in app.app.callback_map.items()
                             if getattr(v.get("callback"), "__name__", "") == name)
        outputs = callback["output"]
        encode = lambda o: {"id": o.component_id, "property": o.component_property}
        headers = {"Authorization": "Basic " + base64.b64encode(b"viewer:v-test").decode()} if viewer else {}
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "a-test", "VDS_VIEWER_PASSWORD": "v-test"} if viewer
                        else {"VDS_ADMIN_PASSWORD": ""}):
            response = self.client.post("/_dash-update-component", headers=headers, json={
                "output": key, "outputs": [encode(o) for o in outputs] if isinstance(outputs, list) else encode(outputs),
                "inputs": inputs, "state": state or [], "changedPropIds": changed or []})
        return response

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        return json.loads(response.get_data(as_text=True))["response"]

    def click(self, kind, ch="1"):
        ident = {"type": kind, "gid": "GW1", "ch": ch}
        return [[{"id": ident, "property": "n_clicks", "value": 1}]], [json.dumps(ident, separators=(",", ":"),
                                                                                sort_keys=True) + ".n_clicks"]

    def test_cards_show_brain_gear_and_a_flashing_bell_for_a_finding(self):
        self.write_report([FINDING], [{"gateway_id": "GW1", "channel": "2", "phase": "needs setup",
                                       "missing": ["machine type"]}])
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_ai_gateway(conn, "GW1", True)
        with closing(app.get_db_connection()) as conn:
            tools = ai_channel.channel_tools(self.directory.name, app.data_storage, conn)
        one, two = json.dumps(tools[("GW1", "1")].to_plotly_json(), default=str), \
            json.dumps(tools[("GW1", "2")].to_plotly_json(), default=str)
        for text in ("fa-brain", "ai-setup-open", "ai-bell", "flash", "Bearing outer race defect"):
            self.assertIn(text, one)
        self.assertIn("Needs setup", two)
        self.assertNotIn("ai-bell", two)

    def test_setup_window_saves_iso_class_and_turns_the_ai_on(self):
        inputs, changed = self.click("ai-setup-open")
        opened = self.ok(self.request("ai_setup_open", inputs, changed=changed))
        self.assertTrue(opened["ai-setup-modal"]["is_open"])
        self.assertIn("The AI cannot start yet", json.dumps(opened["ai-setup-status"]))
        self.assertIn("The VDS reports 1485 RPM now", json.dumps(opened["aim-speed-hint"]))
        values = {"machine": "Pump P-1", "location": "Motor DE", "machine_type": "pump", "power_kw": 22,
                  "foundation": "rigid", "speed_source": "vds", "fixed_rpm": None, "bearing": "6205-2RS",
                  "direction": "horizontal", "line_frequency_hz": 50, "gear_teeth": None, "blades": 6}
        state = [{"id": "ai-setup-target", "property": "data", "value": {"gid": "GW1", "ch": "1"}},
                 {"id": "aim-ai_on", "property": "value", "value": True}] + \
                [{"id": "aim-" + key, "property": "value", "value": values[key]} for key in ai_channel.FIELDS]
        saved = self.ok(self.request("ai_setup_save", [{"id": "aim-save", "property": "n_clicks", "value": 1}], state))
        self.assertIn("ISO 20816-3 group 2, rigid foundation", json.dumps(saved["aim-result"]))
        with closing(app.get_db_connection()) as conn:
            settings = historian.ai_channel_settings(conn, "GW1", "1")
            self.assertEqual((settings["iso_class"], settings["power_kw"]), ("group2_rigid", 22))
            self.assertTrue(historian.ai_enabled(conn, "GW1"))
        preview = self.ok(self.request("ai_setup_iso", [{"id": "aim-machine_type", "property": "value", "value": "pump"},
                                                        {"id": "aim-power_kw", "property": "value", "value": 22},
                                                        {"id": "aim-foundation", "property": "value", "value": "rigid"}]))
        self.assertIn("2.8", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(self.request("ai_setup_save", [{"id": "aim-save", "property": "n_clicks", "value": 1}], state,
                                      viewer=True).status_code, 403)

    def test_bell_explains_the_finding_with_a_picture(self):
        self.write_report([FINDING])
        freqs = [i * 1.25 for i in range(400)]
        normal = [-40.0] * 400
        current = list(normal)
        current[71] = -25.8  # 88.75 Hz
        with open(os.path.join(self.ai_folder, "evidence", "GW1_1_spectrum.json"), "w", encoding="utf-8") as stream:
            json.dump({"freqs": freqs, "current_db": current, "normal_db": normal, "peaks": [[88.75, 14.2]],
                       "lines": [{"name": "Outer race (BPFO)", "hz": 88.7, "order": 3.58, "used_for": "x"}]}, stream)
        inputs, changed = self.click("ai-bell")
        body = self.ok(self.request("ai_explain_open", inputs, changed=changed, viewer=True))
        self.assertTrue(body["ai-explain-modal"]["is_open"])
        text = json.dumps(body["ai-explain-body"], ensure_ascii=False)
        for words in ("Act soon", "outer ring (outer race)", "What the AI saw", "Why it thinks so", "What to do",
                      "+14 dB", "Normal (learned)", "Outer race (BPFO)", "Mechanical looseness"):
            self.assertIn(words, text)
        self.assertIn('"disabled": true', text)  # viewers see the feedback buttons but cannot press them

    def test_feedback_from_the_explanation_is_stored(self):
        key = json.dumps(["GW1", "1", "spectrum", FINDING["since_ms"], "bearing_outer_race"])
        ident = {"type": "ai-explain-feedback", "verdict": "confirmed", "key": key}
        result = self.ok(self.request("ai_explain_feedback", [[{"id": ident, "property": "n_clicks", "value": 1}]],
                                      changed=[json.dumps(ident, separators=(",", ":"), sort_keys=True) + ".n_clicks"]))
        self.assertIn("Thank you", json.dumps(result))
        with closing(app.get_db_connection()) as conn:
            self.assertEqual(conn.execute("SELECT verdict FROM ai_feedback").fetchall(), [("confirmed",)])


if __name__ == "__main__":
    unittest.main()
