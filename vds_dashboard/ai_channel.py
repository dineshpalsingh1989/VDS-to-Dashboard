"""AI on each channel card (IRIV Edge AI only, VDS_EDGE_AI=1).

* A brain icon shows what the AI is doing on the channel: off, needs setup, learning, watching, or found something.
* A gear icon opens the AI setup. The AI will not learn a channel until it knows the machine type,
  the machine's ISO vibration class (from its power and foundation) and the running speed.
* A bell flashes when the AI has a finding. It opens a plain-language explanation with a picture:
  the learned normal spectrum against today's, with the peaks that changed marked.

Advisory only: nothing here sends a command to a machine.
"""

import json
import os
import time
from contextlib import closing

import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from flask import request

from . import ai_views, historian

READ_CALLBACKS = {"ai_setup_open", "ai_setup_iso", "ai_setup_close", "ai_explain_open", "ai_explain_close",
                  "ai_setup_fields", "ai_setup_ready", "ai_setup_drawing"}
ISO_LIMITS = {"group1_rigid": (2.3, 4.5, 7.1), "group1_flexible": (3.5, 7.1, 11.0), "group2_rigid": (1.4, 2.8, 4.5),
              "group2_flexible": (2.3, 4.5, 7.1), "class1_small": (0.71, 1.8, 4.5)}
ISO_NAMES = {"group1_rigid": "ISO 20816-3 group 1, rigid foundation",
             "group1_flexible": "ISO 20816-3 group 1, flexible foundation",
             "group2_rigid": "ISO 20816-3 group 2, rigid foundation",
             "group2_flexible": "ISO 20816-3 group 2, flexible foundation",
             "class1_small": "ISO 10816-1 class I, small machine"}
ZONES = (("A", "New machine", "#16a34a"), ("B", "Fine for long-term running", "#65a30d"),
         ("C", "Restricted: plan a repair", "#f59e0b"), ("D", "Damage likely", "#dc2626"))
EVIDENCE_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

# Fault names from the knowledge folder, in words a machine operator uses
PLAIN = (("outer race", "the outer ring (outer race) of {bearing} is starting to wear"),
         ("inner race", "the inner ring (inner race) of {bearing} is starting to wear"),
         ("rolling element", "a ball or roller in {bearing} is damaged"),
         ("ball", "a ball or roller in {bearing} is damaged"),
         ("cage", "the cage that holds the balls in {bearing} is wearing"),
         ("unbalance", "the rotor is out of balance (unbalance)"),
         ("misalignment", "the shafts are out of line (misalignment)"),
         ("looseness", "something is loose: base bolts, the bearing fit or the foundation (looseness)"),
         ("gear", "the gear teeth are wearing or the gears are misaligned"),
         ("blade", "the impeller or fan blades have a problem (blade pass)"),
         ("vane", "the impeller or fan blades have a problem (blade pass)"),
         ("electrical", "the motor has an electrical fault"),
         ("lubrication", "the bearing needs grease or oil (poor lubrication)"))


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def live_rpm(storage, gid, ch):
    device = (storage or {}).get(gid, {})
    channel = device.get("channels", {}).get(ch, {})
    for value in (channel.get("rpm"), device.get("rpm")):
        try:
            if value and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def channel_status(settings, gateway_on, rpm, progress, findings):
    """(state, short label, tooltip) for one channel. An open finding from the advisor always shows."""
    if findings:
        title = str(findings[0].get("title", "AI finding"))
        short = title.split("likely ", 1)[1] if "likely " in title else title.split(": ", 1)[-1]
        tone = "warning" if any(f.get("severity") == "warning" for f in findings) else "info"
        return f"finding-{tone}", short[:48], title
    if not gateway_on or settings.get("ai_on") is False:
        return "off", "AI off", "AI is off for this channel. Open the AI setup (gear) to turn it on."
    phase = (progress or {}).get("phase")
    missing = historian.ai_setup_missing(settings, rpm) if phase not in ("learning", "monitoring") else []
    if phase == "needs setup":
        missing = progress.get("missing") or missing or ["machine details"]
    if missing:
        return "setup", "Needs setup", "The AI needs " + ", ".join(missing) + ". Open the AI setup (gear)."
    if phase == "learning":
        needed = progress.get("needed_samples") or 1
        done = min(100, round(100 * (progress.get("samples") or 0) / needed))
        return "learning", f"Learning {done}%", ("The AI is learning this machine's normal vibration "
                                                  f"({progress.get('samples') or 0}/{needed} samples).")
    if phase == "monitoring":
        return "watching", "Watching", "The AI knows this machine's normal and is watching for changes."
    return "starting", "Starting", "The AI starts when the next readings arrive."


def channel_tools(data_folder, storage, conn):
    """Brain, gear and bell for every channel card, keyed by (gateway, channel)."""
    report = read_json(os.path.join(data_folder, "ai", "findings.json"), {})
    progress = {(p.get("gateway_id"), p.get("channel")): p for p in report.get("progress", [])}
    findings = {}
    for item in report.get("findings", []):
        if item.get("gateway_id"):
            findings.setdefault((item["gateway_id"], item.get("channel")), []).append(item)
    settings = {(g, c): json.loads(s or "{}") for g, c, s in conn.execute("SELECT gateway_id, channel, settings FROM ai_settings")}
    tools = {}
    for gid, device in (storage or {}).items():
        gateway_on = bool(settings.get((gid, ""), {}).get("enabled"))
        for ch in device.get("channels", {}):
            state, label, tip = channel_status(settings.get((gid, ch), {}), gateway_on, live_rpm(storage, gid, ch),
                                               progress.get((gid, ch)), findings.get((gid, ch)))
            items = [html.Span([html.I(className="fas fa-brain me-1"), label], className=f"ai-brain ai-{state}",
                               title=tip),
                     html.Button(html.I(className="fas fa-cog"), id={"type": "ai-setup-open", "gid": gid, "ch": ch},
                                 n_clicks=0, className="ai-icon-btn", title="AI setup for this channel",
                                 **{"aria-label": f"AI setup for {gid} CH{ch}"})]
            if state.startswith("finding"):
                items.append(html.Button(html.I(className="fas fa-bell"),
                                         id={"type": "ai-bell", "gid": gid, "ch": ch}, n_clicks=0,
                                         className=f"ai-icon-btn ai-bell {'flash' if state == 'finding-warning' else ''}",
                                         title="Why the AI thinks there is an issue",
                                         **{"aria-label": f"AI finding on {gid} CH{ch}: why"}))
            tools[(gid, ch)] = html.Div(items, className="ai-tools" + (" ai-finding" if state.startswith("finding") else ""))
    return tools


# ---------------------------------------------------------------- explanation
def ratio_words(db):
    times = 10 ** (db / 20)
    return f"about {times:.0f} times stronger than usual" if times >= 2 else f"{(times - 1) * 100:.0f}% stronger than usual"


def plain_cause(name, bearing):
    lowered = name.lower()
    for key, sentence in PLAIN:
        if key in lowered:
            return sentence.format(bearing=f"bearing {bearing}" if bearing else "the bearing")
    return name[0].lower() + name[1:] if name else "something changed"


def certainty(confidence):
    return "very likely" if confidence >= 0.8 else "likely" if confidence >= 0.6 else "possibly"


def headline(finding, bearing):
    matches = (finding.get("detail") or {}).get("matches") or []
    kind = finding.get("kind", "")
    if matches:
        best = matches[0]
        return f"The AI thinks {plain_cause(best['name'], bearing)}: {certainty(best['confidence'])} ({best['confidence']:.0%})."
    if kind == "trend":
        return "The vibration keeps rising. If it goes on, it reaches the warning level soon."
    if kind == "severity":
        return "The vibration is above the limit for this type of machine (ISO standard)."
    if kind == "machine":
        return "Comparing the sensors on this machine shows a pattern: " + str(finding.get("title", "")).split(": ", 1)[-1] + "."
    if kind == "waveform":
        return "The shape of the vibration changed: the machine shows more knocks or impacts than usual."
    if kind.startswith("metric:"):
        return f"{kind.split(':', 1)[1]} is clearly different from what is normal for this machine."
    return "The vibration pattern changed compared with what the AI learned as normal for this machine."


def what_changed(finding):
    """What the AI saw, as short sentences."""
    detail = finding.get("detail") or {}
    rpm, lines = detail.get("rpm"), []
    levels = {round(level["hz"], 2): level for level in detail.get("levels", [])}
    unit = detail.get("unit", "")
    for hz, rise in detail.get("peaks", [])[:3]:
        order = f", {hz / (rpm / 60):.2f}x running speed" if rpm else ""
        level = levels.get(round(hz, 2))
        if level and level["normal"] > 0 and not unit:
            times = level["now"] / level["normal"]
            lines.append(f"At {hz:.1f} Hz ({hz * 60:,.0f} CPM{order}) the bearing impact level is {times:.1f} times "
                         "its normal level.")
        elif level and level["normal"] > 0:
            times = level["now"] / level["normal"]
            lines.append(f"At {hz:.1f} Hz ({hz * 60:,.0f} CPM{order}) the vibration went from {level['normal']:.3g} to "
                         f"{level['now']:.3g} {unit}. That is " + (f"about {times:.0f} times" if times >= 2 else
                                                                  f"{(times - 1) * 100:.0f}% more than") + " normal.")
        else:
            lines.append(f"A peak at {hz:.1f} Hz ({hz * 60:,.0f} CPM{order}) is {rise:.0f} dB above normal. "
                         f"That is {ratio_words(rise)}.")
    floor = (detail.get("floor_db") or {}).get("upper") or 0
    if floor >= 3:
        lines.append(f"The background noise at high frequency rose {floor:.0f} dB. Worn or dry bearings do this.")
    for change in detail.get("changes", []):
        lines.append(f"Waveform: {change}.")
    if "value" in detail and detail.get("normal"):
        lines.append(f"{finding.get('kind', '').split(':', 1)[-1]} is {detail['value']:.3g} {detail.get('unit', '')}, "
                     f"{detail['value'] / detail['normal']:.1f} times its normal {detail['normal']:.3g} {detail.get('unit', '')}.")
    if not lines:
        lines.append(str(finding.get("message", ""))[:400])
    return lines


def evidence_figure(evidence, finding):
    """Normal (grey) against now (blue), with the changed peaks circled and the fault lines named."""
    if not evidence or "freqs" not in evidence:
        return None
    x = np.asarray(evidence["freqs"], dtype=float)
    if "now" in evidence:  # amplitude in mm/s (or g for envelope spectra), from the advisor
        now, normal = np.asarray(evidence["now"], dtype=float), np.asarray(evidence["normal_level"], dtype=float)
        unit, axis = evidence.get("unit", ""), evidence.get("axis", "Amplitude")
    else:  # evidence written before units were published
        now, normal = np.asarray(evidence["current_db"], dtype=float), np.asarray(evidence["normal_db"], dtype=float)
        unit, axis = "dB", "Level (dB)"
    figure = go.Figure()
    figure.add_trace(go.Scatter(x=x, y=normal, name="Normal (learned)", mode="lines", fill="tozeroy",
                                line=dict(color="#94a3b8", width=1), fillcolor="rgba(148,163,184,0.25)",
                                hovertemplate="%{x:.1f} Hz<br>normal %{y:.3g} " + unit + "<extra></extra>"))
    figure.add_trace(go.Scatter(x=x, y=now, name="Now", mode="lines", line=dict(color="#1d4ed8", width=1.6),
                                hovertemplate="%{x:.1f} Hz<br>now %{y:.3g} " + unit + "<extra></extra>"))
    peaks = evidence.get("peaks") or []
    if peaks:
        at = [float(now[int(np.argmin(np.abs(x - hz)))]) for hz, _rise in peaks]
        figure.add_trace(go.Scatter(x=[hz for hz, _r in peaks], y=at, mode="markers", name="Changed peak",
                                    marker=dict(size=18, color="rgba(0,0,0,0)", line=dict(color="#dc2626", width=3)),
                                    hovertemplate="%{x:.1f} Hz changed<extra></extra>"))
        top_level = max(float(np.max(now)) if now.size else 0, 1e-12)
        for i, ((hz, rise), y) in enumerate(zip(peaks[:3], at, strict=False)):
            was = float(normal[int(np.argmin(np.abs(x - hz)))])
            if unit == "dB" or was <= 0:
                text = f"<b>+{rise:.0f} dB</b><br>{hz:.1f} Hz"
            elif not unit:  # envelope: no physical unit, so say how many times normal
                text = f"<b>{y / was:.0f}× normal</b><br>{hz:.1f} Hz"
            else:
                text = f"<b>{was:.2g} → {y:.2g} {unit}</b><br>{hz:.1f} Hz"
            figure.add_annotation(x=hz, y=y, text=text, showarrow=True, arrowhead=2,
                                  arrowcolor="#dc2626", ax=-45 if i % 2 == 0 else 45, ay=-30 - 22 * (i % 2),
                                  font=dict(color="#dc2626", size=12),
                                  bgcolor="rgba(255,255,255,0.9)", bordercolor="#dc2626")
    top = float(x.max()) if x.size else 0
    named = 0
    for row in evidence.get("lines") or []:
        hz = row.get("hz")
        near = any(abs(hz - p[0]) <= max(2.0, 0.03 * hz) for p in peaks)
        if hz and hz <= top and (near or row["name"] in ("Running speed",)):
            figure.add_vline(x=hz, line=dict(color="#7c3aed" if near else "#64748b", width=1.2, dash="dot"))
            figure.add_annotation(x=hz, y=0.03 + 0.08 * (named % 2), yref="paper", yanchor="bottom", xanchor="left",
                                  showarrow=False, text=row["name"], bgcolor="rgba(255,255,255,0.85)",
                                  font=dict(size=10, color="#7c3aed" if near else "#64748b"))
            named += 1
    if peaks:  # open zoomed on the change; double-click shows everything
        span = max(p[0] for p in peaks) * 2.2
        if span < top:
            figure.update_xaxes(range=[0, span])
    figure.update_layout(template="plotly_white", height=320, margin=dict(l=50, r=20, t=10, b=70),
                         legend=dict(orientation="h", y=-0.28, x=0), hovermode="x unified",
                         xaxis_title="Frequency (Hz)", yaxis_title=axis)
    if peaks and unit != "dB":  # headroom above the highest peak for its label
        figure.update_yaxes(range=[min(0.0, float(np.min(now))), top_level * 1.7])
    return figure


def trend_picture(conn, finding):
    """For rising readings and ISO alarms: the last 7 days of velocity with the ISO zones behind it."""
    gid, ch = finding.get("gateway_id"), finding.get("channel")
    metric = finding.get("kind", "").split(":", 1)[1] if finding.get("kind", "").startswith("metric:") else "Velocity"
    rows = conn.execute("SELECT timestamp, value FROM trends WHERE gateway_id=? AND channel=? AND metric_name=? "
                        "ORDER BY timestamp DESC LIMIT 2000", (gid, ch, metric)).fetchall()[::-1]
    if not rows:
        return None
    figure = go.Figure(go.Scatter(x=[r[0] for r in rows], y=[r[1] for r in rows], mode="lines", name=metric,
                                  line=dict(color="#1d4ed8", width=1.6)))
    limits = ISO_LIMITS.get(historian.ai_channel_settings(conn, gid, ch).get("iso_class"))
    if limits and metric == "Velocity":
        edges = (0,) + limits + (limits[-1] * 1.5,)
        for (zone, text, colour), low, high in zip(ZONES, edges, edges[1:], strict=False):
            figure.add_hrect(y0=low, y1=high, fillcolor=colour, opacity=0.08, line_width=0,
                             annotation_text=f"Zone {zone}", annotation_position="top left")
    normal = (finding.get("detail") or {}).get("normal")
    if normal:
        figure.add_hline(y=normal, line=dict(color="#64748b", dash="dash"), annotation_text="normal")
    figure.update_layout(template="plotly_white", height=280, margin=dict(l=50, r=20, t=20, b=40),
                         yaxis_title=metric, showlegend=False)
    return figure


def waveform_picture(evidence):
    data = (evidence or {}).get("waveform") or {}
    current, normal = data.get("current") or {}, data.get("normal") or {}
    names = [k for k in ("crest", "kurtosis") if k in current and k in normal]
    if not names:
        return None
    figure = go.Figure([go.Bar(x=[n.capitalize() for n in names], y=[normal[n] for n in names], name="Normal",
                               marker_color="#94a3b8"),
                        go.Bar(x=[n.capitalize() for n in names], y=[current[n] for n in names], name="Now",
                               marker_color="#dc2626")])
    figure.update_layout(template="plotly_white", height=240, barmode="group", margin=dict(l=40, r=20, t=20, b=30),
                         legend=dict(orientation="h", y=1.15))
    return figure


def second_opinion(opinion):
    """What the neural network (on the Hailo AI HAT+ or the processor) thinks, beside the rule-based diagnosis."""
    if not opinion or not opinion.get("top"):
        return None
    where = "the Hailo AI HAT+" if opinion.get("backend") == "hailo" else "the processor (no Hailo HAT in use)"
    names = opinion.get("names") or [c for c, _p in opinion["top"]]
    ranked = ", ".join(f"{name} {p:.0%}" for name, (_c, p) in zip(names, opinion["top"], strict=False))
    verdict = ("It agrees with the diagnosis above." if opinion.get("agrees") else
               "It sees this differently, so weigh both before acting.")
    return html.Div([html.Div("Second opinion: neural network", className="explain-label"),
                     html.P([html.I(className="fas fa-microchip me-2"), f"The neural network on {where} says: {ranked}. ",
                             html.Span(verdict, className="fw-semibold")], className="mb-1")])


def explanation(data_folder, conn, gid, ch, admin):
    report = read_json(os.path.join(data_folder, "ai", "findings.json"), {})
    items = [f for f in report.get("findings", []) if f.get("gateway_id") == gid and f.get("channel") == ch]
    if not items:
        return html.P("The AI has no open finding on this channel any more.", className="text-muted")
    settings = historian.ai_channel_settings(conn, gid, ch)
    bearing = settings.get("bearing", "")
    blocks = []
    for item in items:
        evidence, name = None, str(item.get("evidence") or "")
        if name and set(name) <= EVIDENCE_NAME:
            evidence = read_json(os.path.join(data_folder, "ai", "evidence", f"{name}.json"), None)
        figure = evidence_figure(evidence, item) or waveform_picture(evidence)
        if figure is None and (item.get("kind") in ("trend", "severity", "machine") or str(item.get("kind", "")).startswith("metric:")):
            figure = trend_picture(conn, item)
        matches = (item.get("detail") or {}).get("matches") or []
        best = matches[0] if matches else {}
        since = item.get("since_ms")
        key = json.dumps([gid, ch, item.get("kind"), since, item.get("fault_id")])
        buttons = [dbc.Button(label, id={"type": "ai-explain-feedback", "verdict": verdict, "key": key}, size="sm",
                              color=colour, outline=True, className="me-2", disabled=not admin)
                   for verdict, label, colour in ai_views.VERDICTS] if item.get("kind") not in ("severity", "machine") \
            else []
        warning = item.get("severity") == "warning"
        blocks.append(html.Div([
            html.Div([html.Span("Act soon" if warning else "Keep an eye on it",
                                className=f"explain-chip {'chip-warning' if warning else 'chip-info'}"),
                      html.Small(f"since {time.strftime('%d %b %Y %H:%M', time.localtime(since / 1000))}"
                                 if since else "", className="text-muted ms-2")]),
            html.H5(headline(item, bearing), className="explain-headline"),
            html.Div([html.Div("What the AI saw", className="explain-label"),
                      html.Ul([html.Li(text) for text in what_changed(item)])]),
            dcc.Graph(figure=figure, config={"displaylogo": False, "displayModeBar": False}) if figure else
            html.Small("No picture saved for this finding.", className="text-muted"),
            html.Div([html.Div("Why it thinks so", className="explain-label"),
                      html.Ul([html.Li(text) for text in best.get("evidence", [])])]) if best.get("evidence") else None,
            second_opinion((item.get("detail") or {}).get("brain")),
            html.Div([html.Div("Across the machine", className="explain-label"),
                      html.Ul([html.Li(text) for text in item.get("machine_notes", [])])])
            if item.get("machine_notes") else None,
            html.Div([html.Div("Also possible", className="explain-label"),
                      html.P(", ".join(m["name"] for m in matches[1:3]))]) if len(matches) > 1 else None,
            html.Div([html.Div("What to do", className="explain-label"),
                      html.P(best.get("advice") or "Check the machine at the next stop. Confirm with a hand-held "
                             "measurement or an inspection before any repair.")]),
            html.Details([html.Summary("Full AI message", className="small"),
                          html.P(str(item.get("message", "")), className="small text-muted mt-1")]),
            html.Div([html.Div("After you check the machine, tell the AI", className="explain-label"), *buttons])
            if buttons else None,
        ], className="explain-block"))
    blocks.append(html.P("This is advice from the AI, not a measurement result. Confirm with an inspection before "
                         "maintenance.", className="small text-muted mb-0"))
    return blocks


# ---------------------------------------------------------------- layout
def iso_preview(machine_type, power_kw, foundation):
    try:
        iso = historian.iso_class_for(power_kw, foundation)
    except (TypeError, ValueError):
        iso = ""
    if not iso:
        return html.Small("Enter the power and foundation; the ISO class and its limits appear here.",
                          className="text-muted")
    limits = ISO_LIMITS[iso]
    edges = ("0",) + tuple(f"{v:g}" for v in limits)
    cells = [html.Div([html.Strong(f"Zone {zone}"), html.Div(text, className="small"),
                       html.Div(f"{low}–{high} mm/s" if high else f"above {low} mm/s", className="small")],
                      className="iso-zone", style={"borderTopColor": colour})
             for (zone, text, colour), low, high in zip(ZONES, edges, edges[1:] + (None,), strict=False)]
    what = ai_views.LABELS.get(machine_type, "Machine") if machine_type else "Machine"
    return html.Div([html.Div([html.I(className="fas fa-check-circle text-success me-1"),
                               f"{what}: {ISO_NAMES[iso]} (velocity RMS)"], className="small mb-2"),
                     html.Div(cells, className="iso-zones")])


def modals():
    def required(text):
        return [text, html.Span(" *", className="text-danger")]

    setup = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle(id="ai-setup-title")),
        dbc.ModalBody([
            dcc.Store(id="ai-setup-target"),
            html.Div(id="ai-setup-status"),
            dbc.Switch(id="aim-ai_on", label="AI on for this channel", value=False, disabled=True, className="mb-1"),
            html.Div(id="aim-ai-hint", className="small mb-3"),
            html.H6([html.I(className="fas fa-industry me-2"), "Machine"], className="setup-section"),
            dbc.Row([
                dbc.Col([dbc.Label("Machine name"), dbc.Input(id="aim-machine", maxLength=64,
                                                              placeholder="e.g. Cooling pump P-101")], md=6),
                dbc.Col([dbc.Label("Measuring point note"), dbc.Input(id="aim-location", maxLength=64,
                                                                      placeholder="optional, e.g. near the coupling")], md=6),
            ], className="mb-1"),
            html.Small("Give every channel on the same machine the same machine name, so the AI can compare them.",
                       className="text-muted d-block mb-2"),
            dbc.Row([
                dbc.Col([dbc.Label(required("Power (kW)")), dbc.Input(id="aim-power_kw", type="number", min=0.05,
                                                                     max=100000, step="any", placeholder="e.g. 22")], md=4),
                dbc.Col([dbc.Label(required("Foundation")), dbc.RadioItems(id="aim-foundation", inline=True, options=[
                    {"label": "Rigid", "value": "rigid"}, {"label": "Flexible", "value": "flexible"}])], md=8),
            ], className="mb-1"),
            html.Small("Rigid: bolted to concrete. Flexible: on springs, rubber or a light steel frame.",
                       className="text-muted d-block mb-2"),
            html.Div(id="aim-iso-preview", className="mb-3"),
            html.H6([html.I(className="fas fa-map-marker-alt me-2"), "Where is the sensor?"], className="setup-section"),
            dbc.Row([
                dbc.Col([dbc.Label(required("Sensor is on the")), dcc.Dropdown(
                    id="aim-machine_type", options=ai_views.options(historian.AI_MACHINE_TYPES), clearable=False)], md=4),
                dbc.Col([dbc.Label("End"), dbc.RadioItems(id="aim-end", inline=True, options=[
                    {"label": "DE (drive end)", "value": "DE"}, {"label": "NDE (free end)", "value": "NDE"}])], md=4),
                dbc.Col([dbc.Label("Direction"), dbc.RadioItems(id="aim-direction", inline=True, options=[
                    {"label": "H", "value": "horizontal"}, {"label": "V", "value": "vertical"},
                    {"label": "A", "value": "axial"}])], md=4),
            ], className="mb-2"),
            html.Small("H horizontal, V vertical, A axial (along the shaft). Two channels can sit at the same end in "
                       "different directions.", className="text-muted d-block mb-2"),
            html.Div(id="aim-drawing", className="mb-3"),
            html.H6([html.I(className="fas fa-tachometer-alt me-2"), "Running speed"], className="setup-section"),
            dbc.RadioItems(id="aim-speed_source", value="vds", options=[
                {"label": "From the VDS speed sensor", "value": "vds"},
                {"label": "Fixed speed (no speed sensor)", "value": "fixed"}], className="mb-2"),
            dbc.Row([dbc.Col([dbc.Label("Fixed speed (RPM)"), dbc.Input(id="aim-fixed_rpm", type="number", min=1,
                                                                          max=60000, placeholder="e.g. 1480")], md=4),
                     dbc.Col(html.Div(id="aim-speed-hint", className="small mt-4"), md=8)], className="mb-3"),
            html.H6([html.I(className="fas fa-cogs me-2"), "Details for a better diagnosis"], className="setup-section"),
            dbc.Row([
                dbc.Col([dbc.Label("Bearing number"), dcc.Input(id="aim-bearing", list="aim-bearing-list", maxLength=64,
                                                                placeholder="e.g. 6205-2RS",
                                                                className="form-control")], md=4),
                dbc.Col([dbc.Label("Mains"), dbc.RadioItems(id="aim-line_frequency_hz", inline=True, value=50, options=[
                    {"label": "50 Hz", "value": 50}, {"label": "60 Hz", "value": 60}])], md=4),
                dbc.Col(html.Div([dbc.Label("Gear teeth"), dbc.Input(id="aim-gear_teeth", type="number", min=1,
                                                                      max=1000)], id="aim-gear-wrap"), md=4),
                dbc.Col(html.Div([dbc.Label("Blades / vanes"), dbc.Input(id="aim-blades", type="number", min=1,
                                                                          max=100)], id="aim-blades-wrap"), md=4),
            ], className="mb-2"),
            html.Datalist(id="aim-bearing-list"),
            html.Div(id="aim-result", className="mt-3"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Relearn this sensor", id="aim-relearn", color="secondary", outline=True, className="me-auto",
                       title="After a repair: the AI forgets the old normal and learns the new one"),
            dbc.Button("Close", id="aim-close", color="light"),
            dbc.Button("Save", id="aim-save", color="primary"),
        ]),
    ], id="ai-setup-modal", is_open=False, size="lg", scrollable=True)
    explain = dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle(id="ai-explain-title")),
        dbc.ModalBody([html.Div(id="ai-explain-body"), html.Div(id="ai-explain-result")]),
        dbc.ModalFooter(dbc.Button("Close", id="ai-explain-close", color="light")),
    ], id="ai-explain-modal", is_open=False, size="lg", scrollable=True)
    return [setup, explain]


FIELDS = ("machine", "location", "machine_type", "power_kw", "foundation", "speed_source", "fixed_rpm", "bearing",
          "direction", "line_frequency_hz", "gear_teeth", "blades", "end")
PART_NAMES = {"motor": "Motor", "pump": "Pump", "fan": "Fan", "gearbox": "Gearbox", "compressor": "Compressor",
              "other": "Machine"}
SHORT_DIRECTION = {"horizontal": "H", "vertical": "V", "axial": "A", "radial": "R"}


def setup_missing(form, rpm):
    """What still blocks the AI switch, in the words of the setup window."""
    missing = [label for key, label in (("machine_type", "the part the sensor is on"), ("power_kw", "the power (kW)"),
                                        ("foundation", "the foundation")) if form.get(key) in (None, "")]
    speed = historian.ai_setup_missing({"machine_type": "x", "iso_class": "x", "speed_source": form.get("speed_source"),
                                        "fixed_rpm": form.get("fixed_rpm")}, rpm)
    return missing + (["a fixed speed in RPM" if form.get("speed_source") == "fixed" else
                       "a running speed (the VDS reports none, so choose Fixed speed)"] if speed else [])


def machine_drawing(rows, current, form):
    """Motor, coupling and driven part, with each channel drawn at its end and labelled with its direction.

    rows: {(gid, ch): settings} of every channel; current: (gid, ch) being edited, shown with form's unsaved values.
    """
    name = str(form.get("machine") or "").strip().lower()
    members = {key: dict(value) for key, value in rows.items()
               if name and str(value.get("machine") or "").strip().lower() == name}
    members[current] = dict(members.get(current, {}), **{k: form.get(k) for k in ("machine_type", "end", "direction")})
    parts = [m.get("machine_type") for m in members.values() if m.get("machine_type")]
    driven = next((p for p in parts if p != "motor"), None)
    slots = {}
    unplaced = []
    for (gid, ch), m in sorted(members.items(), key=lambda item: (item[0][0], item[0][1])):
        part = m.get("machine_type")
        side = "motor" if part == "motor" else "driven" if part else None
        chip = html.Span(f"CH{ch} {SHORT_DIRECTION.get(m.get('direction'), '?')}" + (f" · {gid}" if gid != current[0] else ""),
                         className="ch-chip" + (" current" if (gid, ch) == current else ""),
                         title=f"{gid} CH{ch}: {PART_NAMES.get(part, 'part not set')} {m.get('end') or 'end not set'} "
                               f"{m.get('direction') or 'direction not set'}")
        if side and m.get("end") in historian.AI_ENDS:
            slots.setdefault((side, m["end"]), []).append(chip)
        else:
            unplaced.append(chip)

    def slot(side, end):
        return html.Div([html.Div(end, className="slot-name"), html.Div(slots.get((side, end), []), className="slot-chips")],
                        className="drawing-slot" + (" filled" if slots.get((side, end)) else ""))

    motor = html.Div([slot("motor", "NDE"), html.Div("Motor", className="drawing-body"), slot("motor", "DE")],
                     className="drawing-part")
    other = html.Div([slot("driven", "DE"), html.Div(PART_NAMES.get(driven, "Driven machine"), className="drawing-body"),
                      slot("driven", "NDE")], className="drawing-part")
    children = [html.Div([motor, html.Div(className="drawing-coupling", title="Coupling"), other], className="drawing-train")]
    if unplaced:
        children.append(html.Div([html.Small("Not placed yet: ", className="text-muted"), *unplaced], className="mt-2"))
    return html.Div(children, className="machine-drawing", **{"aria-label": "Where each channel's sensor sits"})


def register(app, get_connection, data_folder, get_storage):
    """data_folder may be a path or a function returning it (tests point it at a temporary folder)."""
    def folder():
        return data_folder() if callable(data_folder) else data_folder

    def target_of(trigger):
        return (trigger["gid"], trigger["ch"]) if isinstance(trigger, dict) else (None, None)

    @app.callback([Output("ai-setup-modal", "is_open"), Output("ai-setup-target", "data"),
                   Output("ai-setup-title", "children"), Output("ai-setup-status", "children"),
                   Output("aim-ai_on", "value"), Output("aim-bearing-list", "children"), Output("aim-speed-hint", "children"),
                   Output("aim-save", "disabled")] + [Output("aim-" + key, "value") for key in FIELDS],
                  Input({"type": "ai-setup-open", "gid": ALL, "ch": ALL}, "n_clicks"), prevent_initial_call=True)
    def ai_setup_open(clicks):
        if not any(clicks or []) or not historian.edge_ai_enabled():
            return [no_update] * (8 + len(FIELDS))
        gid, ch = target_of(ctx.triggered_id)
        rpm = live_rpm(get_storage(), gid, ch)
        with closing(get_connection()) as conn:
            settings = historian.ai_channel_settings(conn, gid, ch)
            gateway_on = historian.ai_enabled(conn, gid)
            names = ai_views.designations(ai_views.library(conn))
        missing = historian.ai_setup_missing(settings, rpm)
        status = (dbc.Alert([html.Strong("The AI cannot start yet. It needs:"),
                             html.Ul([html.Li(item) for item in missing], className="mb-0")], color="warning")
                  if missing else dbc.Alert([html.I(className="fas fa-check-circle me-2"),
                                             "Setup complete: the AI can learn this sensor."], color="success"))
        hint = (html.Span([html.I(className="fas fa-check text-success me-1"), f"The VDS reports {rpm:.0f} RPM now."])
                if rpm else html.Span([html.I(className="fas fa-exclamation-triangle text-warning me-1"),
                                       "The VDS reports no speed. Choose a fixed speed and enter the RPM."]))
        values = [settings.get(key) for key in FIELDS]
        values[FIELDS.index("speed_source")] = settings.get("speed_source") or ("vds" if rpm else "fixed")
        values[FIELDS.index("line_frequency_hz")] = settings.get("line_frequency_hz") or 50
        values[FIELDS.index("end")] = settings.get("end") or None
        return [True, {"gid": gid, "ch": ch}, f"AI setup · {gid} CH{ch}", status,
                bool(gateway_on and settings.get("ai_on", True)), [html.Option(value=n) for n in names], hint,
                not ai_views.is_admin()] + values

    @app.callback([Output("aim-gear-wrap", "style"), Output("aim-blades-wrap", "style")],
                  Input("aim-machine_type", "value"))
    def ai_setup_fields(part):
        """Gear teeth only for a gearbox, blades or vanes only for a pump or fan."""
        hidden = {"display": "none"}
        return ({} if part in historian.GEAR_PARTS else hidden), ({} if part in historian.BLADE_PARTS else hidden)

    @app.callback([Output("aim-ai_on", "disabled"), Output("aim-ai_on", "value", allow_duplicate=True),
                   Output("aim-ai-hint", "children")],
                  [Input("aim-machine_type", "value"), Input("aim-power_kw", "value"), Input("aim-foundation", "value"),
                   Input("aim-speed_source", "value"), Input("aim-fixed_rpm", "value")],
                  State("ai-setup-target", "data"), prevent_initial_call=True)
    def ai_setup_ready(part, power, foundation, speed_source, fixed_rpm, target):
        """The AI switch unlocks only when everything the AI needs is filled in."""
        rpm = live_rpm(get_storage(), target["gid"], target["ch"]) if target else None
        missing = setup_missing({"machine_type": part, "power_kw": power, "foundation": foundation,
                                 "speed_source": speed_source, "fixed_rpm": fixed_rpm}, rpm)
        if missing:
            return True, False, html.Span([html.I(className="fas fa-lock me-1"),
                                           "To turn the AI on, enter " + ", ".join(missing) + "."], className="text-warning")
        return False, no_update, html.Span([html.I(className="fas fa-unlock me-1"),
                                            "Everything the AI needs is filled in."], className="text-success")

    @app.callback(Output("aim-drawing", "children"),
                  [Input("aim-machine", "value"), Input("aim-machine_type", "value"), Input("aim-end", "value"),
                   Input("aim-direction", "value")], State("ai-setup-target", "data"))
    def ai_setup_drawing(machine, part, end, direction, target):
        if not target:
            return None
        with closing(get_connection()) as conn:
            rows = {(g, c): json.loads(v or "{}") for g, c, v in conn.execute(
                "SELECT gateway_id, channel, settings FROM ai_settings WHERE channel != ''")}
        return machine_drawing(rows, (target["gid"], target["ch"]),
                               {"machine": machine, "machine_type": part, "end": end, "direction": direction})

    @app.callback(Output("aim-iso-preview", "children"),
                  [Input("aim-machine_type", "value"), Input("aim-power_kw", "value"), Input("aim-foundation", "value")])
    def ai_setup_iso(machine_type, power_kw, foundation):
        return iso_preview(machine_type, power_kw, foundation)

    @app.callback(Output("ai-setup-modal", "is_open", allow_duplicate=True), Input("aim-close", "n_clicks"),
                  prevent_initial_call=True)
    def ai_setup_close(_n):
        return False

    @app.callback([Output("aim-result", "children"), Output("ai-setup-status", "children", allow_duplicate=True)],
                  Input("aim-save", "n_clicks"),
                  [State("ai-setup-target", "data"), State("aim-ai_on", "value")] + [State("aim-" + k, "value") for k in FIELDS],
                  prevent_initial_call=True)
    def ai_setup_save(_n, target, ai_on, *values):
        if not historian.edge_ai_enabled():
            return dbc.Alert(ai_views.NOT_IRIV, color="danger"), no_update
        if not target:
            return no_update, no_update
        form = dict(zip(FIELDS, values, strict=True), ai_on=bool(ai_on))
        missing_inputs = setup_missing(form, live_rpm(get_storage(), target["gid"], target["ch"]))
        if missing_inputs:
            form["ai_on"] = ai_on = False  # the switch stays off until the AI has what it needs
        try:
            with closing(get_connection()) as conn, conn:
                # A class chosen earlier (asset registry, machines.json) stays until power and foundation are given
                form["iso_class"] = historian.ai_channel_settings(conn, target["gid"], target["ch"]).get("iso_class", "")
                saved = historian.save_ai_channel(conn, target["gid"], target["ch"], form)
                if ai_on:
                    historian.save_ai_gateway(conn, target["gid"], True)
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger"), no_update
        missing = historian.ai_setup_missing(saved, live_rpm(get_storage(), target["gid"], target["ch"]))
        if missing or missing_inputs:
            return (dbc.Alert("Saved. The AI stays off until you enter " + ", ".join(missing_inputs or missing) + ".",
                              color="warning"), no_update)
        if not ai_on:
            return dbc.Alert("Saved. The AI is off for this channel.", color="secondary"), no_update
        return (dbc.Alert(f"Saved. The AI learns {target['gid']} CH{target['ch']} as a "
                          f"{ai_views.LABELS.get(saved['machine_type'], 'machine').lower()} "
                          f"({ISO_NAMES.get(saved['iso_class'], saved['iso_class'])}). Learning takes about a day "
                          "of normal running.", color="success"),
                dbc.Alert([html.I(className="fas fa-check-circle me-2"), "Setup complete: the AI can learn this sensor."],
                          color="success"))

    @app.callback(Output("aim-result", "children", allow_duplicate=True), Input("aim-relearn", "n_clicks"),
                  State("ai-setup-target", "data"), prevent_initial_call=True)
    def ai_setup_relearn(_n, target):
        if not historian.edge_ai_enabled() or not target:
            return no_update
        with closing(get_connection()) as conn, conn:
            historian.request_ai_relearn(conn, target["gid"], target["ch"])
        return dbc.Alert("The AI forgets this sensor's old normal and learns again on its next scan.", color="info")

    @app.callback([Output("ai-explain-modal", "is_open"), Output("ai-explain-title", "children"),
                   Output("ai-explain-body", "children"), Output("ai-explain-result", "children")],
                  Input({"type": "ai-bell", "gid": ALL, "ch": ALL}, "n_clicks"), prevent_initial_call=True)
    def ai_explain_open(clicks):
        if not any(clicks or []) or not historian.edge_ai_enabled():
            return no_update, no_update, no_update, no_update
        gid, ch = target_of(ctx.triggered_id)
        with closing(get_connection()) as conn:
            settings = historian.ai_channel_settings(conn, gid, ch)
            body = explanation(folder(), conn, gid, ch, ai_views.is_admin())
        name = " / ".join(p for p in (settings.get("machine"), settings.get("location")) if p)
        title = [html.I(className="fas fa-bell text-warning me-2"), f"{gid} CH{ch}",
                 html.Span(f"  {name}", className="text-muted fw-normal")]
        return True, title, body, None

    @app.callback(Output("ai-explain-modal", "is_open", allow_duplicate=True), Input("ai-explain-close", "n_clicks"),
                  prevent_initial_call=True)
    def ai_explain_close(_n):
        return False

    @app.callback(Output("ai-explain-result", "children", allow_duplicate=True),
                  Input({"type": "ai-explain-feedback", "verdict": ALL, "key": ALL}, "n_clicks"), prevent_initial_call=True)
    def ai_explain_feedback(clicks):
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(clicks or []) or not historian.edge_ai_enabled():
            return no_update
        gid, ch, kind, since_ms, fault_id = json.loads(trigger["key"])
        user = request.authorization.username if request.authorization else "local-admin"
        try:
            with closing(get_connection()) as conn, conn:
                historian.save_ai_feedback(conn, gid, ch, kind, since_ms, fault_id, trigger["verdict"], user)
        except (ValueError, TypeError) as error:
            return dbc.Alert(str(error), color="danger")
        text = {"confirmed": "Thank you. The AI keeps this pattern to recognise the fault again.",
                "wrong": "Thank you. The AI learns this as normal running and closes the finding.",
                "repaired": "Thank you. The AI learns the machine's new normal after the repair."}[trigger["verdict"]]
        return dbc.Alert(text + " It applies this on its next scan.", color="success")
