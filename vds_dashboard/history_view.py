"""Machine history: trend of overall readings; click any moment to see the spectrum and
waveform saved closest to it.

Spectra and waveforms come from the history snapshots (one per channel every
VDS_SNAPSHOT_INTERVAL_S, default 30 minutes) and from recordings. On IRIV Edge AI
installs the spectrum also shows bearing and fault frequency lines and the AI finding.
"""

import csv
import io
import json
import time
from contextlib import closing
from datetime import datetime

import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

from . import ai_views, historian
from .diagnostics import waterfall_figure
from .signals import envelope_spectrum, velocity_from_acceleration, waveform_stats
from .spectrum_view import add_cpm_axis, spectrum_figure
from .vds import spectrum_xy

READ_CALLBACKS = {"diag_gateway_options", "diag_axis_options", "diag_select_channel", "diag_pick_time",
                  "diag_trend", "diag_view_capture", "diag_download"}
# (metric name in the historian, chip label, colour)
METRICS = (("Acceleration", "Acc RMS", "#1f77b4"), ("Velocity", "Vel RMS", "#ff7f0e"),
           ("True Peak", "0-P", "#d62728"), ("Displacement", "Disp", "#9467bd"),
           ("Crest Factor", "CF", "#8c564b"), ("Std Deviation", "Std dev", "#e377c2"))
COLOURS = {name: colour for name, _label, colour in METRICS}
LABELS = {name: label for name, label, _colour in METRICS}
RANGES = (("1d", "1 day", 1), ("7d", "1 week", 7), ("30d", "1 month", 30), ("90d", "3 months", 90))
BEARING_COLOURS = {"ftf": "#8c564b", "bsf": "#9467bd", "bpfo": "#d62728", "bpfi": "#ff7f0e"}
AXIS_STEP = 0.065


def button_radio(component_id, options, value):
    return dbc.RadioItems(id=component_id, options=options, value=value, inline=True, className="btn-tabs",
                          inputClassName="btn-check", labelClassName="btn btn-outline-primary btn-sm",
                          labelCheckedClassName="active")


def page():
    return html.Div([
        html.H3("Machine history"),
        html.P("Choose a machine and channel and the readings to plot. Click anywhere on the history to open the "
               "spectrum and waveform saved closest to that moment.", className="text-muted"),
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="diag-gateway", placeholder="Choose a VDS / machine", clearable=False), md=5),
            dbc.Col(button_radio("diag-axis", [], None), md=7),
        ], className="g-2 align-items-center mb-3"),
        dcc.Store(id="diag-channel"), dcc.Store(id="diag-time"),
        html.Div(id="diag-ai"),
        html.Div([
            dbc.Row([
                dbc.Col(dbc.Checklist(id="diag-metrics", inline=True, className="metric-chips",
                                      options=[{"label": label, "value": name} for name, label, _c in METRICS],
                                      value=["Acceleration", "Velocity"], inputClassName="btn-check",
                                      labelClassName="metric-chip", labelCheckedClassName="active"), md=9),
                dbc.Col(dcc.Dropdown(id="diag-range", clearable=False, value="7d",
                                     options=[{"label": label, "value": key} for key, label, _d in RANGES]), md=3),
            ], className="g-2 align-items-center"),
            dcc.Graph(id="diag-trend", config={"displaylogo": False}, style={"height": "360px"}),
            html.Small("Click anywhere on the history. Ticks along the bottom mark saved spectra.",
                       className="text-muted"),
        ], className="history-card"),
        html.Div([
            dbc.Row([
                dbc.Col(html.Div(id="diag-capture-info"), lg=5),
                dbc.Col([
                    button_radio("diag-view", [{"label": "Spectrum", "value": "spectrum"},
                                               {"label": "Waveform", "value": "waveform"},
                                               {"label": "Waterfall", "value": "waterfall"}], "spectrum"),
                    button_radio("diag-quantity", [{"label": "Acceleration", "value": "acceleration"},
                                                   {"label": "Velocity", "value": "velocity"},
                                                   {"label": "Envelope", "value": "envelope"}], "acceleration"),
                ], lg=7, className="d-flex flex-wrap gap-2 justify-content-lg-end"),
            ], className="g-2 align-items-center"),
            dbc.Row([
                dbc.Col(html.Div(id="diag-bearing", className="bearing-header"), md=8),
                dbc.Col([
                    button_radio("diag-scale", [{"label": "Linear", "value": "linear"},
                                                {"label": "Log", "value": "log"}], "linear"),
                    dbc.Button([html.I(className="fas fa-download me-1"), "CSV"], id="diag-download-btn",
                               color="secondary", outline=True, size="sm", className="ms-2"),
                    dcc.Download(id="diag-file"),
                ], md=4, className="d-flex justify-content-md-end align-items-center"),
            ], className="g-2 align-items-center mt-2"),
            dcc.Graph(id="diag-plot", config={"displaylogo": False}, style={"height": "460px"}),
            html.Div(id="diag-peaks"),
        ], className="history-card"),
        dbc.Button("Save the live spectrum as this channel's baseline", id="baseline-save", color="primary",
                   outline=True, className="mt-2"),
        html.Small(" The Baseline health agent compares new readings with it.", className="text-muted"),
        html.Div(id="baseline-result", className="mt-2"),
    ])


# ---------------------------------------------------------------- figures
def empty_figure(text):
    figure = go.Figure().update_layout(template="plotly_white", height=360, margin=dict(l=40, r=20, t=30, b=30),
                                       xaxis=dict(visible=False), yaxis=dict(visible=False))
    figure.add_annotation(text=text, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                          font=dict(color="#52647a", size=14))
    return figure


def trend_figure(series, captures, selected_ms):
    """One y axis per reading, alternating left and right, like a multi-parameter trend."""
    figure = go.Figure()
    names = [name for name in series if series[name]["x"]]
    if not names:
        return empty_figure("No history yet for this channel and time range.")
    left = (len(names) + 1) // 2
    right = len(names) // 2
    domain = [AXIS_STEP * (left - 1), 1 - AXIS_STEP * max(right - 1, 0)]
    layout = {"xaxis": dict(domain=domain, type="date", showspikes=True, spikemode="across", spikethickness=1)}
    for i, name in enumerate(names):
        axis = "y" if i == 0 else f"y{i + 1}"
        key = "yaxis" if i == 0 else f"yaxis{i + 1}"
        side = "left" if i % 2 == 0 else "right"
        k = i // 2
        spec = dict(title=dict(text=f"{LABELS[name]} {series[name]['unit']}".strip(), font=dict(color=COLOURS[name])),
                    tickfont=dict(color=COLOURS[name]), side=side, domain=[0.24, 1], showgrid=i == 0, zeroline=False)
        if i:
            spec.update(overlaying="y")
        if k:
            spec.update(anchor="free", position=domain[0] - AXIS_STEP * k if side == "left" else domain[1] + AXIS_STEP * k)
        elif side == "right":
            spec.update(anchor="x")
        layout[key] = spec
        figure.add_trace(go.Scatter(x=series[name]["x"], y=series[name]["y"], name=LABELS[name], yaxis=axis,
                                    mode="lines", line=dict(color=COLOURS[name], width=1.6),
                                    hovertemplate=f"{LABELS[name]}: %{{y:.4g}} {series[name]['unit']}<extra></extra>"))
    if captures:
        layout["yaxis99"] = dict(domain=[0, 0.06], visible=False, range=[0, 1])
        figure.add_trace(go.Scatter(x=captures, y=[0.5] * len(captures), yaxis="y99", mode="markers", name="Saved spectra",
                                    marker=dict(symbol="line-ns-open", size=12, color="#1f3b73", line=dict(width=2)),
                                    hovertemplate="Saved spectrum<extra></extra>"))
    if selected_ms:
        moment = datetime.fromtimestamp(selected_ms / 1000)
        figure.add_vline(x=moment, line=dict(color="#126b82", width=1.5, dash="dash"))
        figure.add_annotation(x=moment, y=1, yref="paper", text=moment.strftime("%Y-%m-%d %H:%M:%S"), showarrow=False,
                              yanchor="bottom", font=dict(size=11, color="#126b82"))
    figure.update_layout(template="plotly_white", height=360, margin=dict(l=50, r=50, t=30, b=40),
                         hovermode="x unified", showlegend=False, clickmode="event", **layout)
    return figure


def bearing_header(info):
    found = (info or {}).get("bearing_orders")
    if not found:
        return None
    parts = [html.Strong(found["designation"], className="me-3")]
    for key in ("ftf", "bsf", "bpfo", "bpfi"):
        parts += [html.Span(key.upper(), style={"color": BEARING_COLOURS[key], "fontWeight": 650}),
                  html.Span(f" {found[key]:.2f}x", className="me-3")]
    return parts


def capture_note(capture, clicked_ms):
    when = datetime.fromtimestamp(capture["timestamp_ms"] / 1000)
    text = [html.I(className="fas fa-circle me-2", style={"color": "#126b82", "fontSize": ".6rem"}),
            html.Strong(when.strftime("%d/%m/%Y %H:%M:%S")), html.Span(f" · {capture['source']}", className="text-muted")]
    if clicked_ms is not None:
        minutes = (capture["timestamp_ms"] - clicked_ms) / 60000
        if abs(minutes) >= 1:
            gap = round(abs(minutes))
            text.append(html.Div(f"Nearest saved capture: {gap} minute{'' if gap == 1 else 's'} "
                                 f"{'after' if minutes > 0 else 'before'} the moment you clicked.",
                                 className="small text-muted"))
    return text


# ---------------------------------------------------------------- callbacks
def register(app, get_connection, data_folder):

    @app.callback([Output("diag-gateway", "options"), Output("diag-gateway", "value")], Input("url", "pathname"))
    def diag_gateway_options(_path):
        with closing(get_connection()) as conn:
            gateways = [row[0] for row in conn.execute("SELECT DISTINCT gateway_id FROM channel_state ORDER BY 1")]
            options = []
            for gid in gateways:
                machine = historian.asset_settings(conn, gid, "1").get("machine")
                options.append({"label": f"{machine} — {gid}" if machine else gid, "value": gid})
        return options, (gateways[0] if gateways else None)

    @app.callback([Output("diag-axis", "options"), Output("diag-axis", "value")], Input("diag-gateway", "value"))
    def diag_axis_options(gid):
        if not gid:
            return [], None
        with closing(get_connection()) as conn:
            channels = [row[0] for row in conn.execute(
                "SELECT channel FROM channel_state WHERE gateway_id=? ORDER BY channel", (gid,))]
            options = []
            for ch in channels:
                location = historian.asset_settings(conn, gid, ch).get("location")
                options.append({"label": f"CH{ch}" + (f" · {location}" if location else ""), "value": ch})
        return options, (channels[0] if channels else None)

    @app.callback(Output("diag-channel", "data"), [Input("diag-gateway", "value"), Input("diag-axis", "value")])
    def diag_select_channel(gid, ch):
        return json.dumps([gid, ch]) if gid and ch else None

    @app.callback(Output("diag-time", "data"), [Input("diag-trend", "clickData"), Input("diag-channel", "data")],
                  prevent_initial_call=True)
    def diag_pick_time(click, _channel):
        if ctx.triggered_id != "diag-trend" or not click or not click.get("points"):
            return None  # a new channel starts at its latest capture
        try:
            moment = datetime.fromisoformat(str(click["points"][0]["x"]).replace("T", " "))
        except ValueError:
            return no_update
        return int(moment.timestamp() * 1000)

    @app.callback(Output("diag-trend", "figure"),
                  [Input("diag-channel", "data"), Input("diag-metrics", "value"), Input("diag-range", "value"),
                   Input("diag-time", "data")])
    def diag_trend(channel, metrics, span, selected_ms):
        if not channel:
            return empty_figure("Choose a VDS and channel.")
        gid, ch = json.loads(channel)
        days = dict((key, d) for key, _l, d in RANGES).get(span, 7)
        metrics = [m for m in (metrics or []) if m in COLOURS]
        with closing(get_connection()) as conn:
            newest = conn.execute("SELECT MAX(timestamp) FROM trends WHERE gateway_id=? AND channel=?",
                                  (gid, ch)).fetchone()[0]
            if not newest:
                return empty_figure("No history yet for this channel.")
            end = datetime.fromisoformat(str(newest))
            start = end.timestamp() - days * 86400
            series = {}
            for metric in metrics:
                rows = conn.execute(
                    "SELECT timestamp, value, context FROM trends WHERE gateway_id=? AND channel=? AND metric_name=? "
                    "AND timestamp >= ? ORDER BY timestamp", (gid, ch, metric, datetime.fromtimestamp(start))).fetchall()
                stride = max(1, len(rows) // 2500)
                rows = rows[::stride]
                unit = json.loads(rows[-1][2] or "{}").get("unit", "") if rows else ""
                series[metric] = {"x": [str(r[0]) for r in rows], "y": [r[1] for r in rows], "unit": unit}
            captures = historian.capture_times(conn, gid, ch, int(start * 1000), int(end.timestamp() * 1000) + 60_000)
        return trend_figure(series, [datetime.fromtimestamp(ms / 1000) for ms in captures], selected_ms)

    def spectrum_data(conn, gid, ch, selected_ms, quantity, scale):
        """(figure, peak rows, capture, notes) for the spectrum view."""
        info, settings = (ai_views.machine_info(conn, gid, ch) if historian.edge_ai_enabled() else (None, {}))
        live = historian.get_json(conn, "channel_state", "state", gid, ch, {}).get("rpm")
        if quantity == "envelope":
            capture = historian.nearest_capture(conn, gid, ch, "waveform", selected_ms)
            if not capture:
                return None
            x, y = envelope_spectrum(capture["values"], capture["sampling_freq"])
            ylabel, families = "Envelope power (device units)", ["speed", "bearing"]
        else:
            capture = historian.nearest_capture(conn, gid, ch, "spectrum", selected_ms)
            if not capture:
                return None
            x, y = spectrum_xy(dict(spectrum_data=capture["values"].tolist(), bandwidth=capture["bandwidth"],
                                    lines=capture["lines"]))
            ylabel, families = "Published power spectrum (device units)", ["speed", "bearing", "gear", "electrical"]
            if quantity == "velocity":
                sensor = str(capture["context"].get("sensor_type") or "").lower()
                if "accel" not in sensor:
                    raise ValueError("Velocity is derived from an acceleration spectrum; this sensor reports "
                                     f"{capture['context'].get('sensor_type') or 'an unknown type'}.")
                y = velocity_from_acceleration(x, y)
                ylabel = "Velocity (mm/s, derived from the g² spectrum)"
        rpm = capture.get("rpm")
        if info is not None:
            rpm, _where = ai_views.resolve_rpm(settings, rpm or live)
        elif not rpm:
            rpm = live if live and live > 0 else None
        when = datetime.fromtimestamp(capture["timestamp_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        title = f"{'Envelope spectrum' if quantity == 'envelope' else 'Spectrum'} ({when})" + (
            f" · {rpm:.0f} RPM" if rpm else "")
        figure, rows = spectrum_figure(x, y, title, rpm, info, families, 3, scale == "log")
        figure.update_yaxes(title=ylabel)
        top = figure.layout.xaxis.range[1] if figure.layout.xaxis.range else float(np.max(x))
        add_cpm_axis(figure, top)
        return figure, rows, capture, info

    @app.callback([Output("diag-plot", "figure"), Output("diag-capture-info", "children"), Output("diag-peaks", "children"),
                   Output("diag-bearing", "children"), Output("diag-ai", "children")],
                  [Input("diag-channel", "data"), Input("diag-time", "data"), Input("diag-view", "value"),
                   Input("diag-quantity", "value"), Input("diag-scale", "value")])
    def diag_view_capture(channel, selected_ms, view, quantity, scale):
        if not channel:
            return empty_figure("Choose a VDS and channel."), None, None, None, None
        gid, ch = json.loads(channel)
        ai_box = ai_views.finding_box(data_folder, gid, ch) if historian.edge_ai_enabled() else None
        with closing(get_connection()) as conn:
            try:
                if view == "waterfall":
                    items = historian.recent_spectra(conn, gid, ch, selected_ms or int(time.time() * 1000) + 86_400_000)
                    figure = waterfall_figure(items)
                    figure.update_layout(height=460)
                    note = f"Last {len(items)} saved spectra" + (" up to the moment you clicked." if selected_ms else ".")
                    return figure, html.Span(note, className="text-muted"), None, None, ai_box
                if view == "waveform":
                    capture = historian.nearest_capture(conn, gid, ch, "waveform", selected_ms)
                    if not capture:
                        raise ValueError("No saved waveform for this channel yet.")
                    values, fs = capture["values"], capture["sampling_freq"]
                    figure = go.Figure(go.Scatter(x=np.arange(values.size) / fs, y=values, mode="lines",
                                                  line=dict(color="#1f3b73", width=1)))
                    figure.update_layout(template="plotly_white", height=460, margin=dict(l=60, r=20, t=50, b=50),
                                         title="Waveform", xaxis_title="Time (s)",
                                         yaxis_title="Waveform (sensor units)")
                    stats = waveform_stats(values)
                    summary = html.Div(" · ".join([f"RMS {stats['rms']:.4g}", f"Peak {stats['peak']:.4g}",
                                                   f"Peak-peak {stats['peak_to_peak']:.4g}",
                                                   f"Crest factor {stats['crest']:.2f}",
                                                   f"Kurtosis {stats['kurtosis']:.2f}"]), className="small text-muted")
                    return figure, capture_note(capture, selected_ms) + [summary], None, None, ai_box
                result = spectrum_data(conn, gid, ch, selected_ms, quantity, scale)
                if result is None:
                    raise ValueError("No saved spectrum for this channel yet. Snapshots are kept every 30 minutes "
                                     "by default, and during recordings.")
                figure, rows, capture, info = result
                return (figure, capture_note(capture, selected_ms), ai_views.peak_table(rows), bearing_header(info),
                        ai_box)
            except ValueError as error:
                return empty_figure(str(error)), None, None, None, ai_box

    @app.callback(Output("diag-file", "data"), Input("diag-download-btn", "n_clicks"),
                  [State("diag-channel", "data"), State("diag-time", "data"), State("diag-view", "value")],
                  prevent_initial_call=True)
    def diag_download(_n, channel, selected_ms, view):
        if not channel:
            return no_update
        gid, ch = json.loads(channel)
        kind = "waveform" if view == "waveform" else "spectrum"
        with closing(get_connection()) as conn:
            capture = historian.nearest_capture(conn, gid, ch, kind, selected_ms)
        if not capture:
            return no_update
        out = io.StringIO()
        writer = csv.writer(out)
        values = capture["values"]
        if kind == "spectrum":
            writer.writerow(["frequency_hz", "power_device_units"])
            step = capture["bandwidth"] / capture["lines"]
            writer.writerows((round(i * step, 6), float(v)) for i, v in enumerate(values))
        else:
            writer.writerow(["time_s", "value_sensor_units"])
            writer.writerows((round(i / capture["sampling_freq"], 6), float(v)) for i, v in enumerate(values))
        stamp = datetime.fromtimestamp(capture["timestamp_ms"] / 1000).strftime("%Y%m%d-%H%M%S")
        return dict(content=out.getvalue(), filename=f"{gid}_CH{ch}_{kind}_{stamp}.csv")
