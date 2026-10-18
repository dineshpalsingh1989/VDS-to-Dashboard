"""Diagnostic plots of published VDS data, without reinterpreting spectrum units."""

import numpy as np
import plotly.graph_objects as go
import json
from datetime import datetime, timezone
from plotly.subplots import make_subplots

from vds import spectrum_signature, spectrum_xy


def export_frame(frame):
    frame = frame.copy()
    if "context" not in frame.columns:
        return frame
    contexts = [json.loads(value) if value else {} for value in frame["context"]]
    for key in ("unit", "filter", "detection"):
        frame[key] = [item.get(key) or "unknown" for item in contexts]
    frame["capture_utc"] = [
        datetime.fromtimestamp(item["timestamp_ms"] / 1000, timezone.utc).isoformat()
        if item.get("timestamp_ms")
        else "unknown (legacy local timestamp)"
        for item in contexts
    ]
    return frame.drop(columns=["context"])


def history_figure(frame, title, rpm=False):
    if frame.empty:
        return go.Figure().update_layout(title="No data found")
    frame = export_frame(frame)
    if rpm:
        return go.Figure(
            go.Scattergl(x=frame["timestamp"], y=frame["rpm"], mode="lines+markers")
        ).update_layout(title=title, yaxis_title="RPM", template="plotly_white")
    groups = list(frame.groupby(["unit", "filter", "detection"], dropna=False))
    titles = [" · ".join(str(part) for part in key) for key, _ in groups]
    figure = make_subplots(rows=len(groups), cols=1, subplot_titles=titles)
    for row, ((unit, _, _), group) in enumerate(groups, 1):
        figure.add_trace(
            go.Scattergl(
                x=group["timestamp"],
                y=group["value"],
                mode="lines+markers",
                name=titles[row - 1],
            ),
            row=row,
            col=1,
        )
        figure.update_yaxes(title_text=unit, row=row, col=1)
    figure.update_layout(
        title=title,
        height=max(350, len(groups) * 250),
        template="plotly_white",
        showlegend=False,
    )
    return figure


def spectrum_figure(current, baseline=None, orders=False, harmonics=5):
    x, y = spectrum_xy(current, orders)
    figure = go.Figure(go.Scatter(x=x, y=y, name="Current", mode="lines"))
    if baseline:
        if spectrum_signature(current) != spectrum_signature(baseline):
            raise ValueError(
                "Baseline acquisition settings differ; capture a compatible baseline"
            )
        bx, by = spectrum_xy(baseline, orders)
        figure.add_trace(
            go.Scatter(x=bx, y=by, name="Baseline", mode="lines", line={"dash": "dot"})
        )
    rpm = current.get("rpm")
    if rpm and rpm > 0:
        for order in range(1, min(int(harmonics), 20) + 1):
            position = order if orders else order * rpm / 60
            if position <= x[-1]:
                figure.add_vline(
                    x=position,
                    line_dash="dash",
                    line_color="#b36b00",
                    annotation_text=f"{order}×",
                )
    figure.update_layout(
        template="plotly_white",
        xaxis_title="Order (× RPM)" if orders else "Frequency (Hz)",
        yaxis_title="Published power spectrum (device units)",
        title="Spectrum comparison",
        uirevision="spectrum",
    )
    return figure


def waterfall_figure(snapshots):
    if not snapshots:
        raise ValueError("Record spectra to build a waterfall")
    reference = spectrum_signature(snapshots[-1])
    matching = [item for item in snapshots if spectrum_signature(item) == reference]
    # Keep plots responsive while retaining the full-resolution data in storage.
    x, _ = spectrum_xy(matching[-1])
    stride = max(1, int(np.ceil(len(x) / 2000)))
    z = [spectrum_xy(item)[1][::stride] for item in matching]
    times = [item.get("label", str(item.get("timestamp_ms", ""))) for item in matching]
    figure = go.Figure(
        go.Heatmap(
            x=x[::stride],
            y=times,
            z=z,
            colorscale="Viridis",
            colorbar={"title": "Device units"},
        )
    )
    figure.update_layout(
        template="plotly_white",
        title=f"Spectrum waterfall — {len(matching)} compatible captures",
        xaxis_title="Frequency (Hz)",
        yaxis_title="Capture time",
    )
    return figure
