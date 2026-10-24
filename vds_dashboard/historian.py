"""Durable VDS context, asset configuration, alarms, and maintenance."""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from .fault_frequencies import bearing_orders
from .vds import advance_alarm, freshness, signature, validate_alarm


def setup_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channel_state (
            gateway_id TEXT, channel TEXT, state TEXT NOT NULL,
            PRIMARY KEY(gateway_id, channel));
        CREATE TABLE IF NOT EXISTS asset_settings (
            gateway_id TEXT, channel TEXT, settings TEXT NOT NULL,
            PRIMARY KEY(gateway_id, channel));
        CREATE TABLE IF NOT EXISTS alarm_rules (
            gateway_id TEXT, channel TEXT, rule TEXT NOT NULL,
            PRIMARY KEY(gateway_id, channel));
        CREATE TABLE IF NOT EXISTS alarm_state (
            gateway_id TEXT, channel TEXT, state TEXT NOT NULL,
            PRIMARY KEY(gateway_id, channel));
        CREATE TABLE IF NOT EXISTS alarm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, channel TEXT,
            timestamp_ms INTEGER, severity TEXT, value REAL, context TEXT,
            acknowledged_at REAL, acknowledged_by TEXT);
        CREATE INDEX IF NOT EXISTS idx_alarm_time ON alarm_events(timestamp_ms);
        CREATE TABLE IF NOT EXISTS baselines (
            gateway_id TEXT, channel TEXT, snapshot TEXT NOT NULL,
            PRIMARY KEY(gateway_id, channel));
        CREATE TABLE IF NOT EXISTS maintenance_runs (day TEXT PRIMARY KEY, completed_at REAL);
        CREATE TABLE IF NOT EXISTS learning_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT NOT NULL, channel TEXT NOT NULL,
            kind TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
            rpm REAL, data BLOB NOT NULL, sampling_freq REAL, bandwidth REAL, lines INTEGER,
            context TEXT, UNIQUE (gateway_id, channel, kind, timestamp_ms));
        CREATE INDEX IF NOT EXISTS idx_learning_recent
            ON learning_samples (gateway_id, channel, kind, received_ms);
        CREATE INDEX IF NOT EXISTS idx_learning_received ON learning_samples (received_ms);
        CREATE TABLE IF NOT EXISTS ai_settings (
            gateway_id TEXT, channel TEXT, settings TEXT NOT NULL,
            PRIMARY KEY (gateway_id, channel));
        CREATE TABLE IF NOT EXISTS ai_bearings (designation TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT NOT NULL, channel TEXT NOT NULL,
            kind TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
            rpm REAL, data BLOB NOT NULL, sampling_freq REAL, bandwidth REAL, lines INTEGER,
            context TEXT, UNIQUE (gateway_id, channel, kind, timestamp_ms));
        CREATE INDEX IF NOT EXISTS idx_snapshots_recent ON snapshots (gateway_id, channel, kind, received_ms);
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT NOT NULL, channel TEXT NOT NULL,
            kind TEXT NOT NULL, since_ms INTEGER, fault_id TEXT, verdict TEXT NOT NULL,
            user TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS devices (
            gateway_id TEXT PRIMARY KEY, status TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            info TEXT NOT NULL DEFAULT '{}');
    """)
    # VDS units that already sent data before approval existed keep running: adopt them as allowed.
    conn.execute("INSERT OR IGNORE INTO devices (gateway_id, status, first_seen, last_seen, info) "
                 "SELECT DISTINCT gateway_id, 'allowed', strftime('%s','now'), strftime('%s','now'), '{}' "
                 "FROM channel_state")
    for table in ("trends", "trends_latest", "raw_waveforms", "raw_spectrum"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "context" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN context TEXT")


def get_json(conn, table, column, gid, channel, default=None):
    # Table and column are internal constants, never request parameters.
    row = conn.execute(
        f"SELECT {column} FROM {table} WHERE gateway_id=? AND channel=?", (gid, channel)
    ).fetchone()
    return json.loads(row[0]) if row else (default if default is not None else {})


def asset_settings(conn, gid, channel):
    return get_json(
        conn,
        "asset_settings",
        "settings",
        gid,
        channel,
        {
            "site": "",
            "machine": "",
            "location": "",
            "direction": "",
            "publish_interval": 60,
        },
    )


def save_asset(conn, gid, channel, settings):
    interval = int(settings.get("publish_interval", 60))
    if not 60 <= interval <= 86400:
        raise ValueError("Publishing interval must be between 60 and 86400 seconds")
    result = {
        key: str(settings.get(key) or "").strip()[:128]
        for key in ("site", "machine", "location", "direction")
    }
    result["publish_interval"] = interval
    conn.execute(
        "INSERT OR REPLACE INTO asset_settings VALUES (?,?,?)",
        (gid, channel, json.dumps(result)),
    )


def save_rule(conn, gid, channel, rule):
    if rule.get("enabled"):
        rule = validate_alarm(rule)
    conn.execute(
        "INSERT OR REPLACE INTO alarm_rules VALUES (?,?,?)",
        (gid, channel, json.dumps(rule)),
    )
    conn.execute(
        "DELETE FROM alarm_state WHERE gateway_id=? AND channel=?", (gid, channel)
    )


AI_MACHINE_TYPES = ("motor", "pump", "fan", "gearbox", "compressor", "other")
AI_DIRECTIONS = ("radial", "horizontal", "vertical", "axial")
AI_ISO_CLASSES = ("group1_rigid", "group1_flexible", "group2_rigid", "group2_flexible", "class1_small")
AI_FOUNDATIONS = ("rigid", "flexible")
AI_ENDS = ("DE", "NDE")  # drive end (coupling side) or non-drive end of the part the sensor is on
GEAR_PARTS, BLADE_PARTS = ("gearbox",), ("pump", "fan")


def iso_class_for(power_kw, foundation):
    """ISO vibration class from machine power and foundation.

    Up to 15 kW: ISO 10816-1 class I. 15-300 kW: ISO 20816-3 group 2. Above 300 kW: group 1.
    """
    if power_kw in (None, "") or foundation not in AI_FOUNDATIONS:
        return ""
    power_kw = float(power_kw)
    if power_kw < 15:
        return "class1_small"
    return f"group{2 if power_kw <= 300 else 1}_{foundation}"


def ai_setup_missing(settings, rpm=None):
    """What the AI still needs before it may learn a channel; empty when the setup is complete.

    The AI needs the machine type, its ISO class (from power and foundation) and a running
    speed: the VDS speed sensor must report one, or the user enters a fixed RPM.
    """
    settings = settings or {}
    missing = []
    if not settings.get("machine_type"):
        missing.append("machine type")
    if not settings.get("iso_class"):
        missing.append("machine power and foundation (for the ISO class)")
    fixed = settings.get("fixed_rpm")
    if settings.get("speed_source") == "fixed":
        if not fixed:
            missing.append("fixed speed in RPM")
    elif not (rpm and float(rpm) > 0) and not fixed:
        missing.append("running speed: the VDS reports none, so enter a fixed RPM")
    return missing


DEVICE_STATUSES = ("pending", "allowed", "blocked")


def approval_required():
    """A new VDS waits on the VDS130 page until a user allows it (VDS_DEVICE_APPROVAL=0 turns this off)."""
    return os.environ.get("VDS_DEVICE_APPROVAL", "1").strip().lower() not in ("0", "false", "no")


def device_status(conn, gid):
    row = conn.execute("SELECT status FROM devices WHERE gateway_id=?", (gid,)).fetchone()
    return row[0] if row else None


def note_pending_device(conn, gid, packet, now=None):
    """Remember a VDS that is not allowed yet, with what it sent, so the user can decide."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT info FROM devices WHERE gateway_id=?", (gid,)).fetchone()
    info = json.loads(row[0]) if row else {}
    channels = set(info.get("channels", [])) | {str(packet.get("channel"))}
    info.update(channels=sorted(channels), sensor_type=packet.get("sensor_type") or info.get("sensor_type"),
                rpm=packet.get("rpm") if packet.get("rpm") is not None else info.get("rpm"),
                messages=int(info.get("messages", 0)) + 1)
    if row:
        conn.execute("UPDATE devices SET last_seen=?, info=? WHERE gateway_id=?", (now, json.dumps(info), gid))
    else:
        conn.execute("INSERT INTO devices (gateway_id, status, first_seen, last_seen, info) VALUES (?,?,?,?,?)",
                     (gid, "pending", now, now, json.dumps(info)))


def set_device_status(conn, gid, status, now=None):
    """Allow or block a VDS. Allowing an unknown serial number pre-approves it."""
    gid = str(gid or "").strip()
    if not gid or len(gid) > 64:
        raise ValueError("Unknown VDS")
    if status not in ("allowed", "blocked"):
        raise ValueError("A VDS can be allowed or blocked")
    now = time.time() if now is None else now
    conn.execute("INSERT INTO devices (gateway_id, status, first_seen, last_seen, info) VALUES (?,?,?,?, '{}') "
                 "ON CONFLICT(gateway_id) DO UPDATE SET status=excluded.status", (gid, status, now, now))


def devices(conn):
    return [{"gateway_id": g, "status": s, "first_seen": f, "last_seen": seen, "info": json.loads(i or "{}")}
            for g, s, f, seen, i in conn.execute(
                "SELECT gateway_id, status, first_seen, last_seen, info FROM devices ORDER BY status, gateway_id")]


def edge_ai_enabled():
    """AI features exist only on IRIV Edge AI installs; iriv-edge/install.sh sets VDS_EDGE_AI=1."""
    return os.environ.get("VDS_EDGE_AI", "").strip().lower() in ("1", "true", "yes")


def ai_enabled(conn, gid):
    """The per-VDS AI switch. Off until a user turns it on in the asset registry."""
    return bool(get_json(conn, "ai_settings", "settings", gid, "", {}).get("enabled"))


def save_ai_gateway(conn, gid, enabled):
    gid = str(gid or "").strip()
    if not gid or len(gid) > 64:
        raise ValueError("Unknown VDS")
    conn.execute(
        "INSERT OR REPLACE INTO ai_settings VALUES (?,?,?)",
        (gid, "", json.dumps({"enabled": bool(enabled)})),
    )


def ai_channel_settings(conn, gid, channel):
    return get_json(conn, "ai_settings", "settings", gid, channel, {})


def _optional_int(value, low, high, label):
    if value in (None, ""):
        return None
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return number


def save_ai_channel(conn, gid, channel, form):
    """Validate and store what the user told the AI about one sensor."""
    if channel not in ("1", "2", "3", "4") or not gid:
        raise ValueError("Invalid channel")
    text = {key: str(form.get(key) or "").strip()[:64] for key in ("machine", "location", "bearing")}
    choices = (("machine_type", AI_MACHINE_TYPES), ("direction", AI_DIRECTIONS), ("iso_class", AI_ISO_CLASSES))
    for key, allowed in choices:
        value = form.get(key) or ""
        if value and value not in allowed:
            raise ValueError(f"Unknown {key.replace('_', ' ')}")
        text[key] = value
    speed_source = form.get("speed_source") or "vds"
    if speed_source not in ("vds", "fixed"):
        raise ValueError("Speed source must be the VDS speed sensor or a fixed speed")
    fixed_rpm = form.get("fixed_rpm")
    fixed_rpm = None if fixed_rpm in (None, "") else float(fixed_rpm)
    if fixed_rpm is not None and not 1 <= fixed_rpm <= 60000:
        raise ValueError("Fixed speed must be between 1 and 60000 RPM")
    if speed_source == "fixed" and fixed_rpm is None:
        raise ValueError("Enter the machine speed in RPM, or choose the VDS speed sensor")
    line = int(form.get("line_frequency_hz") or 50)
    if line not in (50, 60):
        raise ValueError("Line frequency must be 50 or 60 Hz")
    previous = ai_channel_settings(conn, gid, channel)
    keep = lambda key, default=None: form[key] if key in form else previous.get(key, default)
    end = keep("end") or ""  # the asset registry form has no end; keep the one set on the channel card
    if end and end not in AI_ENDS:
        raise ValueError("End must be DE (drive end) or NDE (non-drive end)")
    text["end"] = end
    power_kw = keep("power_kw")
    power_kw = None if power_kw in (None, "") else float(power_kw)
    if power_kw is not None and not 0.05 <= power_kw <= 100000:
        raise ValueError("Machine power must be between 0.05 and 100000 kW")
    foundation = keep("foundation") or ""
    if foundation and foundation not in AI_FOUNDATIONS:
        raise ValueError("Foundation must be rigid or flexible")
    if power_kw is not None and foundation and ("power_kw" in form or "foundation" in form or not text["iso_class"]):
        text["iso_class"] = iso_class_for(power_kw, foundation)
    part = text["machine_type"]
    gear_teeth = _optional_int(form.get("gear_teeth"), 1, 1000, "Gear teeth")
    blades = _optional_int(form.get("blades"), 1, 100, "Blades or vanes")
    if part and part not in GEAR_PARTS:
        gear_teeth = None  # only a gearbox has a gear mesh
    if part and part not in BLADE_PARTS:
        blades = None  # only a pump or fan has a vane or blade pass
    settings = dict(
        text, speed_source=speed_source, fixed_rpm=fixed_rpm, line_frequency_hz=line, power_kw=power_kw,
        foundation=foundation, ai_on=bool(keep("ai_on", True)),
        gear_teeth=gear_teeth, blades=blades,
    )
    if previous.get("relearn_at"):
        settings["relearn_at"] = previous["relearn_at"]
    conn.execute("INSERT OR REPLACE INTO ai_settings VALUES (?,?,?)", (gid, channel, json.dumps(settings)))
    return settings


def save_channel_bearing(conn, gid, channel, bearing):
    """The bearing fitted at a measurement point, for spectrum fault lines (all installs)."""
    if channel not in ("1", "2", "3", "4") or not gid:
        raise ValueError("Invalid channel")
    bearing = str(bearing or "").strip()[:64]
    settings = ai_channel_settings(conn, gid, channel)
    settings["bearing"] = bearing
    conn.execute("INSERT OR REPLACE INTO ai_settings VALUES (?,?,?)", (gid, channel, json.dumps(settings)))
    return bearing


def request_ai_relearn(conn, gid, channel, now=None):
    """Ask the advisor to forget a channel, for example after a repair."""
    settings = ai_channel_settings(conn, gid, channel)
    settings["relearn_at"] = time.time() if now is None else now
    conn.execute("INSERT OR REPLACE INTO ai_settings VALUES (?,?,?)", (gid, channel, json.dumps(settings)))


AI_VERDICTS = ("confirmed", "wrong", "repaired")


def _number(form, key, low, high, label):
    try:
        value = float(form.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"Enter {label}") from None
    if not low <= value <= high:
        raise ValueError(f"{label.capitalize()} must be between {low:g} and {high:g}")
    return value


def save_ai_bearing(conn, designation, form):
    """Add a bearing from its catalogue geometry or its published defect frequencies."""
    designation = str(designation or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 ./-]{1,31}", designation):
        raise ValueError("Bearing number: 2-32 letters, digits, spaces, dots, slashes or dashes")
    if form.get("mode") == "geometry":
        balls = _number(form, "balls", 3, 60, "the number of balls or rollers")
        ball = _number(form, "ball_diameter_mm", 0.5, 200, "the ball diameter in mm")
        pitch = _number(form, "pitch_diameter_mm", 2, 2000, "the pitch diameter in mm")
        angle = _number(form, "contact_angle_deg", 0, 45, "the contact angle") if form.get("contact_angle_deg") not in (None, "") else 0.0
        if ball >= pitch:
            raise ValueError("The pitch diameter must be larger than the ball diameter")
        orders = bearing_orders(int(balls), ball, pitch, angle)
        geometry = {"balls": int(balls), "ball_diameter_mm": ball, "pitch_diameter_mm": pitch, "contact_angle_deg": angle}
    else:
        orders = {key: _number(form, key, low, high, key.upper()) for key, low, high in (
            ("bpfo", 0.5, 40), ("bpfi", 0.5, 40), ("bsf", 0.2, 30), ("ftf", 0.1, 0.9))}
        if orders["bpfi"] <= orders["bpfo"]:
            raise ValueError("BPFI is always larger than BPFO; check the values")
        geometry = {}
    data = dict(designation=designation, **{k: round(v, 4) for k, v in orders.items()}, **geometry,
                source=str(form.get("source") or "Added in the dashboard").strip()[:120], added=time.time())
    conn.execute("INSERT OR REPLACE INTO ai_bearings VALUES (?,?)", (designation, json.dumps(data)))
    return data


def ai_bearings(conn):
    return [json.loads(row[0]) for row in conn.execute("SELECT data FROM ai_bearings ORDER BY designation")]


def save_ai_feedback(conn, gid, channel, kind, since_ms, fault_id, verdict, user=""):
    """Record what a person found when checking an AI finding."""
    if verdict not in AI_VERDICTS:
        raise ValueError("Unknown feedback")
    if channel not in ("1", "2", "3", "4") or not gid:
        raise ValueError("Invalid channel")
    if not re.fullmatch(r"spectrum|envelope|waveform|severity|metric:[A-Za-z]{1,40}", str(kind)):
        raise ValueError("Unknown finding type")
    cursor = conn.execute(
        "INSERT INTO ai_feedback (gateway_id,channel,kind,since_ms,fault_id,verdict,user,created) VALUES (?,?,?,?,?,?,?,?)",
        (gid, channel, kind, int(since_ms) if since_ms else None, str(fault_id or "")[:64] or None, verdict,
         str(user or "")[:64], time.time()))
    return cursor.lastrowid


def learning_interval():
    """Seconds between learning samples per channel of an AI-enabled VDS; 0 turns sampling off."""
    try:
        return max(0, int(os.environ.get("VDS_LEARNING_INTERVAL_S", "600")))
    except ValueError:
        return 600


def learning_retention_hours():
    """Hours a learning sample is kept; the advisor reads each one within minutes."""
    try:
        return max(1, int(os.environ.get("VDS_LEARNING_RETENTION_HOURS", "6")))
    except ValueError:
        return 6


def snapshot_interval():
    """Seconds between history snapshots per channel (default 30 minutes); 0 turns them off."""
    try:
        return max(0, int(os.environ.get("VDS_SNAPSHOT_INTERVAL_S", "1800")))
    except ValueError:
        return 1800


def snapshot_retention_days():
    try:
        return max(1, int(os.environ.get("VDS_SNAPSHOT_RETENTION_DAYS", "90")))
    except ValueError:
        return 90


def store_snapshots(conn, packet, context, interval, retention_days=90):
    """Keep a spectrum and a waveform per channel every interval, so any point of the
    history can be opened later. Each channel trims its own snapshots past retention."""
    received_ms = int(packet["received_at"] * 1000)
    for kind, values in (("spectrum", packet["spectrum_data"]), ("waveform", packet["waveform_data"])):
        if values is None or len(values) == 0:
            continue
        last = conn.execute(
            "SELECT MAX(received_ms) FROM snapshots WHERE gateway_id=? AND channel=? AND kind=?",
            (packet["gateway_id"], packet["channel"], kind),
        ).fetchone()[0]
        if last is not None and received_ms - last < interval * 1000:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO snapshots (gateway_id,channel,kind,timestamp_ms,received_ms,rpm,data,"
            "sampling_freq,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (packet["gateway_id"], packet["channel"], kind, packet["timestamp_ms"], received_ms, packet["rpm"],
             np.asarray(values, dtype="<f4").tobytes(), packet["sampling_freq"], packet["bandwidth"],
             packet["lines"], context),
        )
        conn.execute(
            "DELETE FROM snapshots WHERE gateway_id=? AND channel=? AND kind=? AND received_ms < ?",
            (packet["gateway_id"], packet["channel"], kind, received_ms - retention_days * 86_400_000),
        )


def store_learning_samples(conn, packet, context, interval, retention_hours=6):
    """Keep one spectrum and one waveform per channel every interval for the AI advisor.

    Bounded by design: each channel holds at most retention_hours * 3600 / interval
    samples per kind, however many VDS units connect or how often they publish.
    Older rows of the same channel are removed in the same transaction.
    """
    received_ms = int(packet["received_at"] * 1000)
    for kind, values in (("spectrum", packet["spectrum_data"]), ("waveform", packet["waveform_data"])):
        if values is None or len(values) == 0:
            continue
        last = conn.execute(
            "SELECT MAX(received_ms) FROM learning_samples WHERE gateway_id=? AND channel=? AND kind=?",
            (packet["gateway_id"], packet["channel"], kind),
        ).fetchone()[0]
        if last is not None and received_ms - last < interval * 1000:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO learning_samples (gateway_id,channel,kind,timestamp_ms,received_ms,rpm,data,"
            "sampling_freq,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (packet["gateway_id"], packet["channel"], kind, packet["timestamp_ms"], received_ms, packet["rpm"],
             np.asarray(values, dtype="<f4").tobytes(), packet["sampling_freq"], packet["bandwidth"],
             packet["lines"], context),
        )
        conn.execute(
            "DELETE FROM learning_samples WHERE gateway_id=? AND channel=? AND kind=? AND received_ms < ?",
            (packet["gateway_id"], packet["channel"], kind, received_ms - retention_hours * 3_600_000),
        )


def store_packet(conn, packet, recording=False):
    gid, channel, capture = (
        packet["gateway_id"],
        packet["channel"],
        packet["timestamp_ms"],
    )
    state = get_json(conn, "channel_state", "state", gid, channel)
    timestamp = datetime.fromtimestamp(capture / 1000)
    settings = asset_settings(conn, gid, channel)
    metadata = state.get("metadata", {}).copy()
    metrics = state.get("metrics", {}).copy()
    metric_times = state.get("metric_times", {}).copy()
    context = json.dumps(
        {
            key: packet.get(key)
            for key in (
                "metadata",
                "timestamp_ms",
                "received_at",
                "sensor_type",
                "sensitivity",
                "sampling_freq",
                "duration",
                "bandwidth",
                "lines",
                "window",
                "quality_issues",
            )
        }
    )
    accepted_metrics = {}
    for metric, value in packet["metrics"].items():
        metric_context = json.dumps(
            dict(packet["metadata"][metric], timestamp_ms=capture)
        )
        conn.execute(
            "INSERT OR IGNORE INTO trends (timestamp,gateway_id,channel,metric_name,value,rpm,context) VALUES (?,?,?,?,?,?,?)",
            (timestamp, gid, channel, metric, value, packet["rpm"], metric_context),
        )
        if capture > metric_times.get(metric, -1):
            conn.execute(
                """INSERT INTO trends_latest (gateway_id,channel,metric_name,value,rpm,timestamp,context) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(gateway_id,channel,metric_name) DO UPDATE SET value=excluded.value,rpm=excluded.rpm,timestamp=excluded.timestamp,context=excluded.context
                WHERE excluded.timestamp >= trends_latest.timestamp""",
                (gid, channel, metric, value, packet["rpm"], timestamp, metric_context),
            )
            metrics[metric], metadata[metric], metric_times[metric] = (
                value,
                packet["metadata"][metric],
                capture,
            )
            accepted_metrics[metric] = value
    every = snapshot_interval()
    if every and not packet.get("quality_issues"):
        store_snapshots(conn, packet, context, every, snapshot_retention_days())
    interval = learning_interval()
    if interval and edge_ai_enabled() and not packet.get("quality_issues") and ai_enabled(conn, gid):
        store_learning_samples(conn, packet, context, interval, learning_retention_hours())
    # Scalar history is continuous; raw arrays are opt-in recording data.
    if recording and packet["waveform_data"] is not None:
        conn.execute(
            "INSERT OR IGNORE INTO raw_waveforms (gateway_id,channel,timestamp,waveform_data,sampling_freq,context) VALUES (?,?,?,?,?,?)",
            (
                gid,
                channel,
                timestamp,
                np.asarray(packet["waveform_data"], dtype="<f8").tobytes(),
                packet["sampling_freq"],
                context,
            ),
        )
    if recording and packet["spectrum_data"] is not None:
        conn.execute(
            "INSERT OR IGNORE INTO raw_spectrum (gateway_id,channel,timestamp,spectrum_data,bandwidth,lines,context) VALUES (?,?,?,?,?,?,?)",
            (
                gid,
                channel,
                timestamp,
                np.asarray(packet["spectrum_data"], dtype="<f8").tobytes(),
                packet["bandwidth"],
                packet["lines"],
                context,
            ),
        )
    if capture >= state.get("timestamp_ms", -1):
        state.update(
            {
                key: value
                for key, value in packet.items()
                if key not in ("waveform_data", "spectrum_data", "metrics", "metadata")
                and value is not None
            }
        )
    state.update(
        metrics=metrics,
        metadata=metadata,
        metric_times=metric_times,
        publish_interval=settings["publish_interval"],
        asset=settings,
    )
    conn.execute(
        "INSERT OR REPLACE INTO channel_state VALUES (?,?,?)",
        (gid, channel, json.dumps(state)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO config (gateway_id,config_data) VALUES (?,?)",
        (gid, b"{}"),
    )
    rule = get_json(conn, "alarm_rules", "rule", gid, channel)
    previous = get_json(conn, "alarm_state", "state", gid, channel)
    metric = rule.get("metric", "Velocity")
    if rule.get("enabled") and metric in accepted_metrics:
        if freshness(state, packet["received_at"])["status"] != "fresh":
            current = dict(
                severity="unknown",
                candidate="unknown",
                count=0,
                reason="Measurement is stale or its clock is invalid",
            )
        else:
            current = advance_alarm(previous, metrics[metric], metadata[metric], rule)
        if current["severity"] != previous.get("severity", "normal"):
            conn.execute(
                "INSERT INTO alarm_events (gateway_id,channel,timestamp_ms,severity,value,context) VALUES (?,?,?,?,?,?)",
                (
                    gid,
                    channel,
                    capture,
                    current["severity"],
                    metrics[metric],
                    json.dumps(
                        {
                            "metric": metric,
                            "metadata": metadata[metric],
                            "reason": current.get("reason", ""),
                        }
                    ),
                ),
            )
        current["timestamp_ms"] = capture
        conn.execute(
            "INSERT OR REPLACE INTO alarm_state VALUES (?,?,?)",
            (gid, channel, json.dumps(current)),
        )
    state["alarm"] = get_json(conn, "alarm_state", "state", gid, channel)
    state["accepted_metrics"] = accepted_metrics
    return state


def backup_database(source, destination):
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    temporary = destination + ".tmp"
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup failed integrity verification")
    finally:
        target.close()
    os.replace(temporary, destination)


def prune_history(conn, trend_days=0, raw_days=0, alarm_days=0, now=None, learning_hours=0, snapshot_days=0):
    now = now or datetime.now()
    counts = {}
    for table, days in (
        ("trends", trend_days),
        ("raw_waveforms", raw_days),
        ("raw_spectrum", raw_days),
    ):
        if days > 0:
            cutoff = now - timedelta(days=days)
            cursor = conn.execute(
                f"""DELETE FROM {table} WHERE timestamp < ? AND NOT EXISTS (
                SELECT 1 FROM recording_jobs j WHERE j.status='active' AND j.gateway_id={table}.gateway_id
                AND {table}.timestamp BETWEEN j.start_time AND j.end_time)""",
                (cutoff,),
            )
            counts[table] = cursor.rowcount
    if snapshot_days > 0:
        # Catches channels that stopped publishing; active channels trim themselves.
        counts["snapshots"] = conn.execute(
            "DELETE FROM snapshots WHERE received_ms < ?",
            (int((now - timedelta(days=snapshot_days)).timestamp() * 1000),),
        ).rowcount
    if learning_hours > 0:
        # Catches channels that stopped publishing; active channels trim themselves.
        counts["learning_samples"] = conn.execute(
            "DELETE FROM learning_samples WHERE received_ms < ?",
            (int((now - timedelta(hours=learning_hours)).timestamp() * 1000),),
        ).rowcount
    if alarm_days > 0:
        counts["alarm_events"] = conn.execute(
            "DELETE FROM alarm_events WHERE timestamp_ms < ? AND acknowledged_at IS NOT NULL",
            (int((now - timedelta(days=alarm_days)).timestamp() * 1000),),
        ).rowcount
    return counts


def daily_report(conn, day):
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    start_ms, end_ms = (
        int(start.timestamp() * 1000),
        int((start + timedelta(days=1)).timestamp() * 1000),
    )
    lines = [
        f"# VDS maintenance report — {day} (UTC)",
        "",
        "Advisory report; review measurements before planning maintenance.",
        "",
    ]
    local_start = start.astimezone().replace(tzinfo=None)
    local_end = (start + timedelta(days=1)).astimezone().replace(tzinfo=None)
    rows = conn.execute(
        "SELECT gateway_id,channel,metric_name,value,context FROM trends WHERE context IS NOT NULL AND timestamp>=? AND timestamp<?",
        (local_start, local_end),
    ).fetchall()
    groups = {}
    for gid, ch, metric, value, context in rows:
        details = json.loads(context)
        if start_ms <= details.get("timestamp_ms", 0) < end_ms:
            key = (gid, ch, metric, signature(details))
            groups.setdefault(key, []).append(value)
    for (gid, ch, metric, settings), values in sorted(groups.items()):
        asset = asset_settings(conn, gid, ch)
        label = (
            " / ".join(
                asset[key]
                for key in ("site", "machine", "location", "direction")
                if asset[key]
            )
            or f"{gid} CH{ch}"
        )
        meta = json.loads(settings)
        lines.append(
            f"- {label}: {metric}, n={len(values)}, min={min(values):.4g}, max={max(values):.4g}, mean={sum(values) / len(values):.4g}; {meta}"
        )
    if not groups:
        lines.append("No contextual measurements were recorded for this UTC day.")
    events = conn.execute(
        "SELECT gateway_id,channel,severity,value,acknowledged_at FROM alarm_events WHERE timestamp_ms>=? AND timestamp_ms<? ORDER BY timestamp_ms",
        (start_ms, end_ms),
    ).fetchall()
    lines.extend(["", f"Alarm transitions: {len(events)}"])
    lines.extend(
        f"- {gid} CH{ch}: {severity}, value={value}; {'acknowledged' if ack else 'unacknowledged'}"
        for gid, ch, severity, value, ack in events
    )
    return "\n".join(lines) + "\n"


def maintenance_once(conn, data_folder, now=None):
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if conn.execute("SELECT 1 FROM maintenance_runs WHERE day=?", (day,)).fetchone():
        return False
    backup_database(conn, os.path.join(data_folder, "backups", f"vds-{day}.db"))
    folder = os.path.join(data_folder, "reports")
    os.makedirs(folder, exist_ok=True)
    previous = (now - timedelta(days=1)).date().isoformat()
    report = daily_report(conn, previous)
    with open(os.path.join(folder, f"{previous}.md"), "w", encoding="utf-8") as stream:
        stream.write(report)
    prune_history(
        conn,
        int(os.environ.get("VDS_TREND_RETENTION_DAYS", "0")),
        int(os.environ.get("VDS_RAW_RETENTION_DAYS", "0")),
        int(os.environ.get("VDS_ALARM_RETENTION_DAYS", "0")),
        learning_hours=learning_retention_hours(),
        snapshot_days=snapshot_retention_days(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO maintenance_runs VALUES (?,?)", (day, time.time())
    )
    conn.commit()
    return True


def capture_times(conn, gid, channel, start_ms, end_ms):
    """Times (ms) of saved spectra for one channel: history snapshots and recordings."""
    times = {row[0] for row in conn.execute(
        "SELECT timestamp_ms FROM snapshots WHERE gateway_id=? AND channel=? AND kind='spectrum' "
        "AND timestamp_ms BETWEEN ? AND ?", (gid, channel, start_ms, end_ms))}
    for (context,) in conn.execute(
            "SELECT context FROM raw_spectrum WHERE gateway_id=? AND channel=? AND timestamp BETWEEN ? AND ?",
            (gid, channel, datetime.fromtimestamp(start_ms / 1000), datetime.fromtimestamp(end_ms / 1000))):
        try:
            times.add(int(json.loads(context or "{}")["timestamp_ms"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(times)


def nearest_capture(conn, gid, channel, kind, at_ms=None):
    """The saved spectrum or waveform closest to at_ms (latest when None), from snapshots or recordings.

    Returns a dict with timestamp_ms, values (numpy), rpm, sampling_freq, bandwidth, lines and
    context, or None when nothing is saved for this channel.
    """
    candidates = []
    order = "DESC" if at_ms is None else None
    queries = ([("<=", "DESC"), (">=", "ASC")] if at_ms is not None else [(None, order)])
    for comparison, direction in queries:
        where = "" if comparison is None else f" AND timestamp_ms {comparison} ?"
        args = (gid, channel, kind) + (() if comparison is None else (at_ms,))
        row = conn.execute(
            "SELECT timestamp_ms, rpm, data, sampling_freq, bandwidth, lines, context FROM snapshots "
            f"WHERE gateway_id=? AND channel=? AND kind=?{where} ORDER BY timestamp_ms {direction} LIMIT 1",
            args).fetchone()
        if row:
            candidates.append(dict(timestamp_ms=row[0], rpm=row[1], values=np.frombuffer(row[2], "<f4").astype(float),
                                   sampling_freq=row[3], bandwidth=row[4], lines=row[5],
                                   context=json.loads(row[6] or "{}"), source="history snapshot"))
    table, column, extra = (("raw_spectrum", "spectrum_data", "bandwidth, lines") if kind == "spectrum"
                            else ("raw_waveforms", "waveform_data", "sampling_freq"))
    stamp = None if at_ms is None else datetime.fromtimestamp(at_ms / 1000)
    for comparison, direction in queries:
        where = "" if comparison is None else f" AND timestamp {comparison} ?"
        args = (gid, channel) + (() if comparison is None else (stamp,))
        row = conn.execute(f"SELECT timestamp, {column}, {extra}, context FROM {table} WHERE gateway_id=? "
                           f"AND channel=?{where} ORDER BY timestamp {direction} LIMIT 1", args).fetchone()
        if not row:
            continue
        context = json.loads(row[-1] or "{}")
        try:
            ms = int(context.get("timestamp_ms") or datetime.fromisoformat(str(row[0])).timestamp() * 1000)
        except ValueError:
            continue
        item = dict(timestamp_ms=ms, rpm=None, values=np.frombuffer(row[1], "<f8").astype(float), context=context,
                    source="recording")
        if kind == "spectrum":
            item.update(bandwidth=row[2], lines=row[3], sampling_freq=context.get("sampling_freq"))
        else:
            item.update(sampling_freq=row[2], bandwidth=None, lines=None)
        candidates.append(item)
    if not candidates:
        return None
    if at_ms is None:
        return max(candidates, key=lambda c: c["timestamp_ms"])
    return min(candidates, key=lambda c: abs(c["timestamp_ms"] - at_ms))


def recent_spectra(conn, gid, channel, before_ms, limit=30):
    """Up to limit saved spectra up to before_ms, oldest first, for a waterfall."""
    rows = conn.execute(
        "SELECT timestamp_ms, data, bandwidth, lines, context FROM snapshots WHERE gateway_id=? AND channel=? "
        "AND kind='spectrum' AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT ?",
        (gid, channel, before_ms, limit)).fetchall()
    found = {}
    for ms, data, bandwidth, lines, context in rows:
        found[ms] = (np.frombuffer(data, "<f4"), bandwidth, lines, context)
    for stamp, data, bandwidth, lines, context in conn.execute(
            "SELECT timestamp, spectrum_data, bandwidth, lines, context FROM raw_spectrum WHERE gateway_id=? "
            "AND channel=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT ?",
            (gid, channel, datetime.fromtimestamp(before_ms / 1000), limit)):
        try:
            ms = int(json.loads(context or "{}").get("timestamp_ms")
                     or datetime.fromisoformat(str(stamp)).timestamp() * 1000)
        except ValueError:
            continue
        found.setdefault(ms, (np.frombuffer(data, "<f8"), bandwidth, lines, context))
    items = []
    for ms in sorted(found)[-limit:]:
        data, bandwidth, lines, context = found[ms]
        items.append(dict(json.loads(context or "{}"), spectrum_data=data.astype(float).tolist(),
                          bandwidth=bandwidth, lines=lines, timestamp_ms=ms,
                          label=datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")))
    return items
