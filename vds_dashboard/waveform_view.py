"""Waveform tools for the Machine history page (all installs).

* Velocity and envelope waveforms from an acceleration waveform.
* Revolution markers: a line at every shaft turn, so a knock that repeats once per turn stands out.
* Impact finder: marks each impact and measures how often they come, then names the matching
  frequency (running speed or a bearing defect frequency), for example "every 11.3 ms = 88.6 Hz = BPFO".
* An earlier waveform of the same channel behind today's.
* Zoom to one or five turns.
* Orbit: the horizontal sensor against the vertical one at the same bearing, filtered around running speed.

Display only; deterministic; nothing here talks to a machine.
"""

import numpy as np
import plotly.graph_objects as go
from scipy.signal import find_peaks

from .signals import G, MIN_ENVELOPE_FS

MAX_MARKERS = 200
REPEAT_STRENGTH = 0.2  # autocorrelation of the envelope at the repeat; random noise stays well below


def is_acceleration(sensor_type):
    sensor = str(sensor_type or "").lower()
    return "accel" in sensor or sensor.endswith("(g)")


def velocity_waveform(values, fs, min_hz=2.0):
    """Acceleration in g integrated to velocity in mm/s (in the frequency domain, nothing below min_hz)."""
    x = np.asarray(values, dtype=float)
    x = x - x.mean()
    spectrum = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.size, 1.0 / fs)
    keep = f >= min_hz
    out = np.zeros_like(spectrum)
    out[keep] = spectrum[keep] * G / (2j * np.pi * f[keep])
    return np.fft.irfft(out, n=x.size)


def envelope_waveform(values, fs):
    """Envelope of the band 10-40 % of the sampling rate: bearing impacts as clean pulses."""
    x = np.asarray(values, dtype=float)
    x = x - x.mean()
    spectrum = np.fft.fft(x)
    freqs = np.fft.fftfreq(x.size, 1.0 / fs)
    band = (freqs >= 0.1 * fs) & (freqs <= 0.4 * fs)
    return np.abs(np.fft.ifft(np.where(band, 2 * spectrum, 0)))


def find_impacts(values, fs):
    """(impact times in s, repetition period in s or None).

    Impacts are looked for in the envelope when the sampling rate allows (high-frequency ringing),
    else in the rectified waveform. A steady repeat is found from the envelope's autocorrelation,
    which works even when single impacts hardly stand out of the noise; each repeat is then marked.
    """
    x = np.asarray(values, dtype=float)
    if x.size < 64 or not fs:
        return np.array([]), None
    signal = envelope_waveform(x, fs) if fs >= MIN_ENVELOPE_FS else np.abs(x - x.mean())
    level = np.median(signal)
    spread = np.median(np.abs(signal - level)) * 1.4826 + 1e-12
    centred = signal - signal.mean()
    corr = np.correlate(centred, centred, mode="full")[centred.size - 1:]
    corr = corr / (corr[0] or 1)
    start = max(2, int(0.001 * fs))
    lags, props = find_peaks(corr[start:centred.size // 2], height=REPEAT_STRENGTH)
    period = None
    if lags.size:  # the first strong repeat, not a multiple of it
        heights = props["peak_heights"]
        pick = 0 if heights[0] >= 0.6 * heights.max() else int(np.argmax(heights))
        period = (start + lags[pick]) / fs
    if period:
        peaks, _ = find_peaks(signal, height=level + 2 * spread, distance=max(1, int(0.6 * period * fs)))
    else:  # no steady repeat: only clear single impacts
        peaks, _ = find_peaks(signal, height=level + 6 * spread, distance=max(1, int(0.0015 * fs)))
    return peaks / fs, period


def name_frequency(hz, lines, tolerance=0.03):
    """The fault line (name, Hz) within 3 % of hz, if any."""
    best = None
    for name, line_hz in lines:
        if line_hz and abs(hz - line_hz) <= tolerance * line_hz and (best is None or abs(hz - line_hz) < abs(hz - best[1])):
            best = (name, line_hz)
    return best


def impact_summary(times, period, lines):
    if period is None or times.size < 3:
        return f"{times.size} impact{'s' if times.size != 1 else ''} found, no steady repeat." if times.size else \
            "No clear impacts in this waveform."
    hz = 1 / period
    match = name_frequency(hz, lines)
    text = f"{times.size} impacts, repeating every {period * 1000:.1f} ms ({hz:.1f} Hz)"
    return text + (f": that is {match[0]} ({match[1]:.1f} Hz)." if match else ": no fault frequency matches.")


def waveform_figure(values, fs, unit, rpm=None, tools=(), zoom="all", reference=None, impacts=None):
    """The waveform with optional revolution markers, impact markers and an earlier waveform behind it."""
    tools = set(tools or ())
    t = np.arange(len(values)) / fs
    figure = go.Figure()
    if reference is not None and "reference" in tools:
        ref_values, ref_fs, ref_label = reference
        figure.add_trace(go.Scatter(x=np.arange(len(ref_values)) / ref_fs, y=ref_values, mode="lines",
                                    name=ref_label, line=dict(color="#94a3b8", width=1),
                                    hovertemplate="%{x:.4f} s<br>%{y:.4g}<extra>" + ref_label + "</extra>"))
    figure.add_trace(go.Scatter(x=t, y=values, mode="lines", name="This waveform", line=dict(color="#1f77b4", width=1),
                                hovertemplate="%{x:.4f} s<br>%{y:.4g} " + unit + "<extra></extra>"))
    turn = 60.0 / rpm if rpm and rpm > 0 else None
    end = float(t[-1]) if len(t) else 0
    if zoom in ("1", "5") and turn:
        end = min(end, int(zoom) * turn)
        figure.update_xaxes(range=[0, end])
    if "revs" in tools and turn:
        for k in range(1, min(int(end / turn) + 1, MAX_MARKERS)):
            figure.add_vline(x=k * turn, line=dict(color="#16a34a", width=1, dash="dash"), opacity=0.7)
        figure.add_annotation(x=turn, y=1, yref="paper", yanchor="bottom", showarrow=False,
                              text=f"1 turn = {turn * 1000:.1f} ms", font=dict(size=10, color="#16a34a"))
    if impacts is not None and "impacts" in tools and len(impacts):
        index = np.clip(np.round(np.asarray(impacts) * fs).astype(int), 0, len(values) - 1)[:MAX_MARKERS]
        figure.add_trace(go.Scatter(x=index / fs, y=np.asarray(values)[index], mode="markers", name="Impact",
                                    marker=dict(symbol="triangle-down", size=9, color="#dc2626"),
                                    hovertemplate="impact at %{x:.4f} s<extra></extra>"))
    figure.update_layout(template="plotly_white", height=300, margin=dict(l=60, r=20, t=24, b=45),
                         xaxis_title="t [s]", yaxis_title=unit, showlegend="reference" in tools and reference is not None,
                         legend=dict(orientation="h", y=1.12, x=0))
    return figure


def band_around(values, fs, centre_hz, low=0.5, high=2.5):
    """Keep only low x centre .. high x centre (FFT filter): the running-speed part for an orbit."""
    x = np.asarray(values, dtype=float)
    spectrum = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(x.size, 1.0 / fs)
    spectrum[(f < low * centre_hz) | (f > high * centre_hz)] = 0
    return np.fft.irfft(spectrum, n=x.size)


def orbit_figure(horizontal, vertical, fs, rpm, unit, turns=3):
    """Horizontal against vertical over a few turns, filtered around running speed."""
    n = min(len(horizontal), len(vertical))
    h, v = band_around(horizontal[:n], fs, rpm / 60), band_around(vertical[:n], fs, rpm / 60)
    span = min(n, int(turns * 60 / rpm * fs)) if rpm else n
    limit = float(max(np.max(np.abs(h[:span])), np.max(np.abs(v[:span])), 1e-12)) * 1.15
    figure = go.Figure(go.Scatter(x=h[:span], y=v[:span], mode="lines", line=dict(color="#1d4ed8", width=1.5),
                                  hovertemplate="H %{x:.3g}<br>V %{y:.3g}<extra></extra>"))
    figure.add_trace(go.Scatter(x=[h[0]], y=[v[0]], mode="markers", marker=dict(size=9, color="#dc2626"),
                                hoverinfo="skip"))
    ratio = float(np.std(v[:span]) / np.std(h[:span])) if np.std(h[:span]) > 0 else 0
    figure.update_layout(template="plotly_white", height=300, margin=dict(l=60, r=20, t=24, b=45), showlegend=False,
                         xaxis=dict(title=f"Horizontal ({unit})", range=[-limit, limit], zeroline=True),
                         yaxis=dict(title=f"Vertical ({unit})", range=[-limit, limit], zeroline=True,
                                    scaleanchor="x", scaleratio=1))
    shape = ("nearly round, as unbalance usually gives" if 0.67 <= ratio <= 1.5 else
             "a flat ellipse: much more movement one way, which points to a directional force such as "
             "misalignment, or to looseness or a weak support in that direction")
    return figure, f"Orbit over {turns} turns, filtered around running speed. V/H = {ratio:.2f}, {shape}."
