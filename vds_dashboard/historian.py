"""Durable VDS context, asset configuration, alarms, and maintenance."""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import numpy as np

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
    """)
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


def learning_interval():
    """Seconds between learning samples per channel; 0 (default) turns sampling off."""
    try:
        return max(0, int(os.environ.get("VDS_LEARNING_INTERVAL_S", "0")))
    except ValueError:
        return 0


def learning_retention_hours():
    """Hours a learning sample is kept; the advisor reads each one within minutes."""
    try:
        return max(1, int(os.environ.get("VDS_LEARNING_RETENTION_HOURS", "6")))
    except ValueError:
        return 6


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
    interval = learning_interval()
    if interval and not packet.get("quality_issues"):
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


def prune_history(conn, trend_days=0, raw_days=0, alarm_days=0, now=None, learning_hours=0):
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
    )
    conn.execute(
        "INSERT OR REPLACE INTO maintenance_runs VALUES (?,?)", (day, time.time())
    )
    conn.commit()
    return True
