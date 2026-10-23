#!/usr/bin/env python3
"""Advisory vibration anomaly scoring for the Cytron IRIV Edge AI (CM5 + AI HAT+).

The advisor opens the dashboard historian read-only, compares each channel's
latest readings with that channel's own recent history, and writes findings to
``<data-dir>/ai/findings.json``. The dashboard shows them in its health panel.

It is advisory only: it never writes to the historian, never publishes MQTT,
and never drives Modbus, OPC UA, digital outputs, relays, or any machine command.

Scoring backends:
* cpu   - robust statistics (median / MAD). Needs only NumPy. Always available.
* hailo - optional spectrum autoencoder compiled to a Hailo .hef model and run
          on the AI HAT+ (Hailo-8L 13 TOPS or Hailo-8 26 TOPS).
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

FEATURE_BINS = 256
MIN_METRIC_SAMPLES = 20
MIN_SPECTRA = 10
INFO_Z = 3.5
WARNING_Z = 6.0
MIN_SPECTRAL_DB = 6.0
RPM_TOLERANCE = 0.10
MAX_FINDINGS = 20
SPECTRUM_KEYS = ("sensor_type", "sensitivity", "sampling_freq", "window")

log = logging.getLogger("vds-ai-advisor")


# ---------------------------------------------------------------- database
def connect_readonly(db_path):
    """Open the historian so that any write attempt fails."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_time(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def db_time(value):
    # Matches the format the dashboard's sqlite3 datetime adapter writes.
    return value.isoformat(sep=" ")


def metric_signature(context):
    try:
        data = json.loads(context or "{}")
    except ValueError:
        return None
    data.pop("timestamp_ms", None)
    return json.dumps(data, sort_keys=True)


def spectrum_signature(context, bandwidth, lines):
    try:
        data = json.loads(context or "{}")
    except ValueError:
        data = {}
    picked = {key: data.get(key) for key in SPECTRUM_KEYS}
    picked.update(bandwidth=bandwidth, lines=lines)
    return json.dumps(picked, sort_keys=True)


def robust_scale(values, median):
    mad = float(np.median(np.abs(values - median)))
    # Floor the spread so a perfectly steady history cannot make every change infinite.
    return max(1.4826 * mad, 0.02 * abs(float(median)), 1e-9)


def severity_for(z):
    return "warning" if abs(z) >= WARNING_Z else "info"


# ---------------------------------------------------------- scalar metrics
def metric_findings(conn, now, baseline_days, max_age_s):
    findings, scored = [], 0
    latest_rows = conn.execute(
        "SELECT gateway_id, channel, metric_name, value, rpm, timestamp, context FROM trends_latest"
    ).fetchall()
    for gid, ch, metric, value, rpm, stamp, context in latest_rows:
        if value is None or not math.isfinite(value) or stamp is None:
            continue
        latest = parse_time(stamp)
        if (now - latest).total_seconds() > max_age_s:
            continue
        signature = metric_signature(context)
        history = conn.execute(
            "SELECT value, rpm, context FROM trends WHERE gateway_id=? AND channel=? AND metric_name=? "
            "AND timestamp>=? AND timestamp<? ORDER BY timestamp DESC LIMIT 5000",
            (gid, ch, metric, db_time(latest - timedelta(days=baseline_days)), db_time(latest)),
        ).fetchall()
        values = [
            v for v, r, c in history
            if v is not None and math.isfinite(v)
            and metric_signature(c) == signature
            and (not rpm or rpm <= 0 or (r and abs(r - rpm) <= RPM_TOLERANCE * rpm))
        ]
        if len(values) < MIN_METRIC_SAMPLES:
            continue
        scored += 1
        values = np.asarray(values, dtype=float)
        median = float(np.median(values))
        z = (value - median) / robust_scale(values, median)
        if abs(z) < INFO_Z:
            continue
        unit = ""
        try:
            unit = json.loads(context or "{}").get("unit", "")
        except ValueError:
            pass
        direction = "above" if z > 0 else "below"
        speed = f" at about {rpm:.0f} RPM" if rpm and rpm > 0 else ""
        findings.append({
            "severity": severity_for(z), "gateway_id": gid, "channel": ch, "score": round(abs(z), 2),
            "message": (f"{gid} CH{ch} {metric} is {value:.4g} {unit}, {direction} its usual "
                        f"{median:.4g} {unit}{speed} (score {abs(z):.0f} from {len(values)} comparable readings). "
                        "Review operating conditions."),
        })
    return findings, scored


# ---------------------------------------------------------------- spectra
def spectrum_features(blob, lines, bins=FEATURE_BINS):
    """Fixed-length log-power vector, so spectra of any line count compare."""
    values = np.frombuffer(blob, dtype="<f8")
    if not lines or values.size != lines + 1 or not np.all(np.isfinite(values)):
        raise ValueError("Spectrum does not match its line count")
    power = np.abs(values[1:])  # drop the DC line
    if power.size >= bins:
        banded = np.array([chunk.max() for chunk in np.array_split(power, bins)])
    else:
        banded = np.interp(np.linspace(0, power.size - 1, bins), np.arange(power.size), power)
    return np.log10(banded + 1e-12)


def band_hz(index, bandwidth, lines, bins=FEATURE_BINS):
    """Centre frequency of a feature band, matching how spectrum_features groups lines."""
    if lines >= bins:
        line = float(np.mean(np.array_split(np.arange(1, lines + 1), bins)[index]))
    else:
        line = 1 + index * (lines - 1) / (bins - 1)
    return line * bandwidth / lines


def spectrum_groups(conn, now, baseline_days, max_age_s):
    """Yield (gid, ch, bandwidth, latest_vector, baseline_matrix) for each channel."""
    channels = conn.execute("SELECT DISTINCT gateway_id, channel FROM raw_spectrum").fetchall()
    for gid, ch in channels:
        row = conn.execute(
            "SELECT timestamp, spectrum_data, bandwidth, lines, context FROM raw_spectrum "
            "WHERE gateway_id=? AND channel=? ORDER BY timestamp DESC LIMIT 1", (gid, ch)).fetchone()
        stamp, blob, bandwidth, lines, context = row
        latest = parse_time(stamp)
        if (now - latest).total_seconds() > max_age_s:
            continue
        try:
            current = spectrum_features(blob, lines)
        except ValueError:
            continue
        signature = spectrum_signature(context, bandwidth, lines)
        history = conn.execute(
            "SELECT spectrum_data, bandwidth, lines, context FROM raw_spectrum WHERE gateway_id=? AND channel=? "
            "AND timestamp>=? AND timestamp<? ORDER BY timestamp DESC LIMIT 200",
            (gid, ch, db_time(latest - timedelta(days=baseline_days)), db_time(latest))).fetchall()
        baseline = []
        for b, bw, ln, c in history:
            if spectrum_signature(c, bw, ln) != signature:
                continue
            try:
                baseline.append(spectrum_features(b, ln))
            except ValueError:
                continue
        yield gid, ch, bandwidth, lines, current, np.asarray(baseline)


def spectral_findings_cpu(groups):
    findings, scored = [], 0
    for gid, ch, bandwidth, lines, current, baseline in groups:
        if len(baseline) < MIN_SPECTRA:
            continue
        scored += 1
        median = np.median(baseline, axis=0)
        mad = np.median(np.abs(baseline - median), axis=0)
        scale = np.maximum(1.4826 * mad, 0.05)  # 0.05 decades is 0.5 dB
        z = (current - median) / scale
        worst = int(np.argmax(z))
        rise_db = 10 * float(current[worst] - median[worst])
        if z[worst] < INFO_Z or rise_db < MIN_SPECTRAL_DB:
            continue
        findings.append({
            "severity": severity_for(z[worst]), "gateway_id": gid, "channel": ch, "score": round(float(z[worst]), 2),
            "message": (f"{gid} CH{ch} spectrum: energy near {band_hz(worst, bandwidth, lines):.0f} Hz is "
                        f"{rise_db:.1f} dB above its usual level ({len(baseline)} comparable recordings). "
                        "Inspect the diagnostics page for this channel."),
        })
    return findings, scored


def spectral_findings_model(groups, model):
    """Autoencoder backend: reconstruction error versus the channel's own baseline errors."""
    findings, scored = [], 0
    for gid, ch, bandwidth, lines, current, baseline in groups:
        if len(baseline) < MIN_SPECTRA:
            continue
        scored += 1
        errors = model.reconstruction_error(np.vstack([baseline, current[None, :]]))
        history, latest = errors[:-1], float(errors[-1])
        median = float(np.median(history))
        z = (latest - median) / robust_scale(history, median)
        if z < INFO_Z:
            continue
        findings.append({
            "severity": severity_for(z), "gateway_id": gid, "channel": ch, "score": round(float(z), 2),
            "message": (f"{gid} CH{ch} spectrum shape is unusual for this machine: the {model.name} model's "
                        f"reconstruction error is {z:.1f} robust deviations above normal. "
                        "Inspect the diagnostics page for this channel."),
        })
    return findings, scored


# ---------------------------------------------------------------- accelerator
def detect_accelerator():
    """Report the AI HAT+ without requiring its software to be installed."""
    present = os.path.exists("/dev/hailo0")
    info = {"device": "/dev/hailo0" if present else None, "present": present}
    if present:
        try:
            import subprocess
            out = subprocess.run(["hailortcli", "fw-control", "identify"], capture_output=True,
                                 text=True, timeout=10).stdout
            for line in out.splitlines():
                if "Device Architecture" in line:
                    info["architecture"] = line.split(":", 1)[1].strip()
        except Exception:  # noqa: BLE001 - informational only, never fatal
            pass
    return info


def load_model(backend, hef, meta):
    if backend == "cpu":
        return None
    from hailo_backend import HailoAutoencoder  # imported lazily; needs HailoRT
    return HailoAutoencoder(hef, meta)


# ---------------------------------------------------------------- run
def analyse(db_path, now=None, baseline_days=7.0, max_age_s=3600, model=None):
    now = now or datetime.now()
    with closing(connect_readonly(db_path)) as conn:
        metric, metric_count = metric_findings(conn, now, baseline_days, max_age_s)
        groups = spectrum_groups(conn, now, baseline_days, max_age_s)
        if model is None:
            spectral, spectral_count = spectral_findings_cpu(groups)
        else:
            spectral, spectral_count = spectral_findings_model(groups, model)
    findings = sorted(metric + spectral, key=lambda f: (f["severity"] != "warning", -f["score"]))
    return {
        "findings": findings[:MAX_FINDINGS],
        "scored": {"metrics": metric_count, "spectra": spectral_count},
    }


def write_report(data_dir, report):
    folder = Path(data_dir) / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".findings-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=1)
        os.chmod(temporary, 0o644)  # the dashboard container reads it as another user
        os.replace(temporary, folder / "findings.json")
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def export_dataset(db_path, output):
    """Save every recorded spectrum as features for training an autoencoder off-device."""
    if not os.path.exists(db_path + "-shm"):
        raise SystemExit("Start the dashboard first; the export only reads a database it holds open.")
    with closing(connect_readonly(db_path)) as conn:
        rows = conn.execute("SELECT spectrum_data, lines FROM raw_spectrum").fetchall()
    vectors = []
    for blob, lines in rows:
        try:
            vectors.append(spectrum_features(blob, lines))
        except ValueError:
            continue
    if not vectors:
        raise SystemExit("No recorded spectra found. Start a recording in the dashboard first.")
    np.save(output, np.asarray(vectors, dtype=np.float32))
    return len(vectors)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True, help="Dashboard data folder holding vibration_trends.db")
    parser.add_argument("--interval", type=float, default=60, help="Seconds between runs")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--baseline-days", type=float, default=7.0)
    parser.add_argument("--backend", choices=("cpu", "hailo"), default="cpu")
    parser.add_argument("--hef", help="Compiled Hailo autoencoder (hailo backend)")
    parser.add_argument("--meta", help="Normalisation file written by train_autoencoder.py (hailo backend)")
    parser.add_argument("--export-dataset", metavar="FILE.npy", help="Export recorded spectra for training, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db_path = os.path.join(args.data_dir, "vibration_trends.db")
    if args.export_dataset:
        print(f"Exported {export_dataset(db_path, args.export_dataset)} spectra to {args.export_dataset}")
        return 0
    if args.backend == "hailo" and not (args.hef and args.meta):
        parser.error("--backend hailo needs --hef and --meta")

    accelerator = detect_accelerator()
    log.info("AI HAT+: %s", accelerator)
    model = load_model(args.backend, args.hef, args.meta)
    while True:
        started = time.time()
        report = {"generated_at": started, "backend": args.backend if model else "cpu",
                  "accelerator": accelerator}
        try:
            # Only read while the dashboard holds the database open, so this process never
            # creates SQLite side files that the dashboard user could not then open.
            if not (os.path.exists(db_path) and os.path.exists(db_path + "-shm")):
                raise FileNotFoundError(f"Waiting for the dashboard to open {db_path}")
            report.update(analyse(db_path, baseline_days=args.baseline_days, model=model))
        except (sqlite3.Error, OSError) as error:
            log.warning("Analysis skipped: %s", error)
            report.update(findings=[], scored={"metrics": 0, "spectra": 0}, error=str(error))
        write_report(args.data_dir, report)
        log.info("Scored %s; %d findings", report["scored"], len(report["findings"]))
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
