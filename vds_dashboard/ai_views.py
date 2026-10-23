"""AI analysis page: per-VDS AI switch, sensor setup, learning progress and AI reports.

The page only stores settings and reads what the AI advisor publishes in
<data>/ai/. The advisor itself runs as a separate low-priority service
(iriv-edge/ai_advisor.py) and never issues machine commands.
"""

import hmac
import json
import os
from contextlib import closing
from datetime import datetime

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from flask import request

from . import historian

READ_CALLBACKS = {"ai_overview", "ai_gateway_list", "ai_channel_options", "ai_load_channel", "ai_show_report"}
FORM_KEYS = ("machine", "location", "machine_type", "direction", "bearing", "speed_source", "fixed_rpm",
             "line_frequency_hz", "gear_teeth", "blades", "iso_class")
LABELS = {
    "motor": "Motor", "pump": "Pump", "fan": "Fan", "gearbox": "Gearbox", "compressor": "Compressor",
    "other": "Other", "radial": "Radial", "horizontal": "Horizontal (radial)", "vertical": "Vertical (radial)",
    "axial": "Axial", "group1_rigid": "Large, 300 kW–50 MW, rigid foundation",
    "group1_flexible": "Large, 300 kW–50 MW, flexible foundation",
    "group2_rigid": "Medium, 15–300 kW, rigid foundation", "group2_flexible": "Medium, 15–300 kW, flexible foundation",
}


def options(values, blank=None):
    items = [{"label": blank, "value": ""}] if blank else []
    return items + [{"label": LABELS.get(v, v), "value": v} for v in values]


def is_admin():
    password = os.environ.get("VDS_ADMIN_PASSWORD", "")
    if not password:
        return True  # local-only mode: the dashboard already refuses remote clients
    auth = request.authorization
    return bool(auth and hmac.compare_digest(auth.username or "", os.environ.get("VDS_ADMIN_USER", "admin"))
                and hmac.compare_digest(auth.password or "", password))


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def field(label, control, width=6, hint=None):
    children = [dbc.Label(label), control]
    if hint:
        children.append(html.Small(hint, className="text-muted"))
    return dbc.Col(html.Div(children, className="mb-3"), md=width)


def ai_page():
    return html.Div([
        html.H3("AI analysis"),
        html.P("The AI learns each machine's normal vibration, then quietly scans spectra, waveforms and readings "
               "in the background for changes and likely faults. It only advises; it never controls machines.",
               className="text-muted"),
        dbc.Alert(id="ai-storage", color="info"),
        html.H5("1. Connect VDS units to the AI", className="mt-4"),
        html.P("Turn the AI on for each VDS you want analysed. Learning samples are kept only for these units.",
               className="text-muted small"),
        html.Div(id="ai-gateways"),
        html.Div(id="ai-gateway-result", className="mt-2"),
        html.H5("2. Tell the AI about each sensor", className="mt-4"),
        html.P("The more it knows, the more precisely it can name a fault. The bearing number lets it calculate "
               "bearing defect frequencies.", className="text-muted small"),
        dcc.Dropdown(id="ai-channel", placeholder="Choose VDS and channel", className="mb-2"),
        html.Div(id="ai-speed-hint", className="mb-3"),
        dbc.Row([
            field("Machine name", dbc.Input(id="ai-machine", maxLength=64, placeholder="Pump P-101")),
            field("Sensor location", dbc.Input(id="ai-location", maxLength=64, placeholder="Motor drive end")),
            field("Machine type", dcc.Dropdown(id="ai-machine_type", options=options(historian.AI_MACHINE_TYPES))),
            field("Sensor direction", dcc.Dropdown(id="ai-direction", options=options(historian.AI_DIRECTIONS))),
            field("Bearing number", dcc.Input(id="ai-bearing", list="ai-bearing-list", maxLength=64,
                                              placeholder="6205-2RS", className="form-control"),
                  hint="Pick from the bearing library or type it; unknown bearings are flagged below."),
            field("Machine speed", dbc.RadioItems(id="ai-speed_source", options=[
                {"label": "From the VDS speed sensor", "value": "vds"},
                {"label": "Fixed speed (no speed sensor)", "value": "fixed"}], value="vds")),
            field("Fixed speed (RPM)", dbc.Input(id="ai-fixed_rpm", type="number", min=1, max=60000),
                  hint="Required for a fixed speed. Also used whenever the VDS sends no speed.", width=4),
            field("Mains frequency", dbc.RadioItems(id="ai-line_frequency_hz", inline=True, value=50, options=[
                {"label": "50 Hz", "value": 50}, {"label": "60 Hz", "value": 60}]), width=4),
            field("ISO 20816 machine class", dcc.Dropdown(id="ai-iso_class", options=options(
                historian.AI_ISO_CLASSES, blank="Not set")), width=4),
            field("Gear teeth (gearboxes)", dbc.Input(id="ai-gear_teeth", type="number", min=1, max=1000), width=3),
            field("Blades or vanes (pumps, fans)", dbc.Input(id="ai-blades", type="number", min=1, max=100), width=3),
        ]),
        html.Datalist(id="ai-bearing-list"),
        dbc.Button("Save sensor setup", id="ai-save", color="primary", className="me-2"),
        dbc.Button("Relearn this sensor", id="ai-relearn", color="secondary", outline=True),
        html.Small(" Use Relearn after a repair, when the machine's new normal differs from before.",
                   className="text-muted"),
        html.Div(id="ai-result", className="mt-2"),
        html.Div(id="ai-relearn-result", className="mt-2"),
        html.H5("3. Learning progress", className="mt-4"),
        html.Div(id="ai-progress"),
        html.H5("4. AI reports", className="mt-4"),
        html.P("Each day the AI works it writes a report folder: an activity log, plus an evidence folder for each "
               "new finding with a report, the spectrum against normal, and waveform values.",
               className="text-muted small"),
        dcc.Dropdown(id="ai-report-select", placeholder="Choose a report", className="mb-2"),
        html.Div(id="ai-report-view", className="border rounded p-3"),
    ])


def register(app, get_connection, data_folder):
    ai_folder = os.path.join(data_folder, "ai")

    def channels():
        with closing(get_connection()) as conn:
            rows = conn.execute("SELECT gateway_id, channel FROM channel_state ORDER BY gateway_id, channel").fetchall()
            return [(gid, ch, historian.ai_channel_settings(conn, gid, ch)) for gid, ch in rows]

    @app.callback([Output("ai-storage", "children"), Output("ai-storage", "color"), Output("ai-progress", "children"),
                   Output("ai-report-select", "options"), Output("ai-bearing-list", "children")],
                  Input("system_info_interval", "n_intervals"), Input("url", "pathname"))
    def ai_overview(_n, _path):
        report = read_json(os.path.join(ai_folder, "findings.json"), None)
        index = read_json(os.path.join(ai_folder, "index.json"), [])
        if report is None:
            return ("The AI advisor is not running on this system. On an IRIV Edge AI, install it with "
                    "bash iriv-edge/install.sh.", "secondary", None, [], [])
        storage = report.get("storage") or {}
        color = {"ssd_missing": "warning", "failed": "warning", "ssd": "success"}.get(storage.get("state"), "info")
        status = [html.Strong("AI status: "), str(report.get("summary", ""))]
        if storage.get("message"):
            status += [html.Br(), html.Strong("Storage: "), str(storage["message"])]
        rows = []
        for item in report.get("progress", []):
            if item.get("phase") == "learning":
                phase = (f"Learning: {item.get('samples') or 0}/{item.get('needed_samples')} samples, "
                         f"{item.get('hours') or 0}/{item.get('needed_hours')} h of running")
            else:
                phase = str(item.get("phase", "")).capitalize()
            rows.append(html.Tr([html.Td(str(item.get("name", ""))),
                                 html.Td(f"{item.get('gateway_id')} CH{item.get('channel')}"), html.Td(phase),
                                 html.Td(str(item.get("changes", 0))), html.Td("; ".join(item.get("notes", [])))]))
        table = dbc.Table([html.Thead(html.Tr([html.Th(h) for h in ("Machine", "Sensor", "Status", "Changes", "Notes")])),
                           html.Tbody(rows)], size="sm", striped=True) if rows else html.P(
            "No AI-enabled sensors yet.", className="text-muted")
        report_options = [{"label": f"{datetime.fromtimestamp(e.get('created') or 0):%Y-%m-%d %H:%M} — {e.get('title')}",
                           "value": i} for i, e in enumerate(index)]
        bearings = [html.Option(value=str(b)) for b in report.get("bearings", [])]
        return status, color, table, report_options, bearings

    @app.callback(Output("ai-gateways", "children"), Input("url", "pathname"), Input("ai-gateway-result", "children"))
    def ai_gateway_list(_path, _saved):
        with closing(get_connection()) as conn:
            gateways = [row[0] for row in conn.execute("SELECT DISTINCT gateway_id FROM channel_state ORDER BY 1")]
            states = {gid: historian.ai_enabled(conn, gid) for gid in gateways}
        if not gateways:
            return html.P("No VDS has sent data yet.", className="text-muted")
        admin = is_admin()
        return [dbc.Switch(id={"type": "ai-gw-switch", "gid": gid}, label=f"{gid}: AI analysis",
                           value=states[gid], disabled=not admin) for gid in gateways]

    @app.callback(Output("ai-gateway-result", "children"), Input({"type": "ai-gw-switch", "gid": ALL}, "value"),
                  prevent_initial_call=True)
    def ai_toggle_gateway(_values):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not ctx.triggered:
            return no_update
        enabled = bool(ctx.triggered[0]["value"])
        with closing(get_connection()) as conn, conn:
            if historian.ai_enabled(conn, trigger["gid"]) == enabled:
                return no_update
            historian.save_ai_gateway(conn, trigger["gid"], enabled)
        text = "on. It starts learning from the next samples" if enabled else "off"
        return dbc.Alert(f"AI analysis for {trigger['gid']} is {text}.", color="success", duration=6000)

    @app.callback(Output("ai-channel", "options"), Input("url", "pathname"))
    def ai_channel_options(_path):
        return [{"label": " / ".join(filter(None, (s.get("machine"), s.get("location")))) + f" — {gid} CH{ch}"
                 if s.get("machine") or s.get("location") else f"{gid} CH{ch}", "value": json.dumps([gid, ch])}
                for gid, ch, s in channels()]

    @app.callback([Output("ai-" + key, "value") for key in FORM_KEYS] + [Output("ai-speed-hint", "children")],
                  Input("ai-channel", "value"))
    def ai_load_channel(value):
        if not value:
            return [""] * 4 + [None, "vds", None, 50, None, None, ""] + [None]
        gid, ch = json.loads(value)
        with closing(get_connection()) as conn:
            settings = historian.ai_channel_settings(conn, gid, ch)
            state = historian.get_json(conn, "channel_state", "state", gid, ch, {})
        rpm = state.get("rpm")
        hint = (dbc.Alert(f"This VDS reports {rpm:.0f} RPM from its speed sensor.", color="light")
                if isinstance(rpm, (int, float)) and rpm > 0 else
                dbc.Alert("This VDS has not reported a machine speed. Choose 'Fixed speed' and enter the RPM, "
                          "so the AI can find running-speed and bearing frequencies.", color="warning"))
        defaults = {"speed_source": "vds", "line_frequency_hz": 50}
        return [settings.get(key, defaults.get(key, "" if key in ("machine", "location", "bearing") else None))
                for key in FORM_KEYS] + [hint]

    @app.callback(Output("ai-result", "children"), Input("ai-save", "n_clicks"),
                  [State("ai-channel", "value")] + [State("ai-" + key, "value") for key in FORM_KEYS],
                  prevent_initial_call=True)
    def ai_save_channel(_n, value, *values):
        try:
            if not value:
                raise ValueError("Choose a VDS and channel first")
            gid, ch = json.loads(value)
            with closing(get_connection()) as conn, conn:
                historian.save_ai_channel(conn, gid, ch, dict(zip(FORM_KEYS, values)))
                enabled = historian.ai_enabled(conn, gid)
            note = "" if enabled else f" Turn on AI analysis for {gid} above to start learning."
            return dbc.Alert("Sensor setup saved. The AI uses it from its next scan." + note, color="success")
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger")

    @app.callback(Output("ai-relearn-result", "children"), Input("ai-relearn", "n_clicks"),
                  State("ai-channel", "value"), prevent_initial_call=True)
    def ai_relearn(_n, value):
        if not value:
            return dbc.Alert("Choose a VDS and channel first", color="danger")
        gid, ch = json.loads(value)
        with closing(get_connection()) as conn, conn:
            historian.request_ai_relearn(conn, gid, ch)
        return dbc.Alert(f"{gid} CH{ch} will be relearned from its next samples.", color="success")

    @app.callback(Output("ai-report-view", "children"), Input("ai-report-select", "value"))
    def ai_show_report(choice):
        index = read_json(os.path.join(ai_folder, "index.json"), [])
        if choice is None or not isinstance(choice, int) or not 0 <= choice < len(index):
            return html.P("Choose a report to read it here.", className="text-muted mb-0")
        entry = index[choice]
        return [dcc.Markdown(str(entry.get("markdown", ""))),
                html.Small(f"Folder on the device: {entry.get('folder', '')}", className="text-muted")]
