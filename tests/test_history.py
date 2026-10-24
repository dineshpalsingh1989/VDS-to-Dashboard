"""Machine history page: snapshots, nearest capture, trend clicks and the capture viewer."""

import base64
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch

import numpy as np

import app
from vds_dashboard import historian
from vds_dashboard.signals import envelope_spectrum, velocity_from_acceleration, waveform_stats

START = 1_790_000_000  # seconds


def packet(received_at, timestamp_ms, fs=4096, n=4096, sensor="Acceleration(g)"):
    t = np.arange(n) / fs
    return {"gateway_id": "GW1", "channel": "1", "timestamp_ms": timestamp_ms, "received_at": received_at,
            "rpm": 1480, "sampling_freq": fs, "bandwidth": 1000, "lines": 800, "sensor_type": sensor,
            "spectrum_data": (1e-4 + np.where(np.arange(801) == 20, 1e-2, 0)).tolist(),
            "waveform_data": np.sin(2 * np.pi * 25 * t).tolist()}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        historian.setup_schema(self.conn)
        self.conn.executescript("""
            CREATE TABLE raw_spectrum (gateway_id TEXT, channel TEXT, timestamp DATETIME, spectrum_data BLOB,
                bandwidth REAL, lines INTEGER, context TEXT);
            CREATE TABLE raw_waveforms (gateway_id TEXT, channel TEXT, timestamp DATETIME, waveform_data BLOB,
                sampling_freq INTEGER, context TEXT);""")

    def store(self, seconds, interval=1800, days=90):
        item = packet(START + seconds, (START + seconds) * 1000)
        context = json.dumps({"sensor_type": item["sensor_type"], "sampling_freq": item["sampling_freq"]})
        historian.store_snapshots(self.conn, item, context, interval, days)

    def count(self):
        return self.conn.execute("SELECT kind, COUNT(*) FROM snapshots GROUP BY kind").fetchall()

    def test_one_snapshot_per_interval_and_retention(self):
        for seconds in (0, 600, 1200, 1799):
            self.store(seconds)
        self.assertEqual(self.count(), [("spectrum", 1), ("waveform", 1)])
        self.store(1800)
        self.assertEqual(self.count(), [("spectrum", 2), ("waveform", 2)])
        self.store(3 * 86400, days=1)  # older than one day is trimmed for this channel
        self.assertEqual(self.count(), [("spectrum", 1), ("waveform", 1)])

    def test_defaults_are_30_minutes_and_90_days(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VDS_SNAPSHOT_INTERVAL_S", None)
            os.environ.pop("VDS_SNAPSHOT_RETENTION_DAYS", None)
            self.assertEqual((historian.snapshot_interval(), historian.snapshot_retention_days()), (1800, 90))

    def test_nearest_capture_uses_snapshots_and_recordings(self):
        self.store(0)
        self.store(3600)
        recorded = (START + 5400) * 1000
        self.conn.execute("INSERT INTO raw_spectrum VALUES (?,?,?,?,?,?,?)", (
            "GW1", "1", datetime.fromtimestamp(recorded / 1000), np.full(801, 2e-4).tobytes(), 1000, 800,
            json.dumps({"timestamp_ms": recorded})))
        near = lambda at: historian.nearest_capture(self.conn, "GW1", "1", "spectrum", at)  # noqa: E731
        self.assertEqual(near((START + 1000) * 1000)["timestamp_ms"], START * 1000)
        self.assertEqual(near((START + 3000) * 1000)["timestamp_ms"], (START + 3600) * 1000)
        self.assertEqual(near((START + 5000) * 1000)["source"], "recording")
        self.assertEqual(near(None)["timestamp_ms"], recorded, "no click means the latest capture")
        self.assertEqual(historian.capture_times(self.conn, "GW1", "1", START * 1000, recorded),
                         [START * 1000, (START + 3600) * 1000, recorded])
        self.assertIsNone(historian.nearest_capture(self.conn, "GW9", "1", "spectrum", None))


class SignalTests(unittest.TestCase):
    def test_velocity_from_acceleration(self):
        freqs = np.array([0.0, 1.0, 25.0, 100.0])
        velocity = velocity_from_acceleration(freqs, np.array([1.0, 1.0, 1e-4, 1e-4]))
        self.assertEqual(velocity[0], 0)  # no integration below 2 Hz
        self.assertAlmostEqual(velocity[2], 0.01 * 9806.65 / (2 * np.pi * 25), places=6)
        self.assertAlmostEqual(velocity[2] / velocity[3], 4.0)

    def test_envelope_and_waveform_stats(self):
        fs, n = 4096, 4096
        t = np.arange(n) / fs
        since = t % (1 / 88.4)
        x = 0.2 * np.random.default_rng(0).standard_normal(n) + np.exp(-since * 400) * np.sin(2 * np.pi * 1200 * since)
        f, p = envelope_spectrum(x, fs)
        self.assertAlmostEqual(f[np.argmax(p)], 88.4, delta=4)
        with self.assertRaises(ValueError):
            envelope_spectrum(x, 1000)
        stats = waveform_stats(np.sin(2 * np.pi * 5 * t))
        self.assertAlmostEqual(stats["crest"], np.sqrt(2), places=2)


class HistoryPageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(app, "DB_FILE", os.path.join(self.directory.name, "test.db"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app.setup_database()
        app.data_storage.clear()
        self.client = app.app.server.test_client()
        now = time.time()
        meta = json.dumps({"unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS"})
        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO channel_state VALUES (?,?,?)", ("GW1", "1", json.dumps({"rpm": 1480})))
            for i in range(48):
                stamp = datetime.fromtimestamp(now - (47 - i) * 1800)
                conn.execute("INSERT INTO trends (timestamp,gateway_id,channel,metric_name,value,rpm,context) "
                             "VALUES (?,?,?,?,?,?,?)", (stamp, "GW1", "1", "Velocity", 1 + i / 100, 1480, meta))
                if i % 8 == 0:
                    item = packet(now - (47 - i) * 1800, int((now - (47 - i) * 1800) * 1000))
                    context = json.dumps({"sensor_type": item["sensor_type"], "sampling_freq": 4096})
                    historian.store_snapshots(conn, item, context, 1)
        self.first_ms = int((now - 47 * 1800) * 1000)

    def request(self, name, inputs, state=None, viewer=False, changed=None):
        key, callback = next((k, v) for k, v in app.app.callback_map.items()
                             if getattr(v.get("callback"), "__name__", "") == name)
        outputs = callback["output"]
        encode = lambda o: {"id": o.component_id, "property": o.component_property}  # noqa: E731
        headers = {"Authorization": "Basic " + base64.b64encode(b"viewer:v-test").decode()} if viewer else {}
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "a-test", "VDS_VIEWER_PASSWORD": "v-test"} if viewer
                        else {"VDS_ADMIN_PASSWORD": ""}):
            response = self.client.post("/_dash-update-component", headers=headers, json={
                "output": key, "outputs": [encode(o) for o in outputs] if isinstance(outputs, list) else encode(outputs),
                "inputs": inputs, "state": state or [],
                "changedPropIds": changed or [inputs[0]["id"] + "." + inputs[0]["property"]]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        return json.loads(response.get_data(as_text=True))["response"]

    def channel(self):
        return {"id": "diag-channel", "property": "data", "value": json.dumps(["GW1", "1"])}

    def test_trend_shows_readings_and_saved_spectra_for_viewers(self):
        body = self.request("diag_trend", [self.channel(), {"id": "diag-metrics", "property": "value", "value": ["Velocity"]},
                                           {"id": "diag-range", "property": "value", "value": "7d"},
                                           {"id": "diag-time", "property": "data", "value": self.first_ms},
                                           {"id": "diag-trend-tools", "property": "value", "value": ["logy"]}], viewer=True)
        figure = body["diag-trend"]["figure"]
        names = [trace["name"] for trace in figure["data"]]
        self.assertEqual(names, ["Vel RMS", "Saved spectra"])
        self.assertEqual(len(figure["data"][0]["y"]), 48)
        self.assertEqual(len(figure["data"][1]["x"]), 6)
        self.assertEqual(figure["layout"]["hovermode"], "x unified")
        self.assertTrue(figure["layout"]["xaxis"]["rangeslider"]["visible"])
        self.assertEqual(figure["layout"]["yaxis"]["type"], "log")
        self.assertTrue(figure["layout"]["shapes"], "the clicked moment is marked")

    def test_click_picks_a_time_and_new_channel_resets_it(self):
        moment = datetime.fromtimestamp(self.first_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        click = {"id": "diag-trend", "property": "clickData", "value": {"points": [{"x": moment}]}}
        body = self.request("diag_pick_time", [click, self.channel()])
        self.assertEqual(body["diag-time"]["data"] // 1000, self.first_ms // 1000)
        body = self.request("diag_pick_time", [click, self.channel()], changed=["diag-channel.data"])
        self.assertIsNone(body["diag-time"]["data"])

    def test_tree_overall_table_and_picks(self):
        with closing(app.get_db_connection()) as conn, conn:
            historian.save_asset(conn, "GW1", "1", {"site": "Plant A", "machine": "Pump P-1", "location": "Motor DE",
                                                    "direction": "radial", "publish_interval": 60})
            conn.execute("INSERT INTO trends_latest (gateway_id,channel,metric_name,value,rpm,timestamp,context) "
                         "VALUES (?,?,?,?,?,?,?)", ("GW1", "1", "Velocity", 1.47, 1480, datetime.now(),
                                                    json.dumps({"unit": "mm/s"})))
        tree = self.request("diag_tree", [{"id": "diag-search", "property": "value", "value": "motor"},
                                          self.channel()], viewer=True)
        text = json.dumps(tree, ensure_ascii=False)
        self.assertIn("Pump P-1", text)
        self.assertIn("CH1 · Motor DE", text)
        self.assertIn("tree-leaf active", text)
        # Machine, then the device serial number, then its channels
        self.assertLess(text.index("Pump P-1"), text.index("fa-barcode"))
        self.assertLess(text.index("fa-barcode"), text.index("CH1 · Motor DE"))
        empty = self.request("diag_tree", [{"id": "diag-search", "property": "value", "value": "zzz"}, self.channel()])
        self.assertIn("No machine matches", json.dumps(empty))
        overview = self.request("diag_overview", [self.channel(), {"id": "diag-metrics", "property": "value",
                                                                   "value": ["Velocity"]}], viewer=True)
        crumbs = json.dumps(overview["diag-breadcrumb"])
        for part in ("Plant A", "GW1", "Pump P-1", "CH1"):
            self.assertIn(part, crumbs)
        self.assertLess(crumbs.index("Pump P-1"), crumbs.index("GW1"))
        self.assertIn("1.47", json.dumps(overview["diag-overall"]))
        pick = self.request("diag_tree_pick", [[{"id": {"type": "diag-tree", "gid": "GW1", "ch": "1"},
                                                 "property": "n_clicks", "value": 1}]],
                            changed=['{"ch":"1","gid":"GW1","type":"diag-tree"}.n_clicks'])
        self.assertEqual((pick["diag-gateway"]["data"], pick["diag-axis"]["value"]), ("GW1", "1"))
        cell = self.request("diag_cell_pick", [[{"id": {"type": "diag-cell", "ch": "1", "metric": "Acceleration"},
                                                 "property": "n_clicks", "value": 1}]],
                            [{"id": "diag-metrics", "property": "value", "value": ["Velocity"]}],
                            changed=['{"ch":"1","metric":"Acceleration","type":"diag-cell"}.n_clicks'])
        self.assertEqual(cell["diag-metrics"]["value"], ["Velocity", "Acceleration"])

    def view(self, view, quantity="acceleration", at=None, tools=("harmonics", "bearing", "peaks"), speed=None,
             unit="rpm", viewer=True, wave_tools=("revs", "impacts"), zoom="all"):
        return self.request("diag_view_capture", [
            self.channel(), {"id": "diag-time", "property": "data", "value": at},
            {"id": "diag-view", "property": "value", "value": view},
            {"id": "diag-quantity", "property": "value", "value": quantity},
            {"id": "diag-tools", "property": "value", "value": list(tools)},
            {"id": "diag-speed", "property": "value", "value": speed},
            {"id": "diag-speed-unit", "property": "value", "value": unit},
            {"id": "diag-bearing-result", "property": "children", "value": None},
            {"id": "diag-wave-tools", "property": "value", "value": list(wave_tools)},
            {"id": "diag-wave-zoom", "property": "value", "value": zoom}], viewer=viewer)

    def test_waveform_tools(self):
        body = self.view("waveform", zoom="1")
        figure = body["diag-plot"]["figure"]
        self.assertEqual(figure["layout"]["yaxis"]["title"]["text"], "Acceleration (g)")
        self.assertAlmostEqual(figure["layout"]["xaxis"]["range"][1], 60 / 1480, places=4)  # one turn
        self.assertTrue(any("1 turn = 40.5 ms" in a["text"] for a in figure["layout"]["annotations"]))
        self.assertIn("impact", json.dumps(body["diag-peaks"]))
        velocity = self.view("waveform", quantity="velocity")
        self.assertEqual(velocity["diag-plot"]["figure"]["layout"]["yaxis"]["title"]["text"], "Velocity (mm/s)")
        self.assertIn("mm/s", json.dumps(velocity["diag-rms"]))
        earlier = self.view("waveform", at=self.first_ms + 47 * 1_800_000, wave_tools=("reference",))
        self.assertIn("Earlier:", json.dumps(earlier["diag-plot"]["figure"]["data"][0]["name"]))
        orbit = self.view("waveform", wave_tools=("orbit",))
        self.assertIn("An orbit needs a horizontal and a vertical sensor", json.dumps(orbit["diag-plot"]))
        bar = self.request("diag_wave_bar", [{"id": "diag-view", "property": "value", "value": "waveform"}])
        self.assertEqual(bar["diag-wave-bar"]["style"], {})

    def test_spectrum_nearest_to_click_with_harmonics_and_cpm_axis(self):
        body = self.view("spectrum", at=self.first_ms + 60_000)
        figure = body["diag-plot"]["figure"]
        self.assertEqual(figure["layout"]["xaxis"]["title"]["text"], "f [Hz + CPM]")
        self.assertIn("6,000", " ".join(figure["layout"]["xaxis"]["ticktext"]))  # 100 Hz = 6,000 CPM
        harmonics = next(trace for trace in figure["data"] if trace.get("name") == "Harmonics")
        self.assertEqual(harmonics["marker"]["symbol"], "diamond")
        self.assertIn("1480 CPM", json.dumps(figure["layout"]["annotations"]))
        self.assertIn("1x", json.dumps(body["diag-peaks"]))
        self.assertIn("Nearest saved capture: 1 minute before", json.dumps(body["diag-capture-info"]))
        self.assertIn("auto: 1480 RPM", body["diag-speed"]["placeholder"])
        self.assertIn("No bearing set", json.dumps(body["diag-bearing"]))

    def test_typed_speed_moves_the_harmonics(self):
        figure = self.view("spectrum", speed=30, unit="hz")["diag-plot"]["figure"]
        harmonics = next(trace for trace in figure["data"] if trace.get("name") == "Harmonics")
        self.assertEqual(harmonics["x"][:2], [30.0, 60.0])
        plain = self.view("spectrum", tools=())["diag-plot"]["figure"]
        self.assertFalse(any(trace.get("name") in ("Harmonics", "Peaks") for trace in plain["data"]))

    def test_bearing_typed_on_the_page_draws_its_lines_on_any_install(self):
        with patch.dict(os.environ, {"VDS_EDGE_AI": "0"}):
            saved = self.request("diag_bearing_save", [{"id": "diag-bearing-save", "property": "n_clicks", "value": 1}],
                                 [self.channel(), {"id": "diag-bearing-input", "property": "value", "value": "6205ZZ"}])
            self.assertIn("Saved 6205ZZ", json.dumps(saved))
            body = self.view("spectrum")
        header = json.dumps(body["diag-bearing"])
        self.assertIn("6205-2RS", header)
        self.assertIn("BPFO", header)
        tags = json.dumps(body["diag-plot"]["figure"]["layout"]["annotations"])
        self.assertIn("BPFO", tags)
        with closing(app.get_db_connection()) as conn:
            self.assertEqual(historian.ai_channel_settings(conn, "GW1", "1")["bearing"], "6205ZZ")
        unknown = self.request("diag_bearing_save", [{"id": "diag-bearing-save", "property": "n_clicks", "value": 1}],
                               [self.channel(), {"id": "diag-bearing-input", "property": "value", "value": "XYZ-1"}])
        self.assertIn("not in the bearing library", json.dumps(unknown))

    def test_bearing_database_lists_matches_and_picks_one(self):
        inputs = lambda text: [{"id": "diag-bearing-input", "property": "value", "value": text}, self.channel(),
                               {"id": "diag-bearing-result", "property": "children", "value": None}]
        found = self.request("diag_bearing_matches", inputs("6205"), viewer=True)
        rows = json.dumps(found["diag-bearing-matches"])
        self.assertIn("6205-2RS", rows)
        self.assertIn("BPFO 3.58x", rows)
        self.assertNotIn("6310", rows)
        self.assertIn("—", json.dumps(found["diag-bearing-selected"], ensure_ascii=False))  # nothing saved yet
        none = self.request("diag_bearing_matches", inputs("ZZZ999"), viewer=True)
        self.assertIn("No bearing in the library matches", json.dumps(none))
        pick = self.request("diag_bearing_pick", [[{"id": {"type": "diag-bearing-pick", "name": "6205-2RS"},
                                                    "property": "n_clicks", "value": 1}]], viewer=True,
                            changed=['{"name":"6205-2RS","type":"diag-bearing-pick"}.n_clicks'])
        self.assertEqual(pick["diag-bearing-input"]["value"], "6205-2RS")

    def test_viewer_cannot_save_a_bearing(self):
        key = next(k for k, v in app.app.callback_map.items()
                   if getattr(v.get("callback"), "__name__", "") == "diag_bearing_save")
        with patch.dict(os.environ, {"VDS_ADMIN_PASSWORD": "a-test", "VDS_VIEWER_PASSWORD": "v-test"}):
            response = self.client.post("/_dash-update-component", json={"output": key},
                                        headers={"Authorization": "Basic " + base64.b64encode(b"viewer:v-test").decode()})
        self.assertEqual(response.status_code, 403)

    def test_velocity_envelope_waveform_and_waterfall(self):
        velocity = self.view("spectrum", "velocity")
        self.assertIn("Velocity (mm/s", velocity["diag-plot"]["figure"]["layout"]["yaxis"]["title"]["text"])
        self.assertIn("Vel RMS", json.dumps(velocity["diag-rms"]))
        envelope = self.view("spectrum", "envelope")["diag-plot"]["figure"]
        self.assertEqual(envelope["layout"]["yaxis"]["title"]["text"], "Envelope (device units)")
        waveform = self.view("waveform")
        self.assertIn("CF 1.41", json.dumps(waveform["diag-rms"]))
        waterfall = self.view("waterfall")["diag-plot"]["figure"]
        self.assertEqual(waterfall["data"][0]["type"], "heatmap")
        self.assertEqual(len(waterfall["data"][0]["z"]), 6)

    def test_channel_without_saved_spectra_explains_why(self):
        body = self.request("diag_view_capture", [
            {"id": "diag-channel", "property": "data", "value": json.dumps(["GW1", "4"])},
            {"id": "diag-time", "property": "data", "value": None},
            {"id": "diag-view", "property": "value", "value": "spectrum"},
            {"id": "diag-quantity", "property": "value", "value": "acceleration"},
            {"id": "diag-tools", "property": "value", "value": []},
            {"id": "diag-speed", "property": "value", "value": None},
            {"id": "diag-speed-unit", "property": "value", "value": "rpm"},
            {"id": "diag-bearing-result", "property": "children", "value": None},
            {"id": "diag-wave-tools", "property": "value", "value": []},
            {"id": "diag-wave-zoom", "property": "value", "value": "all"}])
        self.assertIn("No saved spectrum", json.dumps(body["diag-plot"]["figure"]))

    def test_measurements_tab_lists_saved_spectra_and_opens_one(self):
        body = self.request("diag_measurements", [self.channel(), {"id": "diag-time", "property": "data", "value": None}],
                            viewer=True)
        text = json.dumps(body)
        self.assertIn("6 saved spectra", text)
        self.assertEqual(text.count('"type": "diag-meas"'), 6)
        pick = self.request("diag_measure_pick", [[{"id": {"type": "diag-meas", "ms": self.first_ms},
                                                    "property": "n_clicks", "value": 1}]],
                            changed=[json.dumps({"ms": self.first_ms, "type": "diag-meas"}, separators=(",", ":"))
                                     + ".n_clicks"])
        self.assertEqual(pick["diag-time"]["data"], self.first_ms)
        tabs = self.request("diag_tree_tab", [{"id": "diag-tree-tab", "property": "value", "value": "measurements"}])
        self.assertEqual(tabs["diag-tree-assets"]["style"], {"display": "none"})

    def test_channel_header_shows_alarm_state_and_signed_in_user(self):
        with closing(app.get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO alarm_state VALUES (?,?,?)", ("GW1", "1", json.dumps({"severity": "warning"})))
        body = self.request("diag_overview", [self.channel(), {"id": "diag-metrics", "property": "value", "value": []}])
        self.assertIn("Warning", json.dumps(body["diag-status"]))
        user = self.request("sidebar_user", [{"id": "url", "property": "pathname", "value": "/"}], viewer=True)
        self.assertIn("Viewer", json.dumps(user))

    def test_settings_pages_share_tabs_and_one_menu_entry(self):
        url = lambda path: [{"id": "url", "property": "pathname", "value": path}]
        for path in ("/device", "/mqtt", "/opcua", "/debug"):
            page = json.dumps(self.request("display_page", url(path), viewer=True))
            for label in ("Device", "MQTT", "Ethernet", "Modbus TCP", "OPC UA", "Data storage", "Debug log"):
                self.assertIn(label, page)
            self.assertIn('"aria-current": "page"', page)
            self.assertTrue(self.request("nav_settings", url(path), viewer=True)["nav-settings"]["active"])
        self.assertFalse(self.request("nav_settings", url("/"), viewer=True)["nav-settings"]["active"])
        # Device details (serial number, uptime, memory ...) live on the Device tab, not on Home
        self.assertIn("pi-serial", json.dumps(self.request("display_page", url("/device"))))
        self.assertNotIn("pi-serial", json.dumps(self.request("display_page", url("/"))))

    def test_baseline_button_still_saves_the_live_spectrum(self):
        app.data_storage["GW1"] = {"channels": {"1": {"spectrum_context": {"bandwidth": 1000, "lines": 800},
                                                       "spectrum_data": [1e-4] * 801}}}
        body = self.request("capture_baseline", [{"id": "baseline-save", "property": "n_clicks", "value": 1}],
                            [self.channel()])
        self.assertIn("Baseline captured", json.dumps(body))
        with closing(app.get_db_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM baselines").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
