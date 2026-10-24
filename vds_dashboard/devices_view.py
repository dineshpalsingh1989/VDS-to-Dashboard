"""VDS130 page: approve new VDS units before their data is used, and see every unit at a glance.

A VDS that publishes for the first time waits here as "New, waiting for approval". Nothing it
sends is stored or shown until an administrator allows it; then it appears on Home with its next
message. A blocked VDS stays ignored. Units that already sent data before approval existed were
allowed automatically, so an update never stops a running plant.
"""

import time
from contextlib import closing
from datetime import datetime

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, ctx, dcc, html, no_update

from . import historian
from .vds import freshness

READ_CALLBACKS = {"devices_list"}


def when(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else "—"


def section():
    return html.Div([
        html.Div(id="devices-pending"),
        html.H5([html.I(className="fas fa-microchip me-2"), "VDS130 units"], className="mt-3"),
        html.Div(id="devices-table"),
        html.Div(id="devices-result", className="mt-2"),
        dcc.Interval(id="devices-interval", interval=5000, n_intervals=0),
        html.Hr(className="my-4"),
    ])


def pending_banner(conn):
    """For Home: a notice when a new VDS is waiting, with a link to the VDS130 page."""
    waiting = [d for d in historian.devices(conn) if d["status"] == "pending"]
    if not waiting or not historian.approval_required():
        return None
    names = ", ".join(d["gateway_id"] for d in waiting[:3]) + (" and more" if len(waiting) > 3 else "")
    return dbc.Alert([html.I(className="fas fa-plug me-2"),
                      html.Strong(f"New VDS130 found: {names}. "),
                      "It is not used until you allow it. ",
                      dbc.Button("Open VDS130", href="/assets", color="warning", size="sm", className="ms-2")],
                     color="warning", className="d-flex align-items-center flex-wrap", id="devices-banner")


def register(app, get_connection, get_storage):
    @app.callback([Output("devices-pending", "children"), Output("devices-table", "children")],
                  [Input("devices-interval", "n_intervals"), Input("devices-result", "children")])
    def devices_list(_n, _result):
        with closing(get_connection()) as conn:
            items = historian.devices(conn)
        storage = get_storage()
        waiting = [d for d in items if d["status"] == "pending"]
        cards = []
        for d in waiting:
            info = d["info"]
            facts = [f"Channels seen: {', '.join('CH' + c for c in info.get('channels', [])) or '—'}",
                     f"Sensor: {info.get('sensor_type') or 'not reported'}",
                     f"Speed: {info.get('rpm'):.0f} RPM" if info.get("rpm") else "Speed: not reported",
                     f"Messages: {info.get('messages', 0)}", f"First seen {when(d['first_seen'])}",
                     f"Last seen {when(d['last_seen'])}"]
            cards.append(html.Div([
                html.Div([html.I(className="fas fa-plug me-2"), html.Strong(d["gateway_id"]),
                          html.Span("New, waiting for approval", className="status-pill status-warning ms-2")]),
                html.Div(" · ".join(facts), className="small text-muted my-2"),
                dbc.Button([html.I(className="fas fa-check me-1"), "Allow"], color="success", size="sm", className="me-2",
                           id={"type": "device-action", "gid": d["gateway_id"], "action": "allowed"}, n_clicks=0),
                dbc.Button([html.I(className="fas fa-ban me-1"), "Block"], color="secondary", outline=True, size="sm",
                           id={"type": "device-action", "gid": d["gateway_id"], "action": "blocked"}, n_clicks=0),
            ], className="device-card"))
        pending = html.Div([html.H5([html.I(className="fas fa-bell me-2 text-warning"), "New VDS130 waiting for approval"]),
                            html.P("Nothing these units send is stored or shown until you allow them. Check the serial "
                                   "number against the unit on site.", className="text-muted small"),
                            html.Div(cards, className="device-cards")], className="devices-pending") if cards else None
        rows = []
        for d in items:
            if d["status"] == "pending":
                continue
            device = storage.get(d["gateway_id"], {})
            channels = device.get("channels", {})
            live = sum(1 for c in channels.values() if freshness(c)["status"] == "fresh")
            allowed = d["status"] == "allowed"
            state = (("Online" if live else "Offline") if allowed else "Blocked")
            tone = {"Online": "status-fresh", "Offline": "status-stale", "Blocked": "status-alert"}[state]
            action = ("blocked", "Block", "fa-ban", "secondary") if allowed else ("allowed", "Allow", "fa-check", "success")
            rows.append(html.Tr([
                html.Td(html.Strong(d["gateway_id"])), html.Td(html.Span(state, className=f"status-pill {tone}")),
                html.Td(f"{live} / {len(channels)} live" if allowed else "—"),
                html.Td(device.get("last_seen") or when(d["last_seen"])),
                html.Td(dbc.Button([html.I(className=f"fas {action[2]} me-1"), action[1]], color=action[3], outline=True,
                                   size="sm", id={"type": "device-action", "gid": d["gateway_id"], "action": action[0]},
                                   n_clicks=0)),
            ]))
        table = (dbc.Table([html.Thead(html.Tr([html.Th(h) for h in ("Serial number", "Status", "Channels", "Last data",
                                                                     "")])), html.Tbody(rows)],
                           size="sm", hover=True, className="mb-0")
                 if rows else html.P("No VDS130 allowed yet. A unit appears above when it first publishes.",
                                     className="text-muted"))
        return pending, table

    @app.callback(Output("devices-result", "children"),
                  Input({"type": "device-action", "gid": ALL, "action": ALL}, "n_clicks"), prevent_initial_call=True)
    def device_action(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []):
            return no_update
        with closing(get_connection()) as conn, conn:
            historian.set_device_status(conn, trigger["gid"], trigger["action"])
        if trigger["action"] == "allowed":
            return dbc.Alert(f"{trigger['gid']} allowed. It appears on Home with its next message "
                             f"({time.strftime('%H:%M:%S')}).", color="success", duration=8000)
        return dbc.Alert(f"{trigger['gid']} blocked: its messages are ignored.", color="secondary", duration=8000)
