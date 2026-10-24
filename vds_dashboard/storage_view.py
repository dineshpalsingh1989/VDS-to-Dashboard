"""Settings > Database & files: where everything is stored, how big it is, and how long it is kept.

Read-only (viewers may look). It shows the SQLite database and its tables, the daily backups and
reports, and on IRIV Edge AI where the AI keeps its learning file, reports, evidence and neural model.
"""

import json
import os
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import dash_bootstrap_components as dbc
from dash import html

from . import historian

REPO = Path(__file__).resolve().parent.parent
TABLES = (("trends", "Overall readings (one row per reading)"), ("trends_latest", "Latest reading per channel"),
          ("snapshots", "Saved spectra and waveforms for the history (every 30 min)"),
          ("raw_spectrum", "Recorded spectra (Data storage recordings)"),
          ("raw_waveforms", "Recorded waveforms (Data storage recordings)"),
          ("learning_samples", "AI learning samples (IRIV Edge AI)"), ("alarm_events", "Alarm history"),
          ("devices", "VDS130 units and their approval"), ("ai_settings", "AI setup per channel"),
          ("asset_settings", "Measurement points"))


def size(path):
    try:
        value = Path(path).stat().st_size
    except OSError:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None


def folder_size(path):
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                continue
    return total, count


def human(total):
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return ""


def modified(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "—"


def approx_rows(conn, table):
    """Rows, cheaply: rowid range rather than counting millions of rows on every page view."""
    try:
        low, high = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
    except Exception:  # noqa: BLE001  table not created on this install
        return None
    return 0 if low is None else high - low + 1


def card(title, icon, body):
    return dbc.Card([dbc.CardHeader([html.I(className=f"fas {icon} me-2"), title]), dbc.CardBody(body)],
                    className="mb-3 storage-card")


def facts(rows):
    return html.Table([html.Tbody([html.Tr([html.Th(label), html.Td(value)]) for label, value in rows])],
                      className="table table-sm mb-0 storage-facts")


def retention(name, default="0"):
    days = int(os.environ.get(name, default) or 0)
    return f"{days} days" if days > 0 else "kept until you delete it"


def page(db_file, data_folder, ai_enabled):
    db_file, data_folder = Path(db_file), Path(data_folder)
    wal = Path(str(db_file) + "-wal")
    with closing(historian_connect(db_file)) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        rows = [(table, text, approx_rows(conn, table)) for table, text in TABLES]
    database = card("Database", "fa-database", [
        facts([("Engine", f"SQLite, journal mode {journal.upper()} (crash-safe, one file, no server to run)"),
               ("File", html.Code(str(db_file))), ("Size", size(db_file) or "—"),
               ("Write-ahead log", f"{size(wal) or '0 B'} (folded into the file automatically)"),
               ("Last change", modified(db_file))]),
        html.Table([html.Thead(html.Tr([html.Th("Table"), html.Th("Holds"), html.Th("Rows (about)")])),
                    html.Tbody([html.Tr([html.Td(html.Code(t)), html.Td(text),
                                         html.Td("—" if n is None else f"{n:,}")]) for t, text, n in rows])],
                   className="table table-sm mt-3 mb-0"),
    ])
    keep = card("How long data is kept", "fa-history", facts([
        ("Overall readings", retention("VDS_TREND_RETENTION_DAYS")),
        ("Recordings", retention("VDS_RAW_RETENTION_DAYS")),
        ("Alarm history", retention("VDS_ALARM_RETENTION_DAYS")),
        ("History spectra and waveforms", (f"one per channel every {historian.snapshot_interval() // 60} min, "
                                           f"kept {historian.snapshot_retention_days()} days")),
        ("Daily backup and report", "once a day, in the folders below"),
    ]))
    files = []
    for label, folder, note in (("Backups", data_folder / "backups", "one copy of the database per day"),
                                ("Daily reports", data_folder / "reports", "Markdown summary of each day")):
        total, count = folder_size(folder)
        files.append((label, html.Span([html.Code(str(folder)), f"  {count} files, {human(total)} · {note}"])))
    folders = card("Folders", "fa-folder-open", facts([("Data folder", html.Code(str(data_folder)))] + files))
    cards = [database, keep, folders]
    if ai_enabled:
        cards.append(ai_card(data_folder))
    return html.Div([html.P("Where the dashboard and the AI keep their data, and how much there is. Change how long "
                            "data is kept with the retention settings in .env (see README).",
                            className="text-muted"), *cards])


def historian_connect(db_file):
    import sqlite3
    return sqlite3.connect(str(db_file), timeout=10)


def ai_card(data_folder):
    ai = data_folder / "ai"
    try:
        report = json.loads((ai / "findings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    storage = report.get("storage") or {}
    root = Path(storage.get("path") or ai)
    learning = root / "learning.db"
    reports_total, reports_count = folder_size(root / "reports")
    evidence_total, evidence_count = folder_size(ai / "evidence")
    models = REPO / "iriv-edge" / "models"
    model_files = sorted(models.glob("vibration_brain*")) if models.exists() else []
    age = time.time() - float(report.get("generated_at") or 0)
    return card("AI files (IRIV Edge AI)", "fa-brain", facts([
        ("Where the AI keeps its data", html.Span([html.Code(str(root)), "  ", storage.get("message", "")])),
        ("Learning file", html.Span([html.Code(str(learning)), f"  {size(learning) or 'not created yet'}"])),
        ("Report folders", f"{reports_count} files, {human(reports_total)} (one folder per day and per finding)"),
        ("Pictures for explanations", f"{evidence_count} files, {human(evidence_total)} in {ai / 'evidence'}"),
        ("Findings for the dashboard", html.Span([html.Code(str(ai / "findings.json")),
                                                  f"  updated {age / 60:.0f} min ago" if report else "  not written yet"])),
        ("Neural model", html.Span([report.get("neural") or "Not reported yet.", html.Br(),
                                    *[html.Span([html.Code(p.name), f" {size(p)}  "]) for p in model_files]])),
    ]))
