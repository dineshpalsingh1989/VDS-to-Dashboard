"""IRIV Edge AI in the dashboard (only when VDS_EDGE_AI=1).

* Asset registry: AI switch, machine details, bearing (with its frequencies in Hz), speed source.
* Spectrum pop-up: fault frequency lines, peak matches and the AI's finding with feedback buttons.
* Health page: learning progress. Reports page: the AI's report folders.

The advisor itself runs as a separate low-priority service (iriv-edge/ai_advisor.py)
and never issues machine commands; this module only stores settings and shows
what it publishes in <data>/ai/.
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
from .fault_frequencies import FAMILIES, LIBRARY, find_bearing, load_bearings

READ_CALLBACKS = {"ai_load_setup", "ai_bearing_preview", "ai_bearing_mode", "ai_health_progress", "ai_report_list",
                  "ai_show_report"}
NOT_IRIV = "AI analysis is only available on IRIV Edge AI installs (VDS_EDGE_AI=1)."
VERDICTS = (("confirmed", "Confirmed", "danger"), ("wrong", "Wrong (normal operation)", "secondary"),
            ("repaired", "Repaired", "success"))
SETUP_KEYS = ("machine_type", "bearing", "speed_source", "fixed_rpm", "line_frequency_hz", "gear_teeth", "blades",
              "iso_class")
BEARING_FIELDS = ("bpfo", "bpfi", "bsf", "ftf", "balls", "ball_diameter_mm", "pitch_diameter_mm",
                  "contact_angle_deg", "source")
LABELS = {
    "motor": "Motor", "pump": "Pump", "fan": "Fan", "gearbox": "Gearbox", "compressor": "Compressor",
    "other": "Other", "group1_rigid": "Large, 300 kW–50 MW, rigid foundation",
    "group1_flexible": "Large, 300 kW–50 MW, flexible foundation",
    "group2_rigid": "Medium, 15–300 kW, rigid foundation", "group2_flexible": "Medium, 15–300 kW, flexible foundation",
    "class1_small": "Small, up to 15 kW (ISO 10816-1 class I)",
    "radial": "Radial", "horizontal": "Horizontal", "vertical": "Vertical", "axial": "Axial",
}
FAMILY_OPTIONS = [{"label": label, "value": key} for key, label, _c, _t in FAMILIES]
_library_cache = {}


def enabled():
    return historian.edge_ai_enabled()


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


def options(values, blank=None):
    items = [{"label": blank, "value": ""}] if blank else []
    return items + [{"label": LABELS.get(v, v), "value": v} for v in values]


def field(label, control, width=6, hint=None):
    children = [dbc.Label(label), control]
    if hint:
        children.append(html.Small(hint, className="text-muted d-block"))
    return dbc.Col(html.Div(children, className="mb-3"), md=width)


# ---------------------------------------------------------------- machine facts
def library(conn):
    """Bearing library file plus bearings added in the dashboard, reloaded when either changes."""
    added = historian.ai_bearings(conn)
    stamp = (LIBRARY.stat().st_mtime if LIBRARY.exists() else 0, json.dumps(added, sort_keys=True))
    if _library_cache.get("stamp") != stamp:
        _library_cache.update(stamp=stamp, bearings=load_bearings(LIBRARY, added))
    return _library_cache["bearings"]


def designations(bearings):
    return sorted({b["designation"] for b in bearings.values()})


def machine_info(conn, gid, ch, bearing=None):
    """What the spectrum lines need: bearing frequencies, gear teeth, blades and mains frequency."""
    settings = historian.ai_channel_settings(conn, gid, ch)
    bearing = settings.get("bearing", "") if bearing is None else bearing
    info = {"line_frequency_hz": settings.get("line_frequency_hz") or 50, "gear_teeth": settings.get("gear_teeth"),
            "blades": settings.get("blades"), "bearing": bearing}
    found = find_bearing(library(conn), bearing) if bearing else None
    if found:
        info["bearing_orders"] = found
    return info, settings


def resolve_rpm(settings, live_rpm):
    """Speed from the VDS speed sensor, or the fixed speed entered for this sensor."""
    if settings.get("speed_source") == "fixed" and settings.get("fixed_rpm"):
        return float(settings["fixed_rpm"]), "fixed speed"
    if live_rpm and live_rpm > 0:
        return float(live_rpm), "VDS speed sensor"
    if settings.get("fixed_rpm"):
        return float(settings["fixed_rpm"]), "fixed speed"
    return None, "no speed"


def bearing_summary(found, rpm):
    if not found:
        return None
    parts = []
    for key in ("bpfo", "bpfi", "bsf", "ftf"):
        value = found[key] * rpm / 60 if rpm else found[key]
        parts.append(f"{key.upper()} {value:.1f} Hz" if rpm else f"{key.upper()} {value:.3f}x")
    return f"{found['designation']}: " + ", ".join(parts)


# ---------------------------------------------------------------- asset registry section
def asset_section():
    return html.Div([
        html.Hr(className="my-4"),
        html.H4([html.I(className="fas fa-brain me-2"), "AI analysis (IRIV Edge AI)"]),
        html.P("The AI learns this machine's normal vibration and reports changes and likely faults in the spectrum "
               "view. The bearing number lets it calculate bearing defect frequencies.", className="text-muted"),
        dbc.Switch(id="ai-gw-enabled", label="AI analysis on for this VDS", value=False, className="mb-3"),
        html.Div(id="ai-speed-hint"),
        dbc.Row([
            field("Machine type", dcc.Dropdown(id="ai-machine_type", options=options(historian.AI_MACHINE_TYPES)), 4),
            field("Bearing number", dcc.Input(id="ai-bearing", list="ai-bearing-list", maxLength=64, debounce=True,
                                              placeholder="e.g. 6205-2RS", className="form-control"), 4,
                  hint="Type the number; seals and clearance (2RS, ZZ, C3) do not matter."),
            field("ISO 20816 machine class", dcc.Dropdown(id="ai-iso_class", options=options(
                historian.AI_ISO_CLASSES, blank="Not set")), 4),
        ]),
        html.Div(id="ai-bearing-preview", className="mb-3"),
        dbc.Row([
            field("Machine speed", dbc.RadioItems(id="ai-speed_source", value="vds", options=[
                {"label": "From the VDS speed sensor", "value": "vds"},
                {"label": "Fixed speed (no speed sensor)", "value": "fixed"}]), 4),
            field("Fixed speed (RPM)", dbc.Input(id="ai-fixed_rpm", type="number", min=1, max=60000), 2),
            field("Mains", dbc.RadioItems(id="ai-line_frequency_hz", inline=True, value=50, options=[
                {"label": "50 Hz", "value": 50}, {"label": "60 Hz", "value": 60}]), 2),
            field("Gear teeth", dbc.Input(id="ai-gear_teeth", type="number", min=1, max=1000), 2),
            field("Blades / vanes", dbc.Input(id="ai-blades", type="number", min=1, max=100), 2),
        ]),
        html.Datalist(id="ai-bearing-list"),
        dbc.Button("Save AI setup", id="ai-save", color="primary", className="me-2"),
        dbc.Button("Relearn this sensor", id="ai-relearn", color="secondary", outline=True),
        html.Small(" Use Relearn after a repair, when the machine's new normal differs from before.",
                   className="text-muted"),
        html.Div(id="ai-result", className="mt-2"),
        html.Div(id="ai-relearn-result", className="mt-2"),
        html.Details([
            html.Summary("Bearing not in the library? Add it"),
            html.P("Use the manufacturer's published defect frequencies, or the ball count and sizes from its "
                   "catalogue.", className="text-muted small mt-2"),
            dbc.Row([
                field("Bearing number", dbc.Input(id="ai-new-bearing", maxLength=32, placeholder="e.g. 22216-E"), 4),
                field("I have", dbc.RadioItems(id="ai-bearing-mode", value="orders", options=[
                    {"label": "Defect frequencies (x shaft speed)", "value": "orders"},
                    {"label": "Ball count and sizes", "value": "geometry"}]), 8),
            ]),
            dbc.Row([field(label, dbc.Input(id="ai-b-" + key, type="number", step="any"), 3) for key, label in (
                ("bpfo", "BPFO"), ("bpfi", "BPFI"), ("bsf", "BSF"), ("ftf", "FTF"))], id="ai-bearing-orders"),
            dbc.Row([field(label, dbc.Input(id="ai-b-" + key, type="number", step="any"), 3) for key, label in (
                ("balls", "Balls or rollers"), ("ball_diameter_mm", "Ball diameter (mm)"),
                ("pitch_diameter_mm", "Pitch diameter (mm)"), ("contact_angle_deg", "Contact angle (deg)"))],
                id="ai-bearing-geometry", style={"display": "none"}),
            dbc.Row([field("Source", dbc.Input(id="ai-b-source", maxLength=120, placeholder="SKF catalogue"))]),
            dbc.Button("Add bearing", id="ai-bearing-save", color="primary", outline=True),
            html.Div(id="ai-bearing-result", className="mt-2"),
        ], className="mt-4"),
    ])


def health_section():
    return html.Div([html.H5("AI learning progress", className="mt-4"), html.Div(id="ai-health-progress")])


def reports_section():
    return html.Div([
        html.H5("AI reports", className="mt-4"),
        html.P("Each day the AI works it writes a report folder: an activity log, plus an evidence folder for each "
               "new finding.", className="text-muted small"),
        dcc.Dropdown(id="ai-report-select", placeholder="Choose an AI report", className="mb-2"),
        html.Div(id="ai-report-view", className="border rounded p-3 bg-white"),
    ])


# ---------------------------------------------------------------- spectrum pop-up pieces
def spectrum_controls():
    """Controls under the spectrum; hidden unless this is an IRIV Edge AI install."""
    return html.Div([
        dbc.Row([
            dbc.Col([dbc.Label("Bearing", className="small mb-0"),
                     dcc.Input(id="modal-bearing", list="modal-bearing-list", debounce=True, maxLength=64,
                               placeholder="e.g. 6205", className="form-control form-control-sm")], md=2),
            dbc.Col([dbc.Label("Fault lines", className="small mb-0"),
                     dbc.Checklist(id="modal-families", options=FAMILY_OPTIONS, inline=True,
                                   value=["speed", "bearing", "gear", "electrical"])], md=6),
            dbc.Col([dbc.Label("Harmonics", className="small mb-0"),
                     dcc.Dropdown(id="modal-harmonics", clearable=False, value=3,
                                  options=[{"label": str(n), "value": n} for n in (1, 2, 3, 5, 10)])], md=2),
            dbc.Col([dbc.Label("Scale", className="small mb-0"),
                     dbc.RadioItems(id="modal-scale", inline=True, value="linear", options=[
                         {"label": "Linear", "value": "linear"}, {"label": "Log", "value": "log"}])], md=2),
        ], className="g-2 align-items-end"),
        html.Datalist(id="modal-bearing-list"),
        html.Div(id="modal-info", className="small text-muted mt-2"),
        dbc.Button("Save this bearing for the sensor", id="modal-save-bearing", size="sm", color="link",
                   className="p-0"),
        html.Div(id="modal-bearing-result", className="small"),
    ], id="modal-controls", style={"display": "none"})


def finding_box(data_folder, gid, ch, compact=False):
    """The AI's current finding for one sensor, with feedback buttons.

    compact gives one line per finding (full text on hover) for one-screen pages."""
    report = read_json(os.path.join(data_folder, "ai", "findings.json"), {})
    items = [f for f in report.get("findings", []) if f.get("gateway_id") == gid and f.get("channel") == ch]
    if not items:
        progress = next((p for p in report.get("progress", []) if p.get("gateway_id") == gid
                         and p.get("channel") == ch), None)
        if not progress:
            return None
        phase = progress.get("phase", "")
        text = (f"AI is learning this machine ({progress.get('samples') or 0}/{progress.get('needed_samples')} "
                f"samples, {progress.get('hours') or 0}/{progress.get('needed_hours')} h)." if phase == "learning"
                else "AI: no unusual change on this sensor.")
        return dbc.Alert([html.I(className="fas fa-brain me-2"), text], color="light",
                         className="ai-line mb-2" if compact else "py-2 mb-2")
    admin = is_admin()
    cards = []
    for item in items:
        key = json.dumps([gid, ch, item.get("kind"), item.get("since_ms"), item.get("fault_id")])
        buttons = [dbc.Button(label, id={"type": "ai-feedback", "verdict": verdict, "key": key}, size="sm",
                              color=colour, outline=True, className="ms-1 mb-1", disabled=not admin)
                   for verdict, label, colour in VERDICTS] if item.get("kind") not in ("severity", "machine") else []
        if compact:
            title = str(item.get("title", "AI finding"))
            short = title.split("likely ", 1)[1] if "likely " in title else title.split(": ", 1)[-1]
            cards.append(html.Div([
                html.I(className="fas fa-brain me-2"),
                html.Span([html.Strong(short), html.Span(" " + str(item.get("message", "")), className="text-muted")],
                          className="ai-line-text", title=f"{title}\n{item.get('message', '')}"),
                html.Span(buttons, className="ai-line-buttons")],
                className="ai-line " + ("ai-line-warning" if item.get("severity") == "warning" else "ai-line-info")))
            continue
        cards.append(dbc.Alert([
            dbc.Row([
                dbc.Col(html.Strong([html.I(className="fas fa-brain me-2"), str(item.get("title", "AI finding"))]),
                        md=7),
                dbc.Col(buttons, md=5, className="text-md-end")], className="g-1 align-items-center"),
            html.Details([
                html.Summary("Details", className="small"),
                html.Div(str(item.get("message", "")), className="small my-1"),
                html.Small(f"Evidence folder: {item['report']}", className="text-muted d-block text-break")
                if item.get("report") else None])],
            color="warning" if item.get("severity") == "warning" else "info", className="py-2 mb-2"))
    return cards


def channel_badges(data_folder):
    """Small AI status badge for each channel card on the dashboard."""
    report = read_json(os.path.join(data_folder, "ai", "findings.json"), {})
    badges = {}
    for row in report.get("progress", []):
        learning = row.get("phase") == "learning"
        badges[(row.get("gateway_id"), row.get("channel"))] = dbc.Badge(
            [html.I(className="fas fa-brain me-1"), "AI learning" if learning else "AI normal"],
            className="ai-badge " + ("ai-learning" if learning else "ai-ok"), title="AI analysis status")
    for item in report.get("findings", []):
        key = (item.get("gateway_id"), item.get("channel"))
        if not key[0]:
            continue
        title = str(item.get("title", ""))
        short = title.split("likely ", 1)[1] if "likely " in title else title.split(": ", 1)[-1]
        badges[key] = dbc.Badge([html.I(className="fas fa-brain me-1"), short[:40]],
                                className="ai-badge ai-finding", title=str(item.get("message", ""))[:300])
    return badges


def peak_table(rows):
    if not rows:
        return html.Div()
    return dbc.Table([
        html.Thead(html.Tr([html.Th("Peak (Hz)"), html.Th("x running speed"), html.Th("Amplitude"),
                            html.Th("Matches")])),
        html.Tbody([html.Tr([html.Td(f"{r['hz']:.2f}"), html.Td("" if r["order"] is None else f"{r['order']:.2f}"),
                             html.Td(f"{r['amplitude']:.4g}"), html.Td(r["match"])]) for r in rows])],
        size="sm", striped=True, bordered=False, className="mt-2")


# ---------------------------------------------------------------- callbacks
def register(app, get_connection, data_folder):
    ai_folder = os.path.join(data_folder, "ai")

    @app.callback([Output("ai-gw-enabled", "value")] + [Output("ai-" + key, "value") for key in SETUP_KEYS]
                  + [Output("ai-speed-hint", "children"), Output("ai-bearing-list", "children")],
                  Input("asset-channel", "value"))
    def ai_load_setup(value):
        if not value:
            return [False, "", "", "vds", None, 50, None, None, "", None, []]
        gid, ch = json.loads(value)
        with closing(get_connection()) as conn:
            settings = historian.ai_channel_settings(conn, gid, ch)
            state = historian.get_json(conn, "channel_state", "state", gid, ch, {})
            switched = historian.ai_enabled(conn, gid)
            names = designations(library(conn))
        rpm = state.get("rpm")
        hint = (dbc.Alert(f"This VDS reports {rpm:.0f} RPM from its speed sensor.", color="light", className="py-2")
                if isinstance(rpm, (int, float)) and rpm > 0 else
                dbc.Alert("This VDS has not reported a machine speed. Choose 'Fixed speed' and enter the RPM, so the "
                          "AI and the spectrum lines can use running-speed and bearing frequencies.",
                          color="warning", className="py-2"))
        defaults = {"speed_source": "vds", "line_frequency_hz": 50, "machine_type": "", "bearing": "",
                    "iso_class": ""}
        return ([switched] + [settings.get(key, defaults.get(key)) for key in SETUP_KEYS]
                + [hint, [html.Option(value=name) for name in names]])

    @app.callback(Output("ai-bearing-preview", "children"),
                  [Input("ai-bearing", "value"), Input("ai-speed_source", "value"), Input("ai-fixed_rpm", "value"),
                   Input("ai-bearing-result", "children")], State("asset-channel", "value"))
    def ai_bearing_preview(bearing, source, fixed, _added, value):
        if not bearing:
            return None
        with closing(get_connection()) as conn:
            found = find_bearing(library(conn), bearing)
            live = (historian.get_json(conn, "channel_state", "state", *json.loads(value), {}).get("rpm")
                    if value else None)
        if not found:
            return dbc.Alert(f"{bearing} is not in the bearing library yet. Add it below, or check the number.",
                             color="warning", className="py-2")
        rpm, where = resolve_rpm({"speed_source": source, "fixed_rpm": fixed}, live)
        text = bearing_summary(found, rpm)
        speed = f" at {rpm:.0f} RPM ({where})" if rpm else " (x shaft speed; enter a speed to see Hz)"
        return dbc.Alert([html.Strong(text), speed, html.Br(),
                          html.Small(f"Source: {found.get('source', '')}", className="text-muted")],
                         color="success", className="py-2")

    @app.callback(Output("ai-result", "children"), Input("ai-save", "n_clicks"),
                  [State("asset-channel", "value"), State("ai-gw-enabled", "value")]
                  + [State("ai-" + key, "value") for key in SETUP_KEYS], prevent_initial_call=True)
    def ai_save_setup(_n, value, switched, *values):
        if not enabled():
            return dbc.Alert(NOT_IRIV, color="danger")
        try:
            if not value:
                raise ValueError("Choose a VDS and channel first")
            gid, ch = json.loads(value)
            with closing(get_connection()) as conn, conn:
                previous = historian.ai_channel_settings(conn, gid, ch)
                form = dict(zip(SETUP_KEYS, values), machine=previous.get("machine", ""),
                            location=previous.get("location", ""), direction=previous.get("direction", ""))
                historian.save_ai_channel(conn, gid, ch, form)
                historian.save_ai_gateway(conn, gid, bool(switched))
            note = "" if switched else f" AI analysis is off for {gid}; switch it on to start learning."
            return dbc.Alert("AI setup saved. The AI uses it from its next scan." + note, color="success")
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger")

    @app.callback(Output("ai-relearn-result", "children"), Input("ai-relearn", "n_clicks"),
                  State("asset-channel", "value"), prevent_initial_call=True)
    def ai_relearn(_n, value):
        if not enabled():
            return dbc.Alert(NOT_IRIV, color="danger")
        if not value:
            return dbc.Alert("Choose a VDS and channel first", color="danger")
        gid, ch = json.loads(value)
        with closing(get_connection()) as conn, conn:
            historian.request_ai_relearn(conn, gid, ch)
        return dbc.Alert(f"{gid} CH{ch} will be relearned from its next samples.", color="success")

    @app.callback([Output("ai-bearing-orders", "style"), Output("ai-bearing-geometry", "style")],
                  Input("ai-bearing-mode", "value"))
    def ai_bearing_mode(mode):
        hidden = {"display": "none"}
        return (hidden, {}) if mode == "geometry" else ({}, hidden)

    @app.callback(Output("ai-bearing-result", "children"), Input("ai-bearing-save", "n_clicks"),
                  [State("ai-new-bearing", "value"), State("ai-bearing-mode", "value")]
                  + [State("ai-b-" + key, "value") for key in BEARING_FIELDS], prevent_initial_call=True)
    def ai_add_bearing(_n, designation, mode, *values):
        if not enabled():
            return dbc.Alert(NOT_IRIV, color="danger")
        try:
            with closing(get_connection()) as conn, conn:
                data = historian.save_ai_bearing(conn, designation, dict(zip(BEARING_FIELDS, values), mode=mode))
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger")
        return dbc.Alert(f"Added {data['designation']}: BPFO {data['bpfo']}, BPFI {data['bpfi']}, BSF {data['bsf']}, "
                         f"FTF {data['ftf']} x shaft speed. You can now type it as the bearing number.",
                         color="success")

    @app.callback(Output("modal-feedback-result", "children"),
                  Input({"type": "ai-feedback", "verdict": ALL, "key": ALL}, "n_clicks"), prevent_initial_call=True)
    def ai_feedback(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update
        if not enabled():
            return dbc.Alert(NOT_IRIV, color="danger")
        gid, ch, kind, since_ms, fault_id = json.loads(trigger["key"])
        user = request.authorization.username if request.authorization else "local-admin"
        try:
            with closing(get_connection()) as conn, conn:
                historian.save_ai_feedback(conn, gid, ch, kind, since_ms, fault_id, trigger["verdict"], user)
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger")
        text = {"confirmed": "Thank you. The AI keeps this fault's pattern to recognise it again.",
                "wrong": "Thank you. The AI learns this pattern as normal operation and closes the finding.",
                "repaired": "Thank you. The AI relearns this machine's new normal."}[trigger["verdict"]]
        return dbc.Alert(f"{gid} CH{ch}: {text} It applies this on its next scan.", color="success", duration=8000)

    @app.callback(Output("modal-bearing-result", "children"), Input("modal-save-bearing", "n_clicks"),
                  [State("modal-target", "data"), State("modal-bearing", "value")], prevent_initial_call=True)
    def ai_save_modal_bearing(_n, target, bearing):
        if not enabled():
            return NOT_IRIV
        if not target:
            return no_update
        try:
            with closing(get_connection()) as conn, conn:
                settings = historian.ai_channel_settings(conn, target["gid"], target["ch"])
                historian.save_ai_channel(conn, target["gid"], target["ch"], dict(settings, bearing=bearing or ""))
        except (ValueError, TypeError) as error:
            return str(error)
        return f"Saved {bearing or 'no bearing'} for {target['gid']} CH{target['ch']}."

    @app.callback(Output("ai-health-progress", "children"), Input("system_info_interval", "n_intervals"))
    def ai_health_progress(_n):
        report = read_json(os.path.join(ai_folder, "findings.json"), None)
        if report is None:
            return html.P("The AI advisor is not running. Install it with bash iriv-edge/install.sh.",
                          className="text-muted")
        rows = []
        for item in report.get("progress", []):
            if item.get("phase") == "learning":
                phase = (f"Learning: {item.get('samples') or 0}/{item.get('needed_samples')} samples, "
                         f"{item.get('hours') or 0}/{item.get('needed_hours')} h")
            else:
                phase = str(item.get("phase", "")).capitalize()
            rows.append(html.Tr([html.Td(str(item.get("name", ""))),
                                 html.Td(f"{item.get('gateway_id')} CH{item.get('channel')}"), html.Td(phase),
                                 html.Td(str(item.get("trend", ""))), html.Td("; ".join(item.get("notes", [])))]))
        storage = (report.get("storage") or {}).get("message", "")
        head = html.Small(f"{report.get('summary', '')} {storage}", className="text-muted d-block mb-2")
        if not rows:
            return [head, html.P("No sensors have AI analysis switched on. Switch it on in the asset registry.",
                                 className="text-muted")]
        return [head, dbc.Table([html.Thead(html.Tr([html.Th(h) for h in ("Machine", "Sensor", "Status", "Trend",
                                                                         "Notes")])), html.Tbody(rows)],
                                size="sm", striped=True)]

    @app.callback(Output("ai-report-select", "options"), Input("url", "pathname"))
    def ai_report_list(_path):
        index = read_json(os.path.join(ai_folder, "index.json"), [])
        return [{"label": f"{datetime.fromtimestamp(e.get('created') or 0):%Y-%m-%d %H:%M} — {e.get('title')}",
                 "value": i} for i, e in enumerate(index)]

    @app.callback(Output("ai-report-view", "children"), Input("ai-report-select", "value"))
    def ai_show_report(choice):
        index = read_json(os.path.join(ai_folder, "index.json"), [])
        if choice is None or not isinstance(choice, int) or not 0 <= choice < len(index):
            return html.P("Choose a report to read it here.", className="text-muted mb-0")
        entry = index[choice]
        return [dcc.Markdown(str(entry.get("markdown", ""))),
                html.Small(f"Folder on the device: {entry.get('folder', '')}", className="text-muted")]
