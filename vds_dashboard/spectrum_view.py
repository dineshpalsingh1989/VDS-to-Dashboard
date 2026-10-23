"""Spectrum plot for the dashboard pop-up: clear trace, peak list and fault frequency lines."""

import numpy as np
import plotly.graph_objects as go
from scipy.signal import find_peaks

from .fault_frequencies import marker_lines, nearest_fault

MAX_PEAKS = 10


def strongest_peaks(x, y, limit=MAX_PEAKS):
    """The clearest peaks, judged by prominence relative to the largest one."""
    if y.size < 3 or float(np.max(y)) <= 0:
        return np.array([], dtype=int)
    peaks, props = find_peaks(y, prominence=0.04 * float(np.max(y)))
    order = np.argsort(props["prominences"])[::-1][:limit]
    return np.sort(peaks[order])


def spectrum_figure(x, y, title, rpm=None, info=None, families=(), harmonics=5, log=False):
    """Returns (figure, peak rows). info carries bearing, gear, blade and mains data for the lines."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    orders = x * 60 / rpm if rpm else None
    figure = go.Figure(go.Scatter(
        x=x, y=y, mode="lines", name="Spectrum", line=dict(color="#1f3b73", width=1.3),
        customdata=orders, hovertemplate=("%{x:.2f} Hz" + (" (%{customdata:.2f}x)" if rpm else "")
                                          + "<br>%{y:.4g}<extra></extra>")))
    lines = marker_lines(info or {}, rpm, set(families), harmonics, float(x.max()) if x.size else 0) if info else []
    if not info and rpm:  # dashboards without the IRIV AI: running speed only, as before
        lines = [("speed", "1x", rpm / 60, "#6c757d")]
    labelled = 0
    for _key, name, hz, colour in lines:
        main = name in ("1x", "2x", "3x", "BPFO", "BPFI", "BSF", "FTF", "GMF", "BPF", "2x Line")
        figure.add_vline(x=hz, line=dict(color=colour, width=1.2 if main else 0.7, dash="dot"),
                         opacity=0.9 if main else 0.45)
        if main:  # label the main lines only, staggered so neighbours do not overlap
            figure.add_annotation(x=hz, y=1.0 - 0.07 * (labelled % 3), yref="paper", text=name, showarrow=False,
                                  xanchor="left", yanchor="top", font=dict(size=10, color=colour),
                                  bgcolor="rgba(255,255,255,0.8)")
            labelled += 1
    if lines:  # every line, harmonics included, names itself on hover
        figure.add_trace(go.Scatter(
            x=[hz for _k, _n, hz, _c in lines], y=[float(y.max()) if y.size else 0] * len(lines), mode="markers",
            marker=dict(size=6, color=[c for _k, _n, _hz, c in lines], symbol="line-ns-open"), name="Fault lines",
            text=[n for _k, n, _hz, _c in lines], hovertemplate="%{text}: %{x:.1f} Hz<extra></extra>"))
    peaks = strongest_peaks(x, y)
    rows = []
    if peaks.size:
        figure.add_trace(go.Scatter(
            x=x[peaks], y=y[peaks], mode="markers+text", name="Peaks", marker=dict(color="#e45756", size=7,
                                                                                   symbol="triangle-down"),
            text=[f"{x[p]:.1f}" for p in peaks], textposition="top center", textfont=dict(size=9),
            hoverinfo="skip"))
        for p in peaks:
            rows.append({"hz": float(x[p]), "order": float(orders[p]) if rpm else None, "amplitude": float(y[p]),
                         "match": nearest_fault(float(x[p]), lines)})
    figure.update_layout(
        template="plotly_white", title=dict(text=title, font=dict(size=15)), height=470,
        margin=dict(l=60, r=20, t=60, b=50), hovermode="x unified", showlegend=False,
        xaxis=dict(title="Frequency (Hz)", showspikes=True, spikemode="across", spikethickness=1),
        yaxis=dict(title="Amplitude (device units)", type="log" if log else "linear",
                   exponentformat="power" if log else "none"))
    if info and (lines or peaks.size) and x.size:
        # Open zoomed to where the fault lines and peaks are; double-click shows the full span.
        top = max([hz for _k, _n, hz, _c in lines] + [float(x[p]) for p in peaks]) * 1.15
        if top < float(x.max()):
            figure.update_xaxes(range=[0, top])
    return figure, rows
