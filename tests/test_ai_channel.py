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
           "detail": {"rpm": 1485, "peaks": [[88.75, 14.2]], "floor_db": {"all": 0.5, "upper": 4.0}, "unit": "mm/s",
                      "levels": [{"hz": 88.75, "now": 2.6, "normal": 0.42}],
                      "brain": {"backend": "hailo", "where": "Hailo AI HAT+", "agrees": True, "bearing": 0.97,
                                "top": [["outer_race", 0.91], ["rolling_element", 0.05], ["normal", 0.02]],
                                "names": ["Bearing outer race defect", "Bearing rolling element defect", "Normal running"]},
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
        self.assertIn("At 88.8 Hz (5,325 CPM, 3.59x running speed) the vibration went from 0.42 to 2.6 mm/s", lines[0])
        self.assertIn("about 6 times normal", lines[0])
        envelope = dict(FINDING, detail=dict(FINDING["detail"], unit="", levels=[{"hz": 88.75, "now": 32, "normal": 3.5}]))
        self.assertIn("the bearing impact level is 9.1 times its normal level", ai_channel.what_changed(envelope)[0])
        old = dict(FINDING, detail=dict(FINDING["detail"], levels=[]))
        self.assertIn("14 dB above normal. That is about 5 times stronger than usual", ai_channel.what_changed(old)[0])
        self.assertIn("background noise at high frequency rose 4 dB", lines[1])


class FaultLineTests(unittest.TestCase):
    def test_fault_lines_follow_the_part(self):
        from vds_dashboard.fault_frequencies import families_for, marker_lines
        self.assertEqual(families_for("motor"), [("speed", "Running speed"), ("bearing", "Bearing"),
                                                 ("electrical", "Electrical")])
        self.assertIn(("gear", "Gear mesh"), families_for("gearbox"))
        self.assertIn(("gear", "Vane pass"), families_for("pump"))
        self.assertNotIn("electrical", [key for key, _label in families_for("fan")])
        self.assertEqual(len(families_for(None)), 4)  # part not set: every family
        info = {"gear_teeth": 23, "blades": 6, "line_frequency_hz": 50}
        every = {"speed", "bearing", "gear", "electrical"}
        motor = {key for key, *_ in marker_lines(dict(info, machine_type="motor"), 1480, every, 1, 2000)}
        self.assertEqual(motor, {"speed", "electrical"})
        gearbox = {key for key, *_ in marker_lines(dict(info, machine_type="gearbox"), 1480, every, 1, 2000)}
        self.assertEqual(gearbox, {"speed", "gear"})


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
                  "direction": "horizontal", "line_frequency_hz": 50, "gear_teeth": 23, "blades": 6, "end": "DE"}
        state = [{"id": "ai-setup-target", "property": "data", "value": {"gid": "GW1", "ch": "1"}},
                 {"id": "aim-ai_on", "property": "value", "value": True}] + \
                [{"id": "aim-" + key, "property": "value", "value": values[key]} for key in ai_channel.FIELDS]
        saved = self.ok(self.request("ai_setup_save", [{"id": "aim-save", "property": "n_clicks", "value": 1}], state))
        self.assertIn("ISO 20816-3 group 2, rigid foundation", json.dumps(saved["aim-result"]))
        with closing(app.get_db_connection()) as conn:
            settings = historian.ai_channel_settings(conn, "GW1", "1")
            self.assertEqual((settings["iso_class"], settings["power_kw"]), ("group2_rigid", 22))
            self.assertEqual((settings["end"], settings["blades"], settings["gear_teeth"]), ("DE", 6, None))
            self.assertTrue(historian.ai_enabled(conn, "GW1"))
        preview = self.ok(self.request("ai_setup_iso", [{"id": "aim-machine_type", "property": "value", "value": "pump"},
                                                        {"id": "aim-power_kw", "property": "value", "value": 22},
                                                        {"id": "aim-foundation", "property": "value", "value": "rigid"}]))
        self.assertIn("2.8", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(self.request("ai_setup_save", [{"id": "aim-save", "property": "n_clicks", "value": 1}], state,
                                      viewer=True).status_code, 403)

    def test_fields_follow_the_part_and_the_ai_switch_waits_for_the_setup(self):
        hidden = {"display": "none"}
        styles = lambda part: self.ok(self.request("ai_setup_fields", [
            {"id": "aim-machine_type", "property": "value", "value": part}]))
        self.assertEqual((styles("gearbox")["aim-gear-wrap"]["style"], styles("gearbox")["aim-blades-wrap"]["style"]),
                         ({}, hidden))
        self.assertEqual((styles("pump")["aim-gear-wrap"]["style"], styles("pump")["aim-blades-wrap"]["style"]),
                         (hidden, {}))
        self.assertEqual(styles("motor")["aim-blades-wrap"]["style"], hidden)

        def ready(part, power, foundation, source="vds", rpm_value=None, ch="2"):
            inputs = [{"id": "aim-" + k, "property": "value", "value": v} for k, v in (
                ("machine_type", part), ("power_kw", power), ("foundation", foundation), ("speed_source", source),
                ("fixed_rpm", rpm_value))]
            return self.ok(self.request("ai_setup_ready", inputs, [{"id": "ai-setup-target", "property": "data",
                                                                    "value": {"gid": "GW1", "ch": ch}}],
                                        changed=["aim-power_kw.value"]))
        locked = ready("pump", None, "rigid")
        self.assertTrue(locked["aim-ai_on"]["disabled"])
        self.assertFalse(locked["aim-ai_on"]["value"])
        self.assertIn("the power (kW)", json.dumps(locked["aim-ai-hint"]))
        self.assertIn("running speed", json.dumps(locked["aim-ai-hint"]))  # CH2 reports no speed
        self.assertFalse(ready("pump", 22, "rigid", "fixed", 1480)["aim-ai_on"]["disabled"])
        self.assertFalse(ready("pump", 22, "rigid", ch="1")["aim-ai_on"]["disabled"])  # CH1 reports 1485 RPM

    def test_save_keeps_the_ai_off_while_details_are_missing(self):
        values = dict.fromkeys(ai_channel.FIELDS)
        values.update(machine_type="pump", speed_source="vds", line_frequency_hz=50)
        state = [{"id": "ai-setup-target", "property": "data", "value": {"gid": "GW1", "ch": "1"}},
                 {"id": "aim-ai_on", "property": "value", "value": True}] + \
                [{"id": "aim-" + key, "property": "value", "value": values[key]} for key in ai_channel.FIELDS]
        saved = self.ok(self.request("ai_setup_save", [{"id": "aim-save", "property": "n_clicks", "value": 1}], state))
        self.assertIn("stays off", json.dumps(saved["aim-result"]))
        with closing(app.get_db_connection()) as conn:
            self.assertFalse(historian.ai_channel_settings(conn, "GW1", "1")["ai_on"])
            self.assertFalse(historian.ai_enabled(conn, "GW1"))

    def test_machine_drawing_places_each_channel(self):
        rows = {("GW1", "1"): {"machine": "Pump P-1", "machine_type": "motor", "end": "NDE", "direction": "horizontal"},
                ("GW1", "2"): {"machine": "pump p-1", "machine_type": "motor", "end": "DE", "direction": "horizontal"},
                ("GW1", "4"): {"machine": "Other", "machine_type": "fan", "end": "DE", "direction": "axial"}}
        drawing = json.dumps(ai_channel.machine_drawing(rows, ("GW1", "3"), {
            "machine": "Pump P-1", "machine_type": "pump", "end": "DE", "direction": "axial"}).to_plotly_json(), default=str)
        for text in ("CH1 H", "CH2 H", "CH3 A", "Motor", "Pump", "current"):
            self.assertIn(text, drawing)
        self.assertNotIn("CH4", drawing)  # another machine

    def test_spectrum_popup_offers_only_the_parts_fault_lines(self):
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_ai_channel(conn, "GW1", "1", {"machine_type": "motor"})
        target = {"id": "modal-target", "property": "data", "value": {"gid": "GW1", "ch": "1", "type": "spec"}}
        options = self.ok(self.request("ai_modal_families", [target], viewer=True))["modal-families"]["options"]
        self.assertEqual([o["value"] for o in options], ["speed", "bearing", "electrical"])

    def test_bell_explains_the_finding_with_a_picture(self):
        self.write_report([FINDING])
        freqs = [i * 1.25 for i in range(400)]
        normal = [-40.0] * 400
        current = list(normal)
        current[71] = -25.8  # 88.75 Hz
        with open(os.path.join(self.ai_folder, "evidence", "GW1_1_spectrum.json"), "w", encoding="utf-8") as stream:
            json.dump({"freqs": freqs, "current_db": current, "normal_db": normal, "peaks": [[88.75, 14.2]],
                       "now": [0.42] * 71 + [2.6] + [0.42] * 328, "normal_level": [0.42] * 400, "unit": "mm/s",
                       "axis": "Velocity (mm/s, from the acceleration spectrum)",
                       "lines": [{"name": "Outer race (BPFO)", "hz": 88.7, "order": 3.58, "used_for": "x"}]}, stream)
        inputs, changed = self.click("ai-bell")
        body = self.ok(self.request("ai_explain_open", inputs, changed=changed, viewer=True))
        self.assertTrue(body["ai-explain-modal"]["is_open"])
        text = json.dumps(body["ai-explain-body"], ensure_ascii=False)
        for words in ("Act soon", "outer ring (outer race)", "What the AI saw", "Why it thinks so", "What to do",
                      "0.42 → 2.6 mm/s", "Velocity (mm/s", "Normal (learned)", "Outer race (BPFO)",
                      "Mechanical looseness", "Second opinion: neural network",
                      "The neural network on the Hailo AI HAT+ says: Bearing outer race defect 91%",
                      "It agrees with the diagnosis above."):
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
