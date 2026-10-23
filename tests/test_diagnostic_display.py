import unittest

from vds_dashboard.diagnostics import waveform_figure, spectrum_with_rpm


class DiagnosticDisplayTests(unittest.TestCase):
    def test_waveform_uses_sample_timing(self):
        figure = waveform_figure(dict(waveform_data=[1, -1, 2], sampling_freq=4))
        self.assertEqual(list(figure.data[0].x), [0, .25, .5])
        for fs in (None, 0, -1, float("nan")):
            with self.assertRaises(ValueError):
                waveform_figure(dict(waveform_data=[1], sampling_freq=fs))

    def test_manual_rpm_converts_both_traces_without_mutating_captures(self):
        current = dict(spectrum_data=[0, 2, 1], bandwidth=40, lines=2)
        baseline = dict(current)
        figure = spectrum_with_rpm(current, baseline, True, 1200)
        for trace in figure.data:
            self.assertEqual(list(trace.x), [0, 1, 2])
        self.assertNotIn("rpm", current)
        self.assertNotIn("rpm", baseline)
        for rpm in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                spectrum_with_rpm(current, orders=True, manual_rpm=rpm)

    def test_capture_rpm_is_used_when_override_blank(self):
        current = dict(spectrum_data=[0, 1], bandwidth=20, lines=1, rpm=600)
        self.assertEqual(list(spectrum_with_rpm(current, orders=True).data[0].x), [0, 2])


class AcquisitionTests(unittest.TestCase):
    def test_peak_ranking_dc_exclusion_and_orders(self):
        from vds_dashboard.diagnostics import spectrum_peaks
        capture = dict(spectrum_data=[99, 1, 5, 1, 9, 0, 99], bandwidth=60, lines=6)
        peaks = spectrum_peaks(capture, 1200)
        self.assertEqual([p["frequency"] for p in peaks], [40, 20])
        self.assertEqual([p["order"] for p in peaks], [2, 1])
        self.assertIsNone(spectrum_peaks(capture)[0]["order"])

    def test_metadata_keeps_unknown_settings_and_marks_override(self):
        from vds_dashboard.diagnostics import capture_details
        capture = dict(timestamp_ms=1000, bandwidth=500, lines=100, rpm=1440)
        details = capture_details(capture, "Spectrum", 1200)
        self.assertEqual(details["Frequency resolution"], "5 Hz")
        self.assertEqual(details["FFT window"], "Unknown")
        self.assertEqual(details["RPM source"], "Manual override")
        self.assertEqual(capture["rpm"], 1440)
        self.assertTrue(details["Capture (UTC)"].endswith("+00:00"))

    def test_cursor_keeps_hz_when_axis_uses_orders(self):
        capture = dict(spectrum_data=[0, 1, 0], bandwidth=40, lines=2, rpm=1200)
        figure = spectrum_with_rpm(capture, orders=True)
        self.assertEqual(list(figure.data[0].customdata[1]), [20, 1])

    def test_recorded_lookup_does_not_mix_timestamps_or_channels(self):
        import sqlite3
        from contextlib import closing
        import tempfile
        import numpy as np
        from vds_dashboard.diagnostics import recorded_capture
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(folder + "/capture.db")) as conn:
                conn.execute("CREATE TABLE raw_waveforms (gateway_id,channel,timestamp,waveform_data,sampling_freq,context)")
                conn.execute("CREATE TABLE raw_spectrum (gateway_id,channel,timestamp,spectrum_data,bandwidth,lines,context)")
                blob = np.array([0, 2, 0], dtype="<f8").tobytes()
                conn.execute("INSERT INTO raw_waveforms VALUES (?,?,?,?,?,?)", ("g", "1", "first", blob, 4, None))
                conn.execute("INSERT INTO raw_spectrum VALUES (?,?,?,?,?,?,?)", ("g", "1", "second", blob, 40, 2, None))
                first = recorded_capture(conn, "g", "1", "first")
                self.assertEqual(set(first), {"waveform"})
                self.assertEqual(first["waveform"]["sampling_freq"], 4)
                second = recorded_capture(conn, "g", "1", "second")
                self.assertEqual(set(second), {"spectrum"})
                self.assertEqual(second["spectrum"]["bandwidth"], 40)
                self.assertEqual(recorded_capture(conn, "g", "2", "second"), {})


class RpmSourceTests(unittest.TestCase):
    def test_live_speed_follows_dashboard_without_changing_capture(self):
        from vds_dashboard.diagnostics import display_rpm, capture_details
        capture = dict(rpm=600)
        displayed = display_rpm(capture, 1440, live=True)
        self.assertEqual(displayed["rpm"], 1440)
        self.assertEqual(capture_details(displayed, "Spectrum")["RPM source"], "Dashboard")
        self.assertEqual(capture["rpm"], 600)

    def test_invalid_dashboard_requires_manual_and_history_stays_independent(self):
        from vds_dashboard.diagnostics import display_rpm
        capture = dict(rpm=600, spectrum_data=[0, 1], bandwidth=20, lines=1)
        for speed in (None, 0, -1, float("nan"), float("inf"), "bad"):
            displayed = display_rpm(capture, speed, live=True)
            self.assertIsNone(displayed["rpm"])
            with self.assertRaises(ValueError):
                spectrum_with_rpm(displayed, orders=True)
            self.assertEqual(list(spectrum_with_rpm(displayed, orders=True, manual_rpm=1200).data[0].x), [0, 1])
        self.assertEqual(display_rpm(capture, 1440, live=False)["rpm"], 600)
