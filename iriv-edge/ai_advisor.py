#!/usr/bin/env python3
"""Self-learning vibration advisor for VDS-to-Dashboard on the Cytron IRIV Edge AI.

For every VDS channel it learns what normal spectra, waveforms and overall
readings look like at each running speed. It then watches for changes, such as
a new peak, a growing harmonic, a raised noise floor or more impacts in the
waveform. Each change is matched against the fault symptom library in
knowledge/ to suggest a likely cause. Bearing defect frequencies are computed
from the bearing numbers in knowledge/.

Advisory only: it opens the dashboard historian read-only, keeps its own
learning in <data-dir>/ai/learning.db, writes <data-dir>/ai/findings.json for
the dashboard health panel, and never issues machine commands.

Built to scale: each sample is processed once, learning is a running average,
and every cycle has a time budget. With many VDS units connected, the advisor
falls behind gracefully and reports its backlog instead of slowing the dashboard.
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
from datetime import datetime
from pathlib import Path

import numpy as np

from learning import (METRIC_FLOOR, SPECTRUM_FLOOR, WAVEFORM_FLOORS, WAVEFORM_NAMES, ProfileStore,
                      bin_frequencies, speed_band, spectrum_features, waveform_features, waveform_vector)
from symptoms import Knowledge, SpectrumView, diagnose

BATCH = 500
CHANGE_Z = 4.0
PEAK_RISE_DB = 6.0
FLOOR_RISE_DB = 3.0
MAX_AGE_S = 2 * 3600
MAX_FINDINGS = 25
BEHIND_WARNING = 20 * BATCH

log = logging.getLogger("vds-ai-advisor")
HERE = Path(__file__).resolve().parent


def connect_readonly(db_path):
    """Open the historian so that any write attempt fails."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_ms(value):
    return int(datetime.fromisoformat(str(value)).timestamp() * 1000)


def settings_signature(context, *keys, **extra):
    try:
        data = json.loads(context or "{}")
    except ValueError:
        data = {}
    if not keys:
        data.pop("timestamp_ms", None)
        picked = data
    else:
        picked = {key: data.get(key) for key in keys}
    picked.update(extra)
    return json.dumps(picked, sort_keys=True)


class Advisor:
    def __init__(self, data_dir, knowledge_dir=HERE / "knowledge", learn_samples=48, learn_hours=12.0):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "vibration_trends.db"
        (self.data_dir / "ai").mkdir(parents=True, exist_ok=True)
        self.store = ProfileStore(self.data_dir / "ai" / "learning.db")
        self.knowledge = Knowledge(knowledge_dir)
        self.learn_samples, self.learn_hours = learn_samples, learn_hours
        self.assets, self.velocity = {}, {}

    # ------------------------------------------------------------ cycle
    def cycle(self, now_ms=None, budget_s=30.0):
        started = time.monotonic()
        processed = 0
        with closing(connect_readonly(self.db_path)) as conn:
            self.assets = {(g, c): json.loads(s) for g, c, s in conn.execute(
                "SELECT gateway_id, channel, settings FROM asset_settings")}
            self.velocity = {(g, c): (v, ctx) for g, c, v, ctx in conn.execute(
                "SELECT gateway_id, channel, value, context FROM trends_latest WHERE metric_name='Velocity'")}
            while True:
                done = self.process_samples(conn) + self.process_metrics(conn)
                processed += done
                if done == 0 or time.monotonic() - started >= budget_s:
                    break
            backlog = self.backlog(conn)
        self.store.flush()
        return self.report(now_ms or int(time.time() * 1000), processed, backlog, time.monotonic() - started)

    def backlog(self, conn):
        waiting = 0
        for source, query in (("learning_samples", "SELECT COUNT(*) FROM learning_samples WHERE id > ?"),
                              ("trends", "SELECT COUNT(*) FROM trends WHERE rowid > ?")):
            waiting += conn.execute(query, (self.store.watermark(source),)).fetchone()[0]
        return waiting

    # ------------------------------------------------------------ samples
    def process_samples(self, conn):
        rows = conn.execute(
            "SELECT id, gateway_id, channel, kind, timestamp_ms, rpm, data, sampling_freq, bandwidth, lines, context "
            "FROM learning_samples WHERE id > ? ORDER BY id LIMIT ?",
            (self.store.watermark("learning_samples"), BATCH)).fetchall()
        for row in rows:
            try:
                if row[3] == "spectrum":
                    self.spectrum_sample(*row[1:])
                elif row[3] == "waveform":
                    self.waveform_sample(*row[1:])
            except (ValueError, TypeError) as error:
                log.debug("Skipped sample %s: %s", row[0], error)
        if rows:
            self.store.set_watermark("learning_samples", rows[-1][0])
        return len(rows)

    def learning(self, state, profile, ms):
        state.update(phase="learning", samples=profile.n, last_ms=ms,
                     hours=round((profile.last_ms - profile.first_ms) / 3600_000, 1))

    def spectrum_sample(self, gid, ch, kind, ms, rpm, blob, fs, bandwidth, lines, context):
        x = spectrum_features(np.frombuffer(blob, "<f4"), lines)
        signature = settings_signature(context, "sensor_type", "sensitivity", "window",
                                       bandwidth=bandwidth, lines=lines)
        band = speed_band(rpm)
        state = self.store.state(gid, ch, "spectrum")
        self.store.touch(gid, ch, "spectrum")
        reference = self.store.reference(gid, ch, "spectrum", signature, band, self.learn_samples, self.learn_hours)
        if reference is None:
            self.store.learn(gid, ch, "spectrum", signature, band, x, ms)
            self.learning(state, self.store.profile(gid, ch, "spectrum", signature, band), ms)
            return
        wave = self.store.state(gid, ch, "waveform")
        view = SpectrumView(bin_frequencies(bandwidth, lines), x, reference.mean, rpm,
                            wave.get("features"), wave.get("normal"))
        z = reference.zscores(x, SPECTRUM_FLOOR)
        peaks = [(hz, rise) for i, hz, rise in view.changed_peaks(PEAK_RISE_DB) if z[i] >= CHANGE_Z]
        floor = {band_name: view.band_rise(band_name) for band_name in ("all", "upper")}
        changed = bool(peaks) or max(floor.values()) >= FLOOR_RISE_DB
        detail = None
        if changed:
            info = self.knowledge.machine(gid, ch, self.assets.get((gid, ch)))
            detail = {"peaks": [[round(hz, 2), round(rise, 1)] for hz, rise in peaks],
                      "floor_db": {k: round(v, 1) for k, v in floor.items()},
                      "rpm": rpm, "matches": diagnose(self.knowledge, view, info)}
        else:
            self.store.learn(gid, ch, "spectrum", signature, band, x, ms)
        self.observe(state, ms, changed, detail)

    def waveform_sample(self, gid, ch, kind, ms, rpm, blob, fs, bandwidth, lines, context):
        values = np.frombuffer(blob, "<f4")
        features = waveform_features(values)
        state = self.store.state(gid, ch, "waveform")
        self.store.touch(gid, ch, "waveform")
        state["features"] = {k: round(float(features[k]), 4) for k in WAVEFORM_NAMES}
        if features["clipped"]:
            # Saturated sensor input: report as data quality, never learn from it.
            self.observe(state, ms, True, {"clipped": True})
            return
        vector = waveform_vector(features)
        signature = settings_signature(context, "sensor_type", "sensitivity", sampling_freq=fs, length=len(values))
        band = speed_band(rpm)
        reference = self.store.reference(gid, ch, "waveform", signature, band, self.learn_samples, self.learn_hours)
        if reference is None:
            self.store.learn(gid, ch, "waveform", signature, band, vector, ms)
            self.learning(state, self.store.profile(gid, ch, "waveform", signature, band), ms)
            return
        m = reference.mean
        normal = {"rms": 10 ** m[0], "peak": 10 ** m[1], "crest": m[2], "kurtosis": m[3], "skewness": m[4]}
        state["normal"] = {k: round(float(v), 4) for k, v in normal.items()}
        z = reference.zscores(vector, WAVEFORM_FLOORS)
        level = features["rms"] / normal["rms"]
        changes = []
        if z[0] >= CHANGE_Z and level >= 1.25:
            changes.append(f"overall level {level:.1f}x normal")
        if z[3] >= CHANGE_Z and features["kurtosis"] >= 4 and features["kurtosis"] - normal["kurtosis"] >= 1:
            changes.append(f"kurtosis {normal['kurtosis']:.1f} to {features['kurtosis']:.1f}")
        if z[2] >= CHANGE_Z and features["crest"] - normal["crest"] >= 1:
            changes.append(f"crest factor {normal['crest']:.1f} to {features['crest']:.1f}")
        if not changes:
            self.store.learn(gid, ch, "waveform", signature, band, vector, ms)
        self.observe(state, ms, bool(changes), {"changes": changes,
                                               "impacts": any(c.startswith(("kurtosis", "crest")) for c in changes)})

    # ------------------------------------------------------------ metrics
    def process_metrics(self, conn):
        mark = self.store.watermark("trends")
        newest = conn.execute("SELECT MAX(rowid) FROM trends").fetchone()[0] or 0
        if newest < mark:  # trends were cleared; start again
            mark = 0
        rows = conn.execute(
            "SELECT rowid, gateway_id, channel, metric_name, value, rpm, timestamp, context FROM trends "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?", (mark, BATCH)).fetchall()
        for rowid, gid, ch, metric, value, rpm, stamp, context in rows:
            try:
                if value is None or not math.isfinite(value) or value <= 0:
                    continue
                self.metric_sample(gid, ch, metric, value, rpm, parse_ms(stamp), context)
            except (ValueError, TypeError) as error:
                log.debug("Skipped trend %s: %s", rowid, error)
        if rows:
            self.store.set_watermark("trends", rows[-1][0])
        return len(rows)

    def metric_sample(self, gid, ch, metric, value, rpm, ms, context):
        kind = "metric:" + metric
        signature = settings_signature(context)
        band = speed_band(rpm)
        x = np.array([math.log10(value)])
        state = self.store.state(gid, ch, kind)
        self.store.touch(gid, ch, kind)
        reference = self.store.reference(gid, ch, kind, signature, band, self.learn_samples, self.learn_hours)
        if reference is None:
            self.store.learn(gid, ch, kind, signature, band, x, ms)
            self.learning(state, self.store.profile(gid, ch, kind, signature, band), ms)
            return
        normal = 10 ** float(reference.mean[0])
        z = float(reference.zscores(x, METRIC_FLOOR)[0])
        ratio = value / normal
        changed = abs(z) >= CHANGE_Z and (ratio >= 1.25 or ratio <= 0.6)
        if not changed:
            self.store.learn(gid, ch, kind, signature, band, x, ms)
        unit = json.loads(context or "{}").get("unit", "")
        self.observe(state, ms, changed, {"value": value, "normal": normal, "unit": unit, "rpm": rpm})

    # ------------------------------------------------------------ findings
    def observe(self, state, ms, changed, detail):
        """A change must be seen in 2 of the last 3 samples before it is reported."""
        recent = (state.get("recent", []) + [bool(changed)])[-3:]
        state.update(phase="monitoring", recent=recent, last_ms=ms)
        state.pop("samples", None)
        if changed:
            state["detail"] = detail
            state.setdefault("since_ms", ms)
        if not any(recent):
            state.pop("since_ms", None)
            state.pop("detail", None)
        state["active"] = sum(recent) >= 2 and "detail" in state

    def describe(self, gid, ch, kind, state, now_ms):
        info = self.knowledge.machine(gid, ch, self.assets.get((gid, ch)))
        where = f"{gid} CH{ch}"
        label = where if info["name"] == where else f"{info['name']} ({where})"
        d = state["detail"]
        severity, parts = "info", []
        if kind == "spectrum":
            rpm = d.get("rpm")
            for hz, rise in d["peaks"][:2]:
                order = f" ({hz / (rpm / 60):.2f}x)" if rpm else ""
                parts.append(f"peak at {hz:.1f} Hz{order} up {rise:.0f} dB")
            if d["floor_db"]["upper"] >= FLOOR_RISE_DB:
                parts.append(f"high-frequency noise floor up {d['floor_db']['upper']:.0f} dB")
            text = f"{label}: spectrum changed from learned normal: " + "; ".join(parts or ["broadband rise"]) + "."
            if d["matches"]:
                best = d["matches"][0]
                bearing = info.get("bearing_orders", {}).get("designation")
                name = best["name"] + (f" ({bearing})" if bearing and best["id"].startswith("bearing_") else "")
                text += f" Likely {name}, {best['confidence']:.0%} match: {', '.join(best['evidence'])}. {best['advice']}"
                others = [m["name"] for m in d["matches"][1:3]]
                if others:
                    text += f" Also possible: {', '.join(others)}."
                if best["confidence"] >= 0.7:
                    severity = "warning"
            if d["peaks"] and d["peaks"][0][1] >= 12:
                severity = "warning"
        elif kind == "waveform":
            if d.get("clipped"):
                text = f"{label}: waveform is clipped. The sensor range may be too small; readings are not reliable."
            else:
                text = f"{label}: waveform changed from learned normal: {', '.join(d['changes'])}."
                if d.get("impacts"):
                    text += " More impacts usually come from bearing damage, looseness or rubbing."
                    severity = "warning"
        else:
            metric = kind.split(":", 1)[1]
            ratio = d["value"] / d["normal"]
            text = (f"{label}: {metric} is {d['value']:.3g} {d['unit']}, {ratio:.1f}x its learned normal "
                    f"{d['normal']:.3g} {d['unit']}" + (f" at about {d['rpm']:.0f} RPM" if d.get("rpm") else "") + ".")
            severity = "warning" if ratio >= 2 else "info"
        zone = self.zone(gid, ch, info)
        if zone:
            text += f" Velocity is in ISO zone {zone}."
            severity = "warning" if zone in "CD" else severity
        days = (now_ms - state.get("since_ms", now_ms)) / 86_400_000
        if days >= 3:
            text += (f" Changed for {days:.0f} days; if this is the new normal after maintenance, "
                     f"relearn with: ai_advisor.py --relearn {gid}/{ch}")
        score = max([p[1] for p in d.get("peaks", [])] + [abs(math.log(d["value"] / d["normal"])) * 10
                                                           if "value" in d else 0])
        return {"severity": severity, "gateway_id": gid, "channel": ch, "kind": kind, "score": round(score, 1),
                "message": text, "since_ms": state.get("since_ms"), "detail": d}

    def zone(self, gid, ch, info):
        value, context = self.velocity.get((gid, ch), (None, None))
        if value is None:
            return None
        meta = json.loads(context or "{}")
        return self.knowledge.iso_zone(info, value, meta.get("unit"), meta.get("detection"))

    def report(self, now_ms, processed, backlog, elapsed):
        findings, channels = [], {}
        for (gid, ch, kind), state in self.store.states():
            phases = channels.setdefault((gid, ch), set())
            phases.add(state.get("phase"))
            if state.get("active") and now_ms - state.get("last_ms", 0) <= MAX_AGE_S * 1000:
                findings.append(self.describe(gid, ch, kind, state, now_ms))
        reported = {(f["gateway_id"], f["channel"]) for f in findings}
        for (gid, ch) in self.velocity:
            info = self.knowledge.machine(gid, ch, self.assets.get((gid, ch)))
            zone = self.zone(gid, ch, info)
            if zone in ("C", "D") and (gid, ch) not in reported:
                value = self.velocity[(gid, ch)][0]
                findings.append({"severity": "warning", "gateway_id": gid, "channel": ch, "kind": "severity",
                                 "score": value, "message": f"{info['name']} ({gid} CH{ch}): Velocity {value:.2f} mm/s "
                                 f"is in ISO zone {zone} ({'restricted operation' if zone == 'C' else 'damage likely'})."})
        findings.sort(key=lambda f: (f["severity"] != "warning", -f["score"]))
        learning = sum(1 for p in channels.values() if "monitoring" not in p)
        summary = (f"Monitoring {len(channels) - learning} channels, still learning {learning}; "
                   f"{processed} new samples analysed in {elapsed:.1f} s.")
        if backlog >= BEHIND_WARNING:
            findings.insert(0, {"severity": "info", "score": 0, "message": (
                f"AI advisor is {backlog} samples behind. Increase VDS_LEARNING_INTERVAL_S or the advisor budget.")})
        return {"generated_at": time.time(), "summary": summary, "backlog": backlog,
                "channels": {"monitoring": len(channels) - learning, "learning": learning},
                "findings": findings[:MAX_FINDINGS]}


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


def status(advisor):
    rows = []
    for (gid, ch, kind), state in sorted(advisor.store.states()):
        phase = state.get("phase", "?")
        if phase == "learning":
            phase += f" ({state.get('samples', 0)}/{advisor.learn_samples} samples, " \
                     f"{state.get('hours', 0)}/{advisor.learn_hours:g} h)"
        elif state.get("active"):
            phase += ", CHANGE REPORTED"
        rows.append(f"{gid}/{ch:<3} {kind:<28} {phase}")
    return "\n".join(rows) or "Nothing learned yet."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True, help="Dashboard data folder holding vibration_trends.db")
    parser.add_argument("--knowledge", default=str(HERE / "knowledge"), help="Fault, bearing and machine files")
    parser.add_argument("--interval", type=float, default=60, help="Seconds between cycles")
    parser.add_argument("--budget", type=float, default=0.5, help="Share of each interval spent analysing (0-1)")
    parser.add_argument("--learn-samples", type=int, default=48, help="Samples needed before monitoring starts")
    parser.add_argument("--learn-hours", type=float, default=12, help="Hours of operation needed before monitoring")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show learning progress per channel and exit")
    parser.add_argument("--relearn", metavar="GATEWAY/CHANNEL", help="Forget what was learned for one channel")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    advisor = Advisor(args.data_dir, args.knowledge, args.learn_samples, args.learn_hours)
    if args.status:
        print(status(advisor))
        return 0
    if args.relearn:
        gid, _, ch = args.relearn.rpartition("/")
        advisor.store.forget(gid, ch)
        print(f"Forgot learning for {gid} CH{ch}; it relearns from the next samples.")
        return 0
    budget = max(1.0, args.interval * min(max(args.budget, 0.05), 0.9))
    while True:
        started = time.time()
        # Only read while the dashboard holds the database open, so this process never
        # creates SQLite side files that the dashboard user could not then open.
        if not (advisor.db_path.exists() and Path(str(advisor.db_path) + "-shm").exists()):
            log.warning("Waiting for the dashboard to open %s", advisor.db_path)
            report = {"generated_at": started, "summary": "Waiting for the dashboard database.",
                      "findings": [], "error": f"Waiting for the dashboard to open {advisor.db_path}"}
        else:
            try:
                report = advisor.cycle(budget_s=budget)
            except sqlite3.Error as error:
                log.warning("Cycle skipped: %s", error)
                report = {"generated_at": started, "summary": f"Cycle skipped: {error}", "findings": [],
                          "error": str(error)}
        write_report(args.data_dir, report)
        log.info("%s %d findings, backlog %s", report["summary"], len(report["findings"]), report.get("backlog", 0))
        if args.once:
            advisor.store.close()
            return 0
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
