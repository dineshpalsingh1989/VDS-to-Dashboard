"""Asset, alarm, diagnostics, and report pages and their Dash callbacks."""

import copy
import json
import os
from contextlib import closing
from datetime import datetime, timezone

import numpy as np
from dash import Input, Output, State, dcc, html, dash_table, no_update
from flask import request
import dash_bootstrap_components as dbc

import historian
from diagnostics import spectrum_with_rpm, waterfall_figure, waveform_figure, capture_details, spectrum_peaks, recorded_capture, display_rpm
from vds import spectrum_xy

READ_CALLBACKS = {
    "channel_options",
    "load_asset_form",
    "load_rule_form",
    "alarm_event_rows",
    "render_diagnostics",
    "diagnostic_capture_options",
    "inspect_spectrum_point",
    "download_report",
    "load_report",
    "refresh_operations",
}


def channel_selector(prefix):
    return dcc.Dropdown(
        id=prefix + "-channel",
        placeholder="Choose device and channel",
        className="mb-3",
    )


def assets_page():
    return html.Div(
        [
            html.H3("Asset registry"),
            html.P(
                "Map VDS channels to physical measurement locations and configure expected publishing intervals.",
                className="text-muted",
            ),
            channel_selector("asset"),
            *[
                html.Div(
                    [dbc.Label(label), dbc.Input(id="asset-" + key, maxLength=128)],
                    className="mb-3",
                )
                for key, label in (
                    ("site", "Site"),
                    ("machine", "Machine"),
                    ("location", "Bearing / measurement location"),
                    ("direction", "Measurement direction"),
                )
            ],
            dbc.Label("Expected publishing interval (seconds)"),
            dbc.Input(
                id="asset-interval", type="number", min=60, max=86400, step=1, value=60
            ),
            html.Small(
                "Freshness warnings begin after three missed intervals. This setting describes the device; it does not reconfigure VDS firmware."
            ),
            dbc.Button(
                "Save asset", id="asset-save", color="primary", className="my-3 d-block"
            ),
            html.Div(id="asset-result"),
        ]
    )


def alarms_page():
    return html.Div(
        [
            html.H3("Alarm management"),
            dbc.Alert(
                "Set thresholds for the specific machine and measurement settings. Initial values are examples.",
                color="info",
            ),
            html.P(
                "Rules apply to a specific metric, unit, filter, and detection method. No automatic machine commands are issued.",
                className="text-muted",
            ),
            channel_selector("rule"),
            dcc.Dropdown(
                id="rule-metric",
                options=[
                    "Acceleration",
                    "Velocity",
                    "Displacement",
                    "Crest Factor",
                    "True Peak",
                    "Std Deviation",
                ],
                value="Velocity",
                clearable=False,
            ),
            html.Div(id="rule-context", className="my-3"),
            dbc.Switch(id="rule-enabled", label="Enable rule", value=False),
            dbc.Row(
                [
                    dbc.Col(
                        [
                            dbc.Label(label),
                            dbc.Input(
                                id="rule-" + key, type="number", min=0, value=value
                            ),
                        ]
                    )
                    for key, label, value in (
                        ("warn", "Warning threshold", 3),
                        ("alert", "Alert threshold", 6),
                        ("hysteresis", "Recovery hysteresis", 0.3),
                        ("persistence", "Consecutive captures", 2),
                    )
                ]
            ),
            dbc.Button("Save rule", id="rule-save", color="primary", className="my-3"),
            html.Div(id="rule-result"),
            html.H4("Event history", className="mt-4"),
            dash_table.DataTable(
                id="alarm-events",
                columns=[
                    {"name": label, "id": key}
                    for key, label in (
                        ("id", "ID"),
                        ("time", "Capture (UTC)"),
                        ("asset", "Asset"),
                        ("severity", "State"),
                        ("value", "Value"),
                        ("context", "Measurement settings / reason"),
                        ("ack", "Acknowledgment"),
                    )
                ],
                data=[],
                row_selectable="single",
                selected_rows=[],
                page_size=15,
                sort_action="native",
                style_table={"overflowX": "auto"},
                style_cell={"textAlign": "left", "whiteSpace": "normal"},
            ),
            dbc.Button(
                "Acknowledge selected event",
                id="alarm-ack",
                color="secondary",
                className="my-3",
            ),
            html.Div(id="alarm-ack-result"),
        ]
    )


def diagnostics_page():
    return html.Div(
        [
            html.H3("Vibration diagnostics"),
            html.P(
                "Inspect waveforms and published spectra, compare baselines, and review recorded captures. These plots do not classify mechanical faults.",
                className="text-muted",
            ),
            channel_selector("diag"),
            dbc.Label("Capture", html_for="diag-capture"),
            dcc.Dropdown(id="diag-capture", options=[{"label": "Live / latest", "value": "live"}],
                         value="live", clearable=False, className="mb-3"),
            html.Small("Latest 100 recorded timestamps. Selected captures compare against the saved channel baseline. Waveform and spectrum are paired only when their timestamps match.", className="text-muted d-block mb-3"),
            dbc.Checklist(
                id="diag-orders",
                options=[{"label": "Order axis (uses RPM)", "value": "orders"}],
                value=[],
            ),
            dbc.Label("Manual RPM (optional)", html_for="diag-rpm"),
            dbc.Input(id="diag-rpm", type="number", min=0, step="any",
                      placeholder="Enter RPM if unavailable, or override for this plot"),
            html.Small("Live view uses dashboard RPM automatically. If RPM is missing or incorrect, enter a positive Manual RPM. Recorded captures use capture RPM. Manual RPM changes only the spectrum and baseline display; the waterfall stays in Hz.", className="text-muted d-block mb-3"),
            dbc.Button(
                "Capture current baseline",
                id="baseline-save",
                color="primary",
                className="my-3",
            ),
            html.Div(id="baseline-result"),
            html.Div(id="diag-result"),
            html.Div(id="diag-details", className="acquisition-grid"),
            dcc.Graph(id="diag-waveform"),
            dcc.Graph(id="diag-spectrum"),
            html.Div(id="diag-cursor", className="alert alert-light border", children="Click a spectrum point to inspect frequency, amplitude, and order."),
            html.H5("Strongest local peaks"),
            html.Small("Discrete spectrum bins, excluding DC and endpoints; amplitudes remain in device units.", className="text-muted"),
            dash_table.DataTable(id="diag-peaks", columns=[
                {"name": "Frequency (Hz)", "id": "frequency", "type": "numeric", "format": {"specifier": ".3f"}},
                {"name": "Amplitude (device units)", "id": "amplitude", "type": "numeric", "format": {"specifier": ".6g"}},
                {"name": "Order", "id": "order", "type": "numeric", "format": {"specifier": ".3f"}}],
                data=[], style_table={"overflowX": "auto"}, style_cell={"textAlign": "left", "padding": "12px"}),
            dcc.Graph(id="diag-waterfall"),
            html.Small(
                "Baseline comparisons require matching acquisition settings. The MQTT schema may omit the FFT window; verify unchanged device settings when the window is unknown."
            ),
        ]
    )


def reports_page():
    return html.Div(
        [
            html.H3("Reports and backups"),
            html.P(
                "Daily reports are advisory summaries grouped by measurement settings. Scheduled backups run once per UTC day.",
                className="text-muted",
            ),
            dcc.DatePickerSingle(
                id="report-day", date=datetime.now(timezone.utc).date()
            ),
            dbc.Button(
                "Download report",
                id="report-download",
                color="primary",
                className="ms-3",
            ),
            dcc.Download(id="report-file"),
            html.Pre(
                id="report-preview",
                className="p-3 bg-white border rounded my-3",
                style={"whiteSpace": "pre-wrap"},
            ),
            dbc.Button(
                "Create and download database backup",
                id="backup-download",
                color="secondary",
            ),
            dcc.Download(id="backup-file"),
            html.Div(id="backup-result"),
            html.Div(id="retention-summary", className="my-3 text-muted"),
        ]
    )


def split_channel(value):
    if not value:
        raise ValueError("Choose a channel first")
    gid, channel = json.loads(value)
    if not isinstance(gid, str) or channel not in ("1", "2", "3", "4"):
        raise ValueError("Invalid channel")
    return gid, channel


def register(app, get_connection, get_snapshot, apply_asset):
    for prefix in ("asset", "rule", "diag"):

        def channel_options(n, _prefix=prefix):
            with closing(get_connection()) as conn:
                rows = conn.execute(
                    "SELECT gateway_id,channel FROM channel_state ORDER BY gateway_id,channel"
                ).fetchall()
                options = []
                for gid, ch in rows:
                    asset = historian.asset_settings(conn, gid, ch)
                    label = " / ".join(
                        asset[key]
                        for key in ("site", "machine", "location", "direction")
                        if asset[key]
                    )
                    options.append(
                        {
                            "label": f"{label or gid} — {gid} CH{ch}",
                            "value": json.dumps([gid, ch]),
                        }
                    )
                return options

        app.callback(
            Output(prefix + "-channel", "options"),
            Input("system_info_interval", "n_intervals"),
        )(channel_options)

    @app.callback(
        [
            Output("asset-" + key, "value")
            for key in ("site", "machine", "location", "direction", "interval")
        ],
        Input("asset-channel", "value"),
    )
    def load_asset_form(value):
        if not value:
            return "", "", "", "", 60
        with closing(get_connection()) as conn:
            asset = historian.asset_settings(conn, *split_channel(value))
        return *(
            asset.get(key, "") for key in ("site", "machine", "location", "direction")
        ), asset["publish_interval"]

    @app.callback(
        Output("asset-result", "children"),
        Input("asset-save", "n_clicks"),
        [State("asset-channel", "value")]
        + [
            State("asset-" + key, "value")
            for key in ("site", "machine", "location", "direction", "interval")
        ],
        prevent_initial_call=True,
    )
    def save_asset_form(n, value, site, machine, location, direction, interval):
        try:
            gid, ch = split_channel(value)
            settings = dict(
                site=site,
                machine=machine,
                location=location,
                direction=direction,
                publish_interval=interval,
            )
            with closing(get_connection()) as conn, conn:
                historian.save_asset(conn, gid, ch, settings)
            apply_asset(gid, ch, settings)
            return dbc.Alert("Asset saved.", color="success")
        except (ValueError, TypeError) as exc:
            return dbc.Alert(str(exc), color="danger")

    @app.callback(
        [
            Output("rule-" + key, "value")
            for key in ("enabled", "warn", "alert", "hysteresis", "persistence")
        ]
        + [Output("rule-context", "children")],
        [Input("rule-channel", "value"), Input("rule-metric", "value")],
    )
    def load_rule_form(value, metric):
        if not value:
            return (
                False,
                3,
                6,
                0.3,
                2,
                "Choose a channel to inspect its measurement settings.",
            )
        with closing(get_connection()) as conn:
            gid, ch = split_channel(value)
            rule = historian.get_json(conn, "alarm_rules", "rule", gid, ch)
            state = historian.get_json(conn, "channel_state", "state", gid, ch)
        if rule.get("metric") != metric:
            rule = {}
        metadata = state.get("metadata", {}).get(metric, {})
        return (
            rule.get("enabled", False),
            rule.get("warn", 3),
            rule.get("alert", 6),
            rule.get("hysteresis", 0.3),
            rule.get("persistence", 2),
            "Current settings: "
            + (
                " | ".join(
                    metadata.get(key) or "unknown"
                    for key in ("unit", "filter", "detection")
                )
                if metadata
                else "not received"
            )
            + ". Thresholds use the displayed unit.",
        )

    @app.callback(
        Output("rule-result", "children"),
        Input("rule-save", "n_clicks"),
        [State("rule-channel", "value"), State("rule-metric", "value")]
        + [
            State("rule-" + key, "value")
            for key in ("enabled", "warn", "alert", "hysteresis", "persistence")
        ],
        prevent_initial_call=True,
    )
    def save_alarm_rule(
        n, value, metric, enabled, warn, alert, hysteresis, persistence
    ):
        try:
            gid, ch = split_channel(value)
            with closing(get_connection()) as conn, conn:
                state = historian.get_json(conn, "channel_state", "state", gid, ch)
                rule = dict(
                    enabled=enabled,
                    metric=metric,
                    warn=warn,
                    alert=alert,
                    hysteresis=hysteresis,
                    persistence=persistence,
                    metadata=state.get("metadata", {}).get(metric, {}),
                )
                historian.save_rule(conn, gid, ch, rule)
            return dbc.Alert(
                "Rule saved. Persistence begins with the next new capture.",
                color="success",
            )
        except (ValueError, TypeError, KeyError) as exc:
            return dbc.Alert(str(exc), color="danger")

    @app.callback(
        Output("alarm-events", "data"), Input("system_info_interval", "n_intervals")
    )
    def alarm_event_rows(n):
        with closing(get_connection()) as conn:
            rows = conn.execute(
                "SELECT id,gateway_id,channel,timestamp_ms,severity,value,context,acknowledged_at,acknowledged_by FROM alarm_events ORDER BY id DESC LIMIT 500"
            ).fetchall()
            result = []
            for eid, gid, ch, ts, severity, value, context, ack, user in rows:
                asset = historian.asset_settings(conn, gid, ch)
                label = (
                    " / ".join(
                        asset[key]
                        for key in ("machine", "location", "direction")
                        if asset[key]
                    )
                    or f"{gid} CH{ch}"
                )
                result.append(
                    dict(
                        id=eid,
                        time=datetime.fromtimestamp(
                            ts / 1000, timezone.utc
                        ).isoformat(),
                        asset=label,
                        severity=severity,
                        value=value,
                        context=" | ".join(
                            str(part)
                            for part in (
                                json.loads(context).get("metric"),
                                *(
                                    json.loads(context).get("metadata", {}).get(key)
                                    for key in ("unit", "filter", "detection")
                                ),
                                json.loads(context).get("reason"),
                            )
                            if part
                        ),
                        ack=f"{user} — {datetime.fromtimestamp(ack, timezone.utc).isoformat()}"
                        if ack
                        else "Pending",
                    )
                )
            return result

    @app.callback(
        Output("alarm-ack-result", "children"),
        Input("alarm-ack", "n_clicks"),
        [State("alarm-events", "selected_row_ids")],
        prevent_initial_call=True,
    )
    def acknowledge_alarm(n, ids):
        if not ids:
            return dbc.Alert("Select an event first.", color="warning")
        user = (
            request.authorization.username if request.authorization else "local-admin"
        )
        with closing(get_connection()) as conn, conn:
            conn.execute(
                "UPDATE alarm_events SET acknowledged_at=?,acknowledged_by=? WHERE id=? AND acknowledged_at IS NULL",
                (datetime.now(timezone.utc).timestamp(), user, int(ids[0])),
            )
        return dbc.Alert(
            "Event acknowledged. The measurement state is unchanged.", color="success"
        )

    def current_spectrum(gid, ch):
        data = get_snapshot().get(gid, {}).get("channels", {}).get(ch, {})
        context = copy.deepcopy(data.get("spectrum_context", {}))
        context.update(
            spectrum_data=data.get("spectrum_data", []),
            metrics=data.get("metrics", {}),
            metadata=data.get("metadata", {}),
        )
        return context

    @app.callback(
        Output("baseline-result", "children"),
        Input("baseline-save", "n_clicks"),
        State("diag-channel", "value"),
        prevent_initial_call=True,
    )
    def capture_baseline(n, value):
        try:
            gid, ch = split_channel(value)
            current = current_spectrum(gid, ch)
            spectrum_xy(current)
            with closing(get_connection()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO baselines VALUES (?,?,?)",
                    (gid, ch, json.dumps(current)),
                )
            return dbc.Alert(
                "Baseline captured with measurement settings and capture time.",
                color="success",
            )
        except (ValueError, TypeError) as exc:
            return dbc.Alert(str(exc), color="warning")

    @app.callback(Output("diag-capture", "options"),
                  Input("diag-channel", "value"), Input("live-update-trigger", "data"))
    def diagnostic_capture_options(value, trigger):
        options = [{"label": "Live / latest", "value": "live"}]
        try:
            gid, ch = split_channel(value)
            with closing(get_connection()) as conn:
                rows = conn.execute("SELECT timestamp FROM raw_spectrum WHERE gateway_id=? AND channel=? UNION SELECT timestamp FROM raw_waveforms WHERE gateway_id=? AND channel=? ORDER BY timestamp DESC LIMIT 100",
                                    (gid, ch, gid, ch)).fetchall()
            options.extend({"label": str(row[0]) + " (stored time)", "value": str(row[0])} for row in rows)
        except (ValueError, TypeError):
            pass
        return options

    @app.callback(Output("diag-cursor", "children"), Input("diag-spectrum", "clickData"),
                  Input("diag-spectrum", "figure"))
    def inspect_spectrum_point(clicked, figure):
        from dash import ctx
        if ctx.triggered_id != "diag-spectrum" or any(item["prop_id"].endswith(".figure") for item in ctx.triggered):
            return "Click a spectrum point to inspect frequency, amplitude, and order."
        try:
            point = clicked["points"][0]
            hz, order = point["customdata"]
            order_text = f"{order:.3f}" if order is not None and np.isfinite(order) else "Unavailable"
            return f"Frequency: {hz:.3f} Hz | Amplitude: {point['y']:.6g} device units | Order: {order_text}"
        except (KeyError, TypeError, IndexError, ValueError):
            return "Click a spectrum trace point to inspect its measurement."

    @app.callback(
        [
            Output("diag-waveform", "figure"),
            Output("diag-spectrum", "figure"),
            Output("diag-waterfall", "figure"),
            Output("diag-result", "children"),
            Output("diag-details", "children"),
            Output("diag-peaks", "data"),
        ],
        [
            Input("diag-channel", "value"),
            Input("diag-orders", "value"),
            Input("diag-rpm", "value"),
            Input("diag-capture", "value"),
            Input("live-update-trigger", "data"),
        ],
    )
    def render_diagnostics(value, orders, manual_rpm, capture, trigger):
        import plotly.graph_objects as go

        try:
            gid, ch = split_channel(value)
            current = current_spectrum(gid, ch)
            notices = []
            device = get_snapshot().get(gid, {})
            data = device.get("channels", {}).get(ch, {})
            wave = dict(data.get("waveform_context", {}), waveform_data=data.get("waveform_data", []))
            with closing(get_connection()) as conn:
                if capture and capture != "live":
                    recorded = recorded_capture(conn, gid, ch, capture)
                    current = recorded.get("spectrum", {})
                    wave = recorded.get("waveform", {})
                    notices.append("Recorded capture selected; baseline button still captures live data.")
                baseline = historian.get_json(conn, "baselines", "snapshot", gid, ch)
                rows = conn.execute(
                    "SELECT timestamp,spectrum_data,context FROM raw_spectrum WHERE gateway_id=? AND channel=? AND context IS NOT NULL ORDER BY timestamp DESC LIMIT 50",
                    (gid, ch),
                ).fetchall()
            live = not capture or capture == "live"
            current = display_rpm(current, device.get("rpm"), live)
            if current.get("rpm") is None and manual_rpm is None:
                notices.append("RPM is missing or invalid. Enter a positive Manual RPM to use the order axis.")
            details = [html.Div([html.H6(kind), html.Dl([
                html.Div([html.Dt(key), html.Dd(str(val))])
                for key, val in capture_details(item, kind, speed).items() if key != "Plot"
            ])], className="acquisition-card") for kind, item, speed in
                (("Waveform", wave, None), ("Spectrum", current, manual_rpm))]
            try:
                waveform = waveform_figure(wave)
            except (ValueError, TypeError) as exc:
                waveform = go.Figure().update_layout(title="Time waveform unavailable")
                notices.append(str(exc))
            try:
                peaks = spectrum_peaks(current, manual_rpm)
            except (ValueError, TypeError):
                peaks = []
            snapshots = []
            for timestamp, blob, context in reversed(rows):
                item = json.loads(context)
                item.update(
                    spectrum_data=np.frombuffer(blob, dtype="<f8").tolist(),
                    sampling_freq=item.get("sampling_freq"),
                    label=str(timestamp),
                )
                snapshots.append(item)
            try:
                spectrum = spectrum_with_rpm(current, baseline or None,
                                             "orders" in (orders or []), manual_rpm)
            except (ValueError, TypeError) as exc:
                notices.append(str(exc))
                try:
                    spectrum = spectrum_with_rpm(current, orders="orders" in (orders or []),
                                                 manual_rpm=manual_rpm)
                except (ValueError, TypeError):
                    spectrum = go.Figure().update_layout(title="Spectrum unavailable")
            try:
                waterfall = waterfall_figure(snapshots)
            except ValueError as exc:
                waterfall = go.Figure()
                notices.append(str(exc))
            return (
                waveform,
                spectrum,
                waterfall,
                dbc.Alert(" ".join(notices), color="info") if notices else "",
                details,
                peaks,
            )
        except (ValueError, TypeError) as exc:
            return go.Figure(), go.Figure(), go.Figure(), dbc.Alert(str(exc), color="info"), [], []

    @app.callback(Output("report-preview", "children"), Input("report-day", "date"))
    def load_report(day):
        if not day:
            return "Choose a UTC day."
        with closing(get_connection()) as conn:
            return historian.daily_report(conn, str(day)[:10])

    @app.callback(
        Output("report-file", "data"),
        Input("report-download", "n_clicks"),
        State("report-day", "date"),
        prevent_initial_call=True,
    )
    def download_report(n, day):
        if not day:
            return no_update
        with closing(get_connection()) as conn:
            content = historian.daily_report(conn, str(day)[:10])
        return dcc.send_string(content, filename=f"vds-report-{str(day)[:10]}.md")

    @app.callback(
        [Output("backup-file", "data"), Output("backup-result", "children")],
        Input("backup-download", "n_clicks"),
        prevent_initial_call=True,
    )
    def download_backup(n):
        try:
            with closing(get_connection()) as conn:
                path = conn.execute("PRAGMA database_list").fetchone()[2]
                destination = os.path.join(
                    os.path.dirname(path), "backups", "manual-backup.db"
                )
                historian.backup_database(conn, destination)
            return dcc.send_file(destination), dbc.Alert(
                "Backup created and integrity checked.", color="success"
            )
        except (OSError, RuntimeError) as exc:
            return no_update, dbc.Alert(str(exc), color="danger")

    @app.callback(
        Output("retention-summary", "children"),
        Input("system_info_interval", "n_intervals"),
    )
    def refresh_operations(n):
        return "Retention in days (0 keeps all): " + ", ".join(
            f"{label}: {os.environ.get(key, '0')}"
            for key, label in (
                ("VDS_TREND_RETENTION_DAYS", "scalar trends"),
                ("VDS_RAW_RETENTION_DAYS", "raw captures"),
                ("VDS_ALARM_RETENTION_DAYS", "acknowledged alarms"),
            )
        )
