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


def waveform_figure(current):
    values = np.asarray(current.get("waveform_data", []), dtype=float)
    fs = current.get("sampling_freq")
    if values.ndim != 1 or not values.size or not np.all(np.isfinite(values)):
        raise ValueError("No validated waveform available for this channel")
    if not isinstance(fs, (int, float)) or not np.isfinite(fs) or fs <= 0:
        raise ValueError("Waveform requires a positive sampling frequency")
    return go.Figure(go.Scattergl(
        x=np.arange(values.size) / fs, y=values, mode="lines", name="Waveform"
    )).update_layout(template="plotly_white", title="Time waveform",
        xaxis_title="Time (s)", yaxis_title="Published waveform (sensor units)",
        uirevision="waveform")


def spectrum_with_rpm(current, baseline=None, orders=False, manual_rpm=None):
    """Apply a display-only speed override without modifying capture metadata."""
    current = dict(current)
    baseline = dict(baseline) if baseline else None
    if manual_rpm is not None:
        if not isinstance(manual_rpm, (int, float)) or not np.isfinite(manual_rpm) or manual_rpm <= 0:
            raise ValueError("Manual RPM must be a finite number greater than zero")
        current["rpm"] = manual_rpm
        if baseline:
            baseline["rpm"] = manual_rpm
    figure = spectrum_figure(current, baseline, orders)
    if manual_rpm is not None:
        figure.update_layout(title=f"Spectrum comparison - manual RPM: {manual_rpm:g}")
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
    for trace in figure.data:
        tx = np.asarray(trace.x, dtype=float)
        # The order axis can use a distinct capture RPM for each trace.
        speed = (baseline.get("rpm") if trace.name == "Baseline" else current.get("rpm"))
        hz = tx * (speed / 60) if orders else tx
        order_values = hz / (speed / 60) if speed and speed > 0 else np.full(tx.size, np.nan)
        trace.update(customdata=np.column_stack((hz, order_values)),
                     hovertemplate="%{customdata[0]:.3f} Hz<br>%{y:.6g} device units<br>Order: %{customdata[1]:.3f}<extra>%{fullData.name}</extra>")
    figure.update_layout(
        hovermode="closest", clickmode="event+select",
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


def capture_details(snapshot, kind, manual_rpm=None):
    """Human-readable acquisition details, preserving unknown legacy settings."""
    timestamp = snapshot.get("timestamp_ms")
    captured = (datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
                if timestamp else ("Stored time: " + snapshot["label"] + " (timezone unknown)" if snapshot.get("label") else "Unknown"))
    rpm = manual_rpm if manual_rpm is not None else snapshot.get("rpm")
    details = {"Plot": kind, "Capture (UTC)": captured,
               "Amplitude": "Device units" if kind == "Spectrum" else "Sensor units",
               "Sampling rate": f"{snapshot['sampling_freq']:g} Hz" if snapshot.get("sampling_freq") else "Unknown",
               "RPM": f"{rpm:g}" if rpm is not None else "Unavailable",
               "RPM source": "Manual override" if manual_rpm is not None else "Capture" if rpm is not None else "Unavailable"}
    if kind == "Spectrum":
        bandwidth, lines = snapshot.get("bandwidth"), snapshot.get("lines")
        details["Frequency resolution"] = f"{bandwidth / lines:g} Hz" if bandwidth and lines else "Unknown"
        details["Bandwidth"] = f"{bandwidth:g} Hz" if bandwidth else "Unknown"
        details["FFT window"] = snapshot.get("window") or "Unknown"
    return details


def spectrum_peaks(snapshot, manual_rpm=None, limit=10):
    x, y = spectrum_xy(snapshot)
    rpm = manual_rpm if manual_rpm is not None else snapshot.get("rpm")
    if manual_rpm is not None and (not np.isfinite(manual_rpm) or manual_rpm <= 0):
        raise ValueError("Manual RPM must be a finite number greater than zero")
    # Exclude DC and endpoints; rank discrete local maxima without interpolation.
    candidates = [i for i in range(1, len(y) - 1)
                  if np.isfinite(y[i]) and y[i] > y[i - 1] and y[i] >= y[i + 1]]
    candidates.sort(key=lambda i: (-y[i], i))
    return [{"frequency": float(x[i]), "amplitude": float(y[i]),
             "order": float(x[i] / (rpm / 60)) if rpm and rpm > 0 else None}
            for i in candidates[:limit]]


def recorded_capture(conn, gid, ch, timestamp):
    """Read only exact timestamp matches: never pair separate acquisitions."""
    result = {}
    for kind, table, column, fields in (("waveform", "raw_waveforms", "waveform_data", "sampling_freq"),
                                 ("spectrum", "raw_spectrum", "spectrum_data", "bandwidth,lines")):
        row = conn.execute(f"SELECT {column},context,{fields} FROM {table} WHERE gateway_id=? AND channel=? AND timestamp=?",
                           (gid, ch, timestamp)).fetchone()
        if row:
            result[kind] = dict(json.loads(row[1]) if row[1] else {},
                                **{column: np.frombuffer(row[0], dtype="<f8").tolist()},
                                label=str(timestamp))
            for key, val in zip(fields.split(","), row[2:]):
                if result[kind].get(key) is None:
                    result[kind][key] = val
    return result
