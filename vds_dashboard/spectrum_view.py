"""Spectrum plot for the dashboard pop-up: clear trace, peak list and fault frequency lines."""

import numpy as np
import plotly.graph_objects as go
from scipy.signal import find_peaks

from .fault_frequencies import marker_lines, nearest_fault

MAX_PEAKS = 10
MAIN_LINES = ("1x", "2x", "3x", "BPFO", "BPFI", "BSF", "FTF", "GMF", "BPF", "2x Line")


def strongest_peaks(x, y, limit=MAX_PEAKS):
    """The clearest peaks, judged by prominence relative to the largest one."""
    if y.size < 3 or float(np.max(y)) <= 0:
        return np.array([], dtype=int)
    peaks, props = find_peaks(y, prominence=0.04 * float(np.max(y)))
    order = np.argsort(props["prominences"])[::-1][:limit]
    return np.sort(peaks[order])


def nice_step(span, target=10):
    raw = span / target if span > 0 else 1
    power = 10 ** np.floor(np.log10(raw))
    return float(min((m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw), default=10 * power))


def add_cpm_axis(figure, top):
    """Frequency ticks labelled in Hz with CPM (cycles per minute) underneath."""
    step = nice_step(top)
    ticks = np.arange(0, top + step / 2, step)
    figure.update_xaxes(tickmode="array", tickvals=ticks.tolist(),
                        ticktext=[f'{t:g}<br><span style="color:#7b8aa0">{t * 60:,.0f}</span>' for t in ticks],
                        title="Frequency (Hz / CPM)")


def add_harmonic_markers(figure, x, y, rpm, top, count=20):
    """Running-speed harmonics: dashed lines with a red diamond on the trace, and a CPM label at 1x."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    shaft = rpm / 60
    hz = [k * shaft for k in range(1, count + 1) if k * shaft <= top]
    if not hz or x.size == 0:
        return
    for k, f in enumerate(hz, start=1):
        figure.add_vline(x=f, line=dict(color="#2647d6", width=1, dash="dash"), opacity=0.75)
    values = [float(y[int(np.argmin(np.abs(x - f)))]) for f in hz]
    figure.add_trace(go.Scatter(x=hz, y=values, mode="markers", name="Harmonics",
                                marker=dict(symbol="diamond", size=9, color="#e3262f", line=dict(color="white", width=1)),
                                text=[f"{k}x" for k in range(1, len(hz) + 1)],
                                hovertemplate="%{text}: %{x:.2f} Hz<extra></extra>"))
    figure.add_annotation(x=hz[0], y=1, yref="paper", text=f"<b>{rpm:.0f} CPM</b><br>1x", showarrow=False,
                          xanchor="left", yanchor="top", align="left", font=dict(size=11, color="#203047"))


def spectrum_figure(x, y, title, rpm=None, info=None, families=(), harmonics=5, log=False, key_box=True):
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
    labelled, key = 0, []
    for _key, name, hz, colour in lines:
        main = name in MAIN_LINES
        figure.add_vline(x=hz, line=dict(color=colour, width=1.2 if main else 0.7, dash="dot"),
                         opacity=0.9 if main else 0.45)
        if main:
            # Shaded band (±2 %) where this fault frequency can sit as speed varies a little
            figure.add_vrect(x0=hz * 0.98, x1=hz * 1.02, fillcolor=colour, opacity=0.08, line_width=0, layer="below")
            # Coloured label tag, staggered so neighbouring tags do not overlap
            figure.add_annotation(x=hz, y=1.0 - 0.075 * (labelled % 3), yref="paper", text=f"<b>{name}</b>",
                                  showarrow=False, xanchor="left", yanchor="top", font=dict(size=10, color="white"),
                                  bgcolor=colour, bordercolor=colour, borderpad=2, opacity=0.95)
            labelled += 1
            key.append(f'<span style="color:{colour}">■</span> {name} {hz:.1f} Hz')
    if key and key_box:  # colour key in the top-right corner
        figure.add_annotation(x=0.995, y=0.985, xref="paper", yref="paper", xanchor="right", yanchor="top",
                              align="left", showarrow=False, text="<br>".join(key), font=dict(size=10, color="#203047"),
                              bgcolor="rgba(255,255,255,0.92)", bordercolor="#cbd5e1", borderwidth=1, borderpad=6)
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
