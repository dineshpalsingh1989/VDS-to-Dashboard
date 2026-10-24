"""Asset, alarm, diagnostics, and report pages and their Dash callbacks."""

import copy
import json
import os
from contextlib import closing
from datetime import datetime, timezone

from dash import Input, Output, State, dcc, html, dash_table, no_update
from flask import request
import dash_bootstrap_components as dbc

from . import ai_views, devices_view, historian, history_view
from .vds import spectrum_xy

READ_CALLBACKS = {
    "channel_options",
    "load_asset_form",
    "load_rule_form",
    "alarm_event_rows",
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
            html.H3("VDS130"),
            html.P(
                "Allow new VDS units, see each unit's status, and map its channels to measurement locations.",
                className="text-muted",
            ),
            devices_view.section(),
            html.H5("Measurement points"),
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
            *([ai_views.asset_section()] if historian.edge_ai_enabled() else []),
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
    return history_view.page()


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
            *([ai_views.reports_section()] if historian.edge_ai_enabled() else []),
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
    for prefix in ("asset", "rule"):

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
        State("diag-channel", "data"),
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
