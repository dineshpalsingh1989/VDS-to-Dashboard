"""Machine history: an asset tree, overall values, a multi-reading trend, and the spectrum and
waveform saved closest to any moment you click.

Spectra and waveforms come from the history snapshots (one per channel every
VDS_SNAPSHOT_INTERVAL_S, default 30 minutes) and from recordings. Bearing defect lines,
the bearing entry and manual speed are diagnostics for every install; the AI finding
appears only on IRIV Edge AI installs.
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
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from . import ai_views, historian
from .diagnostics import waterfall_figure
from .fault_frequencies import marker_lines, nearest_fault
from .signals import envelope_spectrum, velocity_from_acceleration, waveform_stats
from .spectrum_view import add_cpm_axis, add_harmonic_markers, spectrum_figure
from .vds import freshness, spectrum_xy

READ_CALLBACKS = {"diag_tree_tab", "diag_measurements", "diag_measure_pick", "diag_tree", "diag_init", "diag_tree_pick", "diag_axis_options", "diag_select_channel",
                  "diag_overview", "diag_cell_pick", "diag_pick_time", "diag_trend", "diag_view_capture",
                  "diag_bearing_load", "diag_download", "diag_bearing_matches", "diag_bearing_pick"}
# (metric name in the historian, chip label, colour)
METRICS = (("Acceleration", "Acc RMS", "#1f77b4"), ("Velocity", "Vel RMS", "#ff7f0e"),
           ("True Peak", "0-P", "#d62728"), ("Displacement", "Disp", "#9467bd"),
           ("Crest Factor", "CF", "#8c564b"), ("Std Deviation", "Std dev", "#e377c2"))
COLOURS = {name: colour for name, _label, colour in METRICS}
LABELS = {name: label for name, label, _colour in METRICS}
RANGES = (("1d", "1 day", 1), ("7d", "1 week", 7), ("30d", "1 month", 30), ("90d", "3 months", 90),
          ("max", "Max", 3650))
BEARING_COLOURS = {"ftf": "#8c564b", "bsf": "#e8930c", "bpfo": "#1f8ed6", "bpfi": "#e3267a"}
AXIS_STEP = 0.065


def button_radio(component_id, options, value, class_name="btn-tabs"):
    return dbc.RadioItems(id=component_id, options=options, value=value, inline=True, className=class_name,
                          inputClassName="btn-check", labelClassName="btn btn-outline-primary btn-sm",
                          labelCheckedClassName="active")


def tool_toggles(component_id, options, value):
    """Icon toggle buttons, like a plot toolbar."""
    return dbc.Checklist(id=component_id, options=options, value=value, inline=True, className="tool-toggles",
                         inputClassName="btn-check", labelClassName="btn btn-sm tool-btn", labelCheckedClassName="active")


def icon(name, text=None):
    return html.Span([html.I(className=f"fas {name}"), html.Span(text, className="ms-1") if text else None])


def page():
    return html.Div([
        html.Div(id="diag-breadcrumb", className="mh-breadcrumb"),
        dcc.Store(id="diag-gateway"), dcc.Store(id="diag-channel"), dcc.Store(id="diag-time"),
        dbc.Row([
            dbc.Col(html.Div([
                button_radio("diag-tree-tab", [{"label": "Assets", "value": "assets"},
                                               {"label": "Measurements", "value": "measurements"}], "assets",
                             "btn-tabs panel-tabs"),
                html.Div([
                    dbc.Input(id="diag-search", placeholder="Search", size="sm", debounce=True,
                              className="my-2", type="search"),
                    html.Div(id="diag-tree", className="mh-tree"),
                ], id="diag-tree-assets"),
                html.Div(id="diag-measurements", className="mh-measurements", style={"display": "none"}),
            ], className="mh-panel"), lg=3),
            dbc.Col([
                dbc.Row([
                    dbc.Col(button_radio("diag-axis", [], None, "btn-tabs axis-tabs"), width="auto"),
                    dbc.Col(html.Div(id="diag-channel-name", className="mh-channel-name"), className="text-truncate"),
                    dbc.Col(html.Div(id="diag-status"), width="auto"),
                ], className="g-2 align-items-center mb-2"),
                html.Details([html.Summary("Overall values, all channels"),
                              html.Div(id="diag-overall", className="mt-2")], className="mh-card mh-overall"),
                html.Div(id="diag-ai"),
                html.Div([
                    dbc.Row([
                        dbc.Col(dbc.Checklist(id="diag-metrics", inline=True, className="metric-chips",
                                              options=[{"label": label, "value": name} for name, label, _c in METRICS],
                                              value=["Acceleration", "Velocity"], inputClassName="btn-check",
                                              labelClassName="metric-chip", labelCheckedClassName="active"), lg=7),
                        dbc.Col([
                            tool_toggles("diag-trend-tools", [{"label": html.Span("log y", className="tool-text"),
                                                               "value": "logy"}], []),
                            dcc.Dropdown(id="diag-range", clearable=False, value="7d", className="mh-range",
                                         options=[{"label": label, "value": key} for key, label, _d in RANGES]),
                        ], lg=5, className="d-flex gap-2 align-items-center justify-content-lg-end"),
                    ], className="g-2 align-items-center"),
                    dcc.Graph(id="diag-trend", config={"displaylogo": False}, style={"height": "220px"}),

                ], className="mh-card"),
                html.Div([
                    dbc.InputGroup([
                        dbc.InputGroupText("Speed:"),
                        dbc.Input(id="diag-speed", type="number", min=0, step="any", debounce=True,
                                  placeholder="auto", style={"maxWidth": "8rem"}),
                    ], size="sm", className="mh-speed"),
                    button_radio("diag-speed-unit", [{"label": "RPM", "value": "rpm"}, {"label": "Hz", "value": "hz"}],
                                 "rpm"),
                    button_radio("diag-view", [{"label": "Spectrum", "value": "spectrum"},
                                               {"label": "Waveform", "value": "waveform"},
                                               {"label": "Waterfall", "value": "waterfall"}], "spectrum"),
                    button_radio("diag-quantity", [{"label": "Velocity", "value": "velocity"},
                                                   {"label": "Acceleration", "value": "acceleration"},
                                                   {"label": "Envelope", "value": "envelope"}], "acceleration"),
                ], className="mh-controls"),
                html.Div([
                    dbc.Row([
                        dbc.Col(html.Div(id="diag-capture-info", className="capture-pill"), width="auto"),
                        dbc.Col(html.Div(id="diag-rms", className="mh-rms"), className="text-end"),
                    ], className="g-2 align-items-center"),
                    dbc.Row([
                        dbc.Col(html.Div(id="diag-bearing", className="bearing-header"), lg=7),
                        dbc.Col([
                            tool_toggles("diag-tools", [
                                {"label": html.Span("log x", className="tool-text"), "value": "logx"},
                                {"label": html.Span("log y", className="tool-text"), "value": "logy"},
                                {"label": icon("fa-grip-lines-vertical"), "value": "harmonics"},
                                {"label": icon("fa-circle-notch"), "value": "bearing"},
                                {"label": icon("fa-caret-down"), "value": "peaks"},
                            ], ["harmonics", "bearing", "peaks"]),
                            dbc.Button(html.I(className="fas fa-download"), id="diag-download-btn", color="light",
                                       size="sm", className="tool-btn ms-1", title="Download CSV"),
                            dcc.Download(id="diag-file"),
                        ], lg=5, className="d-flex justify-content-lg-end align-items-center"),
                    ], className="g-2 align-items-center mt-1"),
                    html.Details([
                        html.Summary([html.I(className="fas fa-life-ring me-1"), "Bearing database"], className="small"),
                        html.Div([
                            html.Div([html.I(className="fas fa-exclamation-triangle me-2"),
                                      "Bearing catalogue may be inaccurate. Verify before use."], className="bearing-warning"),
                            html.Div("Selected", className="bearing-label"),
                            html.Div(id="diag-bearing-selected", className="bearing-selected"),
                            html.Div("Search", className="bearing-label"),
                            dbc.InputGroup([
                                dcc.Input(id="diag-bearing-input", list="diag-bearing-list", debounce=False, maxLength=64,
                                          placeholder="Search by designation, e.g. 6205-2RS",
                                          className="form-control form-control-sm"),
                                dbc.Button("Save", id="diag-bearing-save", size="sm", color="primary"),
                            ], size="sm"),
                            html.Datalist(id="diag-bearing-list"),
                            html.Div(id="diag-bearing-matches", className="bearing-matches"),
                            html.Div(id="diag-bearing-result", className="small mt-1"),
                        ], className="bearing-db"),
                    ], className="mh-bearing-edit"),
                    dcc.Graph(id="diag-plot", config={"displaylogo": False}, style={"height": "300px"}),
                    html.Div(id="diag-peaks"),
                ], className="mh-card"),
                dbc.Button("Save the live spectrum as this channel's baseline", id="baseline-save", color="primary",
                           outline=True, size="sm", className="mt-1"),
                html.Small(" The Baseline health agent compares new readings with it.", className="text-muted"),
                html.Div(id="baseline-result", className="mt-2"),
            ], lg=9),
        ], className="g-3"),
    ])


# ---------------------------------------------------------------- figures
def empty_figure(text, height=300):
    figure = go.Figure().update_layout(template="plotly_white", height=height, margin=dict(l=40, r=20, t=30, b=30),
                                       xaxis=dict(visible=False), yaxis=dict(visible=False))
    figure.add_annotation(text=text, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                          font=dict(color="#52647a", size=14))
    return figure


def trend_figure(series, captures, selected_ms, logy=False):
    """One y axis per reading, alternating left and right, with a range slider underneath."""
    figure = go.Figure()
    names = [name for name in series if series[name]["x"]]
    if not names:
        return empty_figure("No history yet for this channel and time range.", 300)
    left, right = (len(names) + 1) // 2, len(names) // 2
    domain = [AXIS_STEP * (left - 1), 1 - AXIS_STEP * max(right - 1, 0)]
    layout = {"xaxis": dict(domain=domain, type="date", showspikes=True, spikemode="across", spikethickness=1,
                            rangeslider=dict(visible=True, thickness=0.07, bgcolor="#f1f5f9"))}
    for i, name in enumerate(names):
        axis, key = ("y", "yaxis") if i == 0 else (f"y{i + 1}", f"yaxis{i + 1}")
        side, k = ("left" if i % 2 == 0 else "right"), i // 2
        spec = dict(title=dict(text=f"{LABELS[name]} {series[name]['unit']}".strip(), font=dict(color=COLOURS[name])),
                    tickfont=dict(color=COLOURS[name]), side=side, domain=[0.0, 1], showgrid=i == 0, zeroline=False,
                    type="log" if logy else "linear", rangemode="tozero" if not logy else "normal")
        if i:
            spec.update(overlaying="y")
        if k:
            spec.update(anchor="free", position=domain[0] - AXIS_STEP * k if side == "left" else domain[1] + AXIS_STEP * k)
        elif side == "right":
            spec.update(anchor="x")
        layout[key] = spec
        colour = COLOURS[name]
        figure.add_trace(go.Scatter(
            x=series[name]["x"], y=series[name]["y"], name=LABELS[name], yaxis=axis, mode="lines",
            line=dict(color=colour, width=1.6), fill="tozeroy" if i == 0 else None,
            fillcolor="rgba(32,48,71,0.07)" if i == 0 else None,
            hovertemplate=f"{LABELS[name]}: %{{y:.4g}} {series[name]['unit']}<extra></extra>"))
    if captures:
        layout["yaxis99"] = dict(domain=[0, 0.06], visible=False, range=[0, 1])  # ticks just inside the bottom
        figure.add_trace(go.Scatter(x=captures, y=[0.5] * len(captures), yaxis="y99", mode="markers", name="Saved spectra",
                                    marker=dict(symbol="line-ns-open", size=12, color="#1f3b73", line=dict(width=2)),
                                    hovertemplate="Saved spectrum<extra></extra>"))
    if selected_ms:
        moment = datetime.fromtimestamp(selected_ms / 1000)
        figure.add_vline(x=moment, line=dict(color="#2a7fd4", width=1.5, dash="dash"))
        figure.add_annotation(x=moment, y=1, yref="paper", text=f"▼ {moment:%d/%m/%Y %H:%M:%S}", showarrow=False,
                              yanchor="bottom", font=dict(size=11, color="#2a7fd4"))
    figure.update_layout(template="plotly_white", height=220, margin=dict(l=50, r=50, t=26, b=10),
                         hovermode="x unified", showlegend=False, clickmode="event", **layout)
    return figure


def bearing_header(found):
    if not found:
        return html.Span("No bearing set: open 'Bearing at this measurement point' below to type one.",
                         className="text-muted small")
    parts = [html.Strong(found["designation"], className="me-3")]
    for key in ("ftf", "bsf", "bpfo", "bpfi"):
        parts += [html.Span(key.upper(), style={"color": BEARING_COLOURS[key], "fontWeight": 700}),
                  html.Span(f" {found[key]:.2f}×", className="me-3")]
    return parts


def capture_pill(capture, clicked_ms):
    when = datetime.fromtimestamp(capture["timestamp_ms"] / 1000)
    pill = [html.I(className="fas fa-circle me-2", style={"color": "#2a7fd4", "fontSize": ".6rem"}),
            html.Strong(f"{when:%d/%m/%Y, %H:%M:%S}"), html.Span(f" · {capture['source']}", className="text-muted")]
    if clicked_ms is not None:
        minutes = (capture["timestamp_ms"] - clicked_ms) / 60000
        gap = round(abs(minutes))
        if gap >= 1:
            pill.append(html.Div(f"Nearest saved capture: {gap} minute{'' if gap == 1 else 's'} "
                                 f"{'after' if minutes > 0 else 'before'} the moment you clicked.",
                                 className="small text-muted"))
    return pill


def overall_value(conn, gid, ch, metric, at_ms):
    """The VDS's own overall reading closest to a capture time, for the RMS shown with the plot."""
    stamp = datetime.fromtimestamp(at_ms / 1000)
    rows = [conn.execute(f"SELECT value, context FROM trends WHERE gateway_id=? AND channel=? AND metric_name=? "
                         f"AND timestamp {op} ? ORDER BY timestamp {order} LIMIT 1", (gid, ch, metric, stamp)).fetchone()
            for op, order in (("<=", "DESC"), (">=", "ASC"))]
    row = next((r for r in rows if r), None)
    if not row:
        return None
    return row[0], json.loads(row[1] or "{}").get("unit", "")


# ---------------------------------------------------------------- callbacks
def register(app, get_connection, data_folder):

    def gateways(conn):
        result = []
        for gid in [row[0] for row in conn.execute("SELECT DISTINCT gateway_id FROM channel_state ORDER BY 1")]:
            channels = []
            for ch, body in conn.execute("SELECT channel, state FROM channel_state WHERE gateway_id=? ORDER BY channel",
                                         (gid,)):
                state = json.loads(body or "{}")
                asset = historian.asset_settings(conn, gid, ch)
                channels.append({"ch": ch, "location": asset.get("location", ""), "machine": asset.get("machine", ""),
                                 "site": asset.get("site", ""), "fresh": freshness(state)["status"] == "fresh"})
            result.append({"gid": gid, "channels": channels})
        return result

    @app.callback([Output("diag-tree-assets", "style"), Output("diag-measurements", "style")],
                  Input("diag-tree-tab", "value"))
    def diag_tree_tab(tab):
        hidden = {"display": "none"}
        return (hidden, {}) if tab == "measurements" else ({}, hidden)

    @app.callback(Output("diag-measurements", "children"), [Input("diag-channel", "data"), Input("diag-time", "data")])
    def diag_measurements(channel, selected_ms):
        """Every saved spectrum for the channel, newest first; clicking one opens it."""
        if not channel:
            return html.Small("Choose a channel.", className="text-muted")
        gid, ch = json.loads(channel)
        with closing(get_connection()) as conn:
            times = historian.capture_times(conn, gid, ch, 0, int(time.time() * 1000) + 86_400_000)[-300:][::-1]
        if not times:
            return html.Small("No saved spectra for this channel yet.", className="text-muted mt-2 d-block")
        nearest = min(times, key=lambda ms: abs(ms - selected_ms)) if selected_ms else times[0]
        items, day = [html.Div(f"CH{ch} · {len(times)} saved spectra", className="meas-head")], None
        for ms in times:
            moment = datetime.fromtimestamp(ms / 1000)
            if moment.date() != day:
                day = moment.date()
                items.append(html.Div(f"{moment:%d %b %Y}", className="meas-day"))
            items.append(html.Button([html.I(className="fas fa-chart-bar me-2"), f"{moment:%H:%M:%S}"],
                                     id={"type": "diag-meas", "ms": ms}, n_clicks=0,
                                     className="tree-leaf" + (" active" if ms == nearest else "")))
        return items

    @app.callback(Output("diag-time", "data", allow_duplicate=True),
                  Input({"type": "diag-meas", "ms": ALL}, "n_clicks"), prevent_initial_call=True)
    def diag_measure_pick(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update
        return int(trigger["ms"])

    @app.callback(Output("diag-tree", "children"), [Input("diag-search", "value"), Input("diag-channel", "data")])
    def diag_tree(search, channel):
        current = json.loads(channel) if channel else [None, None]
        term = (search or "").strip().lower()
        with closing(get_connection()) as conn:
            items = gateways(conn)
        # Site > machine > device serial number > channels, like an asset tree in a CMMS.
        machines = {}
        for item in items:
            machine = next((c["machine"] for c in item["channels"] if c["machine"]), "")
            site = next((c["site"] for c in item["channels"] if c["site"]), "")
            leaves = []
            for c in item["channels"]:
                text = f"CH{c['ch']}" + (f" · {c['location']}" if c["location"] else "")
                if term and term not in f"{item['gid']} {machine} {site} {text}".lower():
                    continue
                active = current == [item["gid"], c["ch"]]
                leaves.append(html.Button([html.Span(className=f"tree-dot {'ok' if c['fresh'] else 'off'}"),
                                           html.I(className="fas fa-signal tree-icon tree-axis"), text],
                                          id={"type": "diag-tree", "gid": item["gid"], "ch": c["ch"]},
                                          className="tree-leaf" + (" active" if active else ""), n_clicks=0))
            if leaves:
                machines.setdefault(machine, []).append(html.Details([
                    html.Summary([html.I(className="fas fa-barcode me-2 tree-icon"), html.Span(item["gid"]),
                                  html.Small(" SN", className="text-muted ms-1")], title=f"Device serial number {item['gid']}"),
                    html.Div(leaves, className="tree-leaves")], open=True, className="tree-node tree-device"))
        nodes = [html.Details([
            html.Summary([html.I(className="fas fa-cog me-2 tree-icon"),
                          html.Span(machine or "Machine not named", className="fw-semibold" if machine else "text-muted")]),
            html.Div(devices)], open=True, className="tree-node") for machine, devices in sorted(
                machines.items(), key=lambda pair: (not pair[0], pair[0].lower()))]
        if not nodes:
            return html.Small("No machine matches." if term else "No VDS has sent data yet.", className="text-muted")
        site = next((c["site"] for item in items for c in item["channels"] if c["site"]), "")
        return [html.Div([html.I(className="fas fa-map-marker-alt me-2 tree-icon"), site or "Site"],
                         className="tree-site")] + nodes

    @app.callback([Output("diag-gateway", "data"), Output("diag-axis", "value")], Input("url", "pathname"))
    def diag_init(_path):
        with closing(get_connection()) as conn:
            row = conn.execute("SELECT gateway_id, MIN(channel) FROM channel_state GROUP BY gateway_id "
                               "ORDER BY gateway_id LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (None, None)

    @app.callback([Output("diag-gateway", "data", allow_duplicate=True), Output("diag-axis", "value", allow_duplicate=True)],
                  Input({"type": "diag-tree", "gid": ALL, "ch": ALL}, "n_clicks"), prevent_initial_call=True)
    def diag_tree_pick(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update, no_update
        return trigger["gid"], trigger["ch"]

    @app.callback(Output("diag-axis", "options"), Input("diag-gateway", "data"))
    def diag_axis_options(gid):
        if not gid:
            return []
        with closing(get_connection()) as conn:
            item = next((g for g in gateways(conn) if g["gid"] == gid), {"channels": []})
        return [{"label": html.Span([html.I(className=f"fas fa-wave-square me-1 {'text-success' if c['fresh'] else 'text-danger'}"),
                                     f"CH{c['ch']}"]), "value": c["ch"]} for c in item["channels"]]

    @app.callback(Output("diag-channel", "data"), [Input("diag-gateway", "data"), Input("diag-axis", "value")])
    def diag_select_channel(gid, ch):
        return json.dumps([gid, ch]) if gid and ch else None

    @app.callback([Output("diag-breadcrumb", "children"), Output("diag-channel-name", "children"),
                   Output("diag-overall", "children"), Output("diag-status", "children")],
                  [Input("diag-channel", "data"), Input("diag-metrics", "value")])
    def diag_overview(channel, metrics):
        if not channel:
            return None, None, html.Small("Waiting for the first VDS message.", className="text-muted"), None
        gid, ch = json.loads(channel)
        with closing(get_connection()) as conn:
            asset = historian.asset_settings(conn, gid, ch)
            severity = historian.get_json(conn, "alarm_state", "state", gid, ch, {}).get("severity") or "not set"
            item = next((g for g in gateways(conn) if g["gid"] == gid), {"channels": []})
            latest = {(c, m): (v, json.loads(ctx_ or "{}").get("unit", "")) for c, m, v, ctx_ in conn.execute(
                "SELECT channel, metric_name, value, context FROM trends_latest WHERE gateway_id=?", (gid,))}
        crumbs = [p for p in (asset.get("site"), asset.get("machine"), gid, f"CH{ch}") if p]
        breadcrumb = [html.Span([html.Span(text, className="crumb" + (" current" if i == len(crumbs) - 1 else "")),
                                 html.Span(" / ", className="crumb-sep") if i < len(crumbs) - 1 else None])
                      for i, text in enumerate(crumbs)]
        name = " / ".join(p for p in (asset.get("machine"), asset.get("location"), asset.get("direction")) if p)
        channels = [c["ch"] for c in item["channels"]]
        rows = []
        for metric, label, _colour in METRICS:
            if not any((c, metric) in latest for c in channels):
                continue
            unit = next((latest[(c, metric)][1] for c in channels if (c, metric) in latest), "")
            cells = [html.Td(html.Span([label, html.Small(f" {unit}", className="text-muted")]), className="mh-row-label")]
            for c in channels:
                value = latest.get((c, metric))
                active = c == ch and metric in (metrics or [])
                cells.append(html.Td(html.Button(f"{value[0]:.4g}" if value else "—",
                                                 id={"type": "diag-cell", "ch": c, "metric": metric}, n_clicks=0,
                                                 className="overall-cell" + (" active" if active else "")) if value
                                     else html.Span("—", className="text-muted")))
            rows.append(html.Tr(cells))
        head = html.Thead(html.Tr([html.Th("Overall")] + [html.Th([html.Span(className="tree-dot ok me-1"), f"CH{c}"])
                                                          for c in channels]))
        table = dbc.Table([head, html.Tbody(rows)], size="sm", borderless=True, className="mb-0 overall-table") if rows \
            else html.Small("No overall readings yet.", className="text-muted")
        tone = {"alert": "status-alert", "warning": "status-warning", "normal": "status-fresh"}.get(severity, "status-unknown")
        status = html.Span(["Alarm: ", severity.capitalize()], className=f"status-pill {tone}", title="Alarm state of this channel")
        header = html.Span([html.I(className="fas fa-check-circle me-1 text-muted"), html.Strong(gid, title="Device serial number"),
                            html.Span(f"  {name}", className="text-muted")])
        return (breadcrumb, header,
                table, status)

    @app.callback([Output("diag-axis", "value", allow_duplicate=True), Output("diag-metrics", "value")],
                  Input({"type": "diag-cell", "ch": ALL, "metric": ALL}, "n_clicks"), State("diag-metrics", "value"),
                  prevent_initial_call=True)
    def diag_cell_pick(clicks, metrics):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update, no_update
        chosen = list(metrics or [])
        if trigger["metric"] not in chosen:
            chosen.append(trigger["metric"])
        return trigger["ch"], chosen

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
                   Input("diag-time", "data"), Input("diag-trend-tools", "value")])
    def diag_trend(channel, metrics, span, selected_ms, tools):
        if not channel:
            return empty_figure("Choose a machine and channel.", 300)
        gid, ch = json.loads(channel)
        days = dict((key, d) for key, _l, d in RANGES).get(span, 7)
        metrics = [m for m in (metrics or []) if m in COLOURS]
        with closing(get_connection()) as conn:
            newest = conn.execute("SELECT MAX(timestamp) FROM trends WHERE gateway_id=? AND channel=?",
                                  (gid, ch)).fetchone()[0]
            if not newest:
                return empty_figure("No history yet for this channel.", 300)
            end = datetime.fromisoformat(str(newest))
            start = max(end.timestamp() - days * 86400, 0)
            series = {}
            for metric in metrics:
                rows = conn.execute(
                    "SELECT timestamp, value, context FROM trends WHERE gateway_id=? AND channel=? AND metric_name=? "
                    "AND timestamp >= ? ORDER BY timestamp", (gid, ch, metric, datetime.fromtimestamp(start))).fetchall()
                rows = rows[::max(1, len(rows) // 2500)]
                unit = json.loads(rows[-1][2] or "{}").get("unit", "") if rows else ""
                series[metric] = {"x": [str(r[0]) for r in rows], "y": [r[1] for r in rows], "unit": unit}
            captures = historian.capture_times(conn, gid, ch, int(start * 1000), int(end.timestamp() * 1000) + 60_000)
        return trend_figure(series, [datetime.fromtimestamp(ms / 1000) for ms in captures], selected_ms,
                            "logy" in (tools or []))

    @app.callback([Output("diag-bearing-input", "value"), Output("diag-bearing-list", "children")],
                  Input("diag-channel", "data"))
    def diag_bearing_load(channel):
        if not channel:
            return "", []
        gid, ch = json.loads(channel)
        with closing(get_connection()) as conn:
            bearing = historian.ai_channel_settings(conn, gid, ch).get("bearing", "")
            names = ai_views.designations(ai_views.library(conn))
        return bearing, [html.Option(value=name) for name in names]

    @app.callback([Output("diag-bearing-matches", "children"), Output("diag-bearing-selected", "children")],
                  [Input("diag-bearing-input", "value"), Input("diag-channel", "data"), Input("diag-bearing-result", "children")])
    def diag_bearing_matches(text, channel, _saved):
        """Bearings in the library that match what is typed, and the bearing saved for this channel."""
        with closing(get_connection()) as conn:
            bearings = ai_views.library(conn)
            saved = historian.ai_channel_settings(conn, *json.loads(channel)).get("bearing", "") if channel else ""
        unique = {b["designation"]: b for b in bearings.values()}
        term = "".join(str(text or "").split()).upper()
        names = sorted((n for n in unique if term in n.upper().replace(" ", "")), key=lambda n: (not n.upper().startswith(term), n))
        rows = [html.Button([html.Span(name, className="fw-semibold"),
                             html.Small(f"BPFO {unique[name]['bpfo']:.2f}x · BPFI {unique[name]['bpfi']:.2f}x",
                                        className="text-muted ms-auto")],
                            id={"type": "diag-bearing-pick", "name": name}, n_clicks=0,
                            className="bearing-match" + (" active" if name == saved else ""),
                            title=unique[name].get("source", "")) for name in names[:30]]
        if not rows:
            rows = [html.Small("No bearing in the library matches. You can still save it.", className="text-muted")]
        found = ai_views.find_bearing(bearings, saved) if saved else None
        selected = html.Span([html.Small("Bearing (1)", className="text-muted me-2"), html.Strong(saved),
                              html.Small(f"  {found['designation']} geometry" if found and found["designation"] != saved else "",
                                         className="text-muted")]) if saved else             html.Span([html.Small("Bearing (1)", className="text-muted me-2"), "—"])
        return rows, selected

    @app.callback(Output("diag-bearing-input", "value", allow_duplicate=True),
                  Input({"type": "diag-bearing-pick", "name": ALL}, "n_clicks"), prevent_initial_call=True)
    def diag_bearing_pick(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update
        return trigger["name"]

    @app.callback(Output("diag-bearing-result", "children"), Input("diag-bearing-save", "n_clicks"),
                  [State("diag-channel", "data"), State("diag-bearing-input", "value")], prevent_initial_call=True)
    def diag_bearing_save(_n, channel, bearing):
        if not channel:
            return no_update
        gid, ch = json.loads(channel)
        try:
            with closing(get_connection()) as conn, conn:
                found = ai_views.find_bearing(ai_views.library(conn), bearing) if bearing else None
                historian.save_channel_bearing(conn, gid, ch, bearing)
        except ValueError as error:
            return html.Span(str(error), className="text-danger")
        if bearing and not found:
            return html.Span(f"Saved {bearing}, but it is not in the bearing library, so no lines can be drawn. "
                             "Add it to iriv-edge/knowledge/bearings.csv, or in the asset registry on IRIV Edge AI.",
                             className="text-warning")
        return html.Span(f"Saved {bearing or 'no bearing'} for {gid} CH{ch}.", className="text-success")

    def speed_for(conn, gid, ch, capture, speed, unit):
        """Typed speed first, then the fixed speed set for the channel, the capture's speed or the live speed."""
        if speed:
            value = float(speed) * (60 if unit == "hz" else 1)
            return (value, "typed") if value > 0 else (None, "no speed")
        settings = historian.ai_channel_settings(conn, gid, ch)
        live = historian.get_json(conn, "channel_state", "state", gid, ch, {}).get("rpm")
        if settings.get("speed_source") == "fixed" and settings.get("fixed_rpm"):
            return float(settings["fixed_rpm"]), "fixed speed"
        for value, where in ((capture.get("rpm"), "capture"), (live, "VDS speed sensor"),
                             (settings.get("fixed_rpm"), "fixed speed")):
            if value and value > 0:
                return float(value), where
        return None, "no speed"

    def spectrum_view(conn, gid, ch, selected_ms, quantity, tools, speed, unit):
        info, _settings = ai_views.machine_info(conn, gid, ch)
        if quantity == "envelope":
            capture = historian.nearest_capture(conn, gid, ch, "waveform", selected_ms)
            if not capture:
                return None
            x, y = envelope_spectrum(capture["values"], capture["sampling_freq"])
            ylabel = "Envelope (device units)"
        else:
            capture = historian.nearest_capture(conn, gid, ch, "spectrum", selected_ms)
            if not capture:
                return None
            x, y = spectrum_xy(dict(spectrum_data=capture["values"].tolist(), bandwidth=capture["bandwidth"],
                                    lines=capture["lines"]))
            ylabel = "Acceleration (published power spectrum, device units)"
            if quantity == "velocity":
                sensor = str(capture["context"].get("sensor_type") or "").lower()
                if "accel" not in sensor:
                    raise ValueError("Velocity is derived from an acceleration spectrum; this sensor reports "
                                     f"{capture['context'].get('sensor_type') or 'an unknown type'}.")
                y = velocity_from_acceleration(x, y)
                ylabel = "Velocity (mm/s, derived from the g² spectrum)"
        rpm, where = speed_for(conn, gid, ch, capture, speed, unit)
        tools = tools or []
        families = ["bearing", "gear", "electrical"] if "bearing" in tools else []
        figure, rows = spectrum_figure(x, y, "", rpm, info if families else None, families, 3,
                                       "logy" in tools, key_box=False)
        if not families:  # no fault lines asked for: drop the plain 1x line too, harmonics come below
            figure.layout.annotations, figure.layout.shapes = (), ()
        if "peaks" not in tools:
            figure.data = [trace for trace in figure.data if trace.name != "Peaks"]
        top = figure.layout.xaxis.range[1] if figure.layout.xaxis.range else float(np.max(x))
        if "harmonics" in tools and rpm:
            add_harmonic_markers(figure, x, y, rpm, top)
        if rows and rpm:  # name each peak after the harmonic or fault frequency it sits on
            lines = [("speed", f"{k}x", k * rpm / 60, "") for k in range(1, 41)]
            lines += marker_lines(info, rpm, set(families), 3, float(np.max(x))) if families else []
            for row in rows:
                row["match"] = nearest_fault(row["hz"], lines)
        if "logx" in tools:
            figure.update_xaxes(type="log", title="Frequency (Hz)", range=None)
        else:
            add_cpm_axis(figure, top)
            figure.update_xaxes(title="f [Hz + CPM]")
        figure.update_yaxes(title=ylabel)
        figure.update_layout(title=None, margin=dict(l=60, r=20, t=16, b=50), height=300)
        return figure, (rows if "peaks" in tools else []), capture, info, rpm, where

    @app.callback([Output("diag-plot", "figure"), Output("diag-capture-info", "children"), Output("diag-rms", "children"),
                   Output("diag-peaks", "children"), Output("diag-bearing", "children"), Output("diag-ai", "children"),
                   Output("diag-speed", "placeholder")],
                  [Input("diag-channel", "data"), Input("diag-time", "data"), Input("diag-view", "value"),
                   Input("diag-quantity", "value"), Input("diag-tools", "value"), Input("diag-speed", "value"),
                   Input("diag-speed-unit", "value"), Input("diag-bearing-result", "children")])
    def diag_view_capture(channel, selected_ms, view, quantity, tools, speed, unit, _saved):
        if not channel:
            return empty_figure("Choose a machine and channel.", 360), None, None, None, None, None, "auto"
        gid, ch = json.loads(channel)
        ai_box = ai_views.finding_box(data_folder, gid, ch, compact=True) if historian.edge_ai_enabled() else None
        with closing(get_connection()) as conn:
            info, _settings = ai_views.machine_info(conn, gid, ch)
            header = bearing_header(info.get("bearing_orders"))
            try:
                if view == "waterfall":
                    items = historian.recent_spectra(conn, gid, ch, selected_ms or int(time.time() * 1000) + 86_400_000)
                    figure = waterfall_figure(items)
                    figure.update_layout(height=300)
                    note = f"Last {len(items)} saved spectra" + (" up to the moment you clicked." if selected_ms else ".")
                    return figure, html.Span(note, className="text-muted"), None, None, header, ai_box, "auto"
                if view == "waveform":
                    capture = historian.nearest_capture(conn, gid, ch, "waveform", selected_ms)
                    if not capture:
                        raise ValueError("No saved waveform for this channel yet.")
                    values, fs = capture["values"], capture["sampling_freq"]
                    figure = go.Figure(go.Scatter(x=np.arange(values.size) / fs, y=values, mode="lines",
                                                  line=dict(color="#1f77b4", width=1)))
                    figure.update_layout(template="plotly_white", height=300, margin=dict(l=60, r=20, t=16, b=45),
                                         xaxis_title="t [s]", yaxis_title="Waveform (sensor units)")
                    stats = waveform_stats(values)
                    rms = html.Span([html.Span("RMS ", className="text-muted"), html.Strong(f"{stats['rms']:.4g}"),
                                     html.Span(f"  ·  Peak {stats['peak']:.4g} · P-P {stats['peak_to_peak']:.4g} · "
                                               f"CF {stats['crest']:.2f} · Kurtosis {stats['kurtosis']:.2f}",
                                               className="text-muted")])
                    return figure, capture_pill(capture, selected_ms), rms, None, header, ai_box, "auto"
                result = spectrum_view(conn, gid, ch, selected_ms, quantity, tools, speed, unit)
                if result is None:
                    raise ValueError("No saved spectrum for this channel yet. Snapshots are kept every 30 minutes "
                                     "by default, and during recordings.")
                figure, rows, capture, info, rpm, where = result
                metric = "Velocity" if quantity == "velocity" else "Acceleration"
                overall = overall_value(conn, gid, ch, metric, capture["timestamp_ms"])
                rms = html.Span([html.Span(f"{LABELS[metric]} ", className="text-muted"),
                                 html.Strong(f"{overall[0]:.4g}"), html.Span(f" {overall[1]}", className="text-muted")]) \
                    if overall else None
                placeholder = f"auto: {rpm:.0f} RPM" if rpm and where != "typed" else ("no speed" if not rpm else "auto")
                return (figure, capture_pill(capture, selected_ms), rms, ai_views.peak_table(rows), header, ai_box,
                        placeholder)
            except ValueError as error:
                return empty_figure(str(error), 360), None, None, None, header, ai_box, "auto"

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
