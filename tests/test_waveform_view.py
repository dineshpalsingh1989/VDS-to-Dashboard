"""Waveform tools: impact finder, velocity integration, turns and orbit."""

import unittest

import numpy as np

from vds_dashboard import waveform_view

FS = 4096
T = np.arange(FS) / FS
BPFO = 3.5848 * 25  # 6205 outer race at 1500 RPM


def bearing_waveform(rng, impacts=True):
    x = np.sin(2 * np.pi * 25 * T) + 0.2 * rng.standard_normal(T.size)
    if impacts:
        since = T % (1 / BPFO)
        x += 0.6 * np.exp(-since * 400) * np.sin(2 * np.pi * 1200 * since)
    return x


class WaveformToolTests(unittest.TestCase):
    def test_impacts_are_timed_and_named(self):
        rng = np.random.default_rng(1)
        times, period = waveform_view.find_impacts(bearing_waveform(rng), FS)
        self.assertAlmostEqual(1 / period, BPFO, delta=0.03 * BPFO)
        self.assertGreater(times.size, 60)
        text = waveform_view.impact_summary(times, period, [("Running speed", 25.0), ("BPFO", BPFO)])
        self.assertIn("that is BPFO (89.6 Hz)", text)
        quiet = waveform_view.find_impacts(bearing_waveform(rng, impacts=False), FS)
        self.assertEqual(waveform_view.impact_summary(*quiet, []), "No clear impacts in this waveform.")

    def test_velocity_from_acceleration(self):
        velocity = waveform_view.velocity_waveform(np.sin(2 * np.pi * 25 * T), FS)
        self.assertAlmostEqual(float(np.max(np.abs(velocity))), 9806.65 / (2 * np.pi * 25), delta=0.5)

    def test_turn_markers_and_zoom(self):
        figure = waveform_view.waveform_figure(np.sin(2 * np.pi * 25 * T), FS, "g", 1500, {"revs"}, "5")
        self.assertAlmostEqual(figure.layout.xaxis.range[1], 0.2)
        self.assertEqual(len(figure.layout.shapes), 5)
        self.assertIn("1 turn = 40.0 ms", figure.layout.annotations[0].text)

    def test_orbit_shape(self):
        round_fig, round_text = waveform_view.orbit_figure(np.cos(2 * np.pi * 25 * T), np.sin(2 * np.pi * 25 * T),
                                                           FS, 1500, "mm/s")
        self.assertIn("nearly round", round_text)
        _flat, flat_text = waveform_view.orbit_figure(np.cos(2 * np.pi * 25 * T), 0.2 * np.sin(2 * np.pi * 25 * T),
                                                      FS, 1500, "mm/s")
        self.assertIn("flat ellipse", flat_text)
        self.assertEqual(round_fig.layout.yaxis.scaleanchor, "x")


if __name__ == "__main__":
    unittest.main()
