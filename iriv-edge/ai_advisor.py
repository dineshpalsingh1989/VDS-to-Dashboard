#!/usr/bin/env python3
"""Self-learning vibration advisor for VDS-to-Dashboard on the Cytron IRIV Edge AI.

For every VDS with its AI switch turned on in the dashboard, it learns what
normal spectra, envelope spectra, waveforms and overall readings look like at
each running speed. It then quietly watches for changes: a new peak, a growing
harmonic, bearing impacts in the envelope spectrum, more impacts in the
waveform, or a reading that keeps rising. Each change is matched against the
fault symptom library in knowledge/ and against faults that people confirmed
before, to suggest a likely cause. Rising readings get a time-to-action
estimate. People's feedback (confirmed, wrong, repaired) teaches it.

Every day it works it writes a report folder: an activity log, plus a folder of
evidence for each new finding. When an NVMe SSD is mounted, it announces and
then moves its learning file and reports onto the SSD.

Advisory only: it opens the dashboard historian read-only, never issues machine
commands, and runs at low priority with a capped scan rate.
"""

import argparse
import json
import logging
import math
import os
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np

from learning import (METRIC_FLOOR, SPECTRUM_FLOOR, WAVEFORM_FLOORS, WAVEFORM_NAMES, ProfileStore,
                      bin_frequencies, envelope_features, speed_band, speed_tolerant, spectrum_features,
                      waveform_features, waveform_vector)
from reports import Reports, atomic_json
from storage import Storage, detect
from symptoms import Knowledge, SpectrumView, diagnose, frequency_table

BATCH = 200
CHANGE_Z = 4.0
PEAK_RISE_DB = 6.0
GREY_Z, GREY_RISE_DB = 2.5, 3.0  # borderline changes: not reported, but never learned either
FLOOR_RISE_DB = 3.0
MAX_AGE_S = 2 * 3600
MAX_FINDINGS = 25
BEHIND_WARNING = 20 * BATCH
TREND_DAYS = 7
TREND_HORIZON_DAYS = 60
ACCEPT_SAMPLES = 6       # after "wrong", this many samples teach the new normal quickly
ACCEPT_WEIGHT = 0.25
LABEL_SIMILARITY = 0.8
TREND_METRICS = ("Velocity", "Acceleration")

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


def write_report(data_dir, report):
    folder = Path(data_dir) / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / "findings.json", report)


def fit_trend(points, windows=(1, 2, 3, 5, 7)):
    """Straight-line trend of hourly means: slope per day, value now and fit quality.

    Wear often starts after a flat period, so the line is fitted over the last 1, 2, 3,
    5 and 7 days and the window that fits best is used.
    """
    if len(points) < 12:
        return None
    hours = np.array([p[0] for p in points], dtype=float)
    values = np.array([p[1] for p in points], dtype=float)
    days = (hours - hours[-1]) / 86_400_000
    if -days[0] < 1:
        return None
    best = None
    for window in windows:
        keep = days >= -window
        if keep.sum() < 12 or (best and keep.sum() == best["points"]):
            continue
        d, v = days[keep], values[keep]
        slope, current = np.polyfit(d, v, 1)
        spread = float(np.sum((v - v.mean()) ** 2))
        r2 = 1 - float(np.sum((v - (slope * d + current)) ** 2)) / spread if spread > 0 else 0.0
        if best is None or r2 > best["r2"] + 0.02:
            best = {"slope": float(slope), "current": float(current), "r2": round(r2, 3),
                    "points": int(keep.sum()), "days_fitted": window}
    if best is None:
        return None
    best["weekly"] = best["slope"] * 7 / best["current"] if best["current"] > 0 else 0.0
    best["rising"] = bool(best["slope"] > 0 and best["r2"] >= 0.5 and best["weekly"] >= 0.10)
    return best


def in_days(days):
    """Plain wording for a time to action."""
    if days < 1:
        return "within a day"
    return f"in about {days:.0f} day" + ("" if round(days) == 1 else "s")


def similarity(a, b):
    a, b = np.clip(a, 0, None), np.clip(b, 0, None)
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / norm) if norm > 0 else 0.0


class Advisor:
    def __init__(self, data_dir, knowledge_dir=HERE / "knowledge", learn_samples=48, learn_hours=12.0,
                 max_rate=None, use_ssd=True, mounts_file="/proc/mounts", sys_block="/sys/block",
                 report_days=180):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "vibration_trends.db"
        self.local = self.data_dir / "ai"
        self.local.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge = Knowledge(knowledge_dir)
        self.learn_samples, self.learn_hours, self.max_rate = learn_samples, learn_hours, max_rate
        self.use_ssd, self.mounts_file, self.sys_block = use_ssd, mounts_file, sys_block
        self.storage = Storage(self.data_dir)
        self.pending_ssd = None
        self.assets, self.velocity, self.ai, self.enabled, self.units = {}, {}, {}, set(), {}
        self.trends, self.trend_hour, self.tables_text = {}, None, None
        self.events = []
        root = self.storage.root()
        self.store = ProfileStore(root / "learning.db") if root.exists() else None
        self.reports = Reports(root, self.local, report_days)
        self.storage_status = {"state": "local", "message": "AI data is stored on the built-in eMMC.",
                               "path": str(root)}

    # ------------------------------------------------------------ storage
    def check_storage(self):
        """Pick the storage location; move to the SSD one cycle after announcing it."""
        root = self.storage.root()
        if self.storage.on_ssd():
            if not root.exists():
                self.storage_status = {"state": "ssd_missing", "path": str(root), "message": (
                    f"The NVMe SSD holding AI data ({root}) is not available. AI analysis is paused until it is "
                    "mounted again.")}
                return False
            if self.store is None:
                self.store = ProfileStore(root / "learning.db")
            self.storage_status = {"state": "ssd", "path": str(root),
                                   "message": f"AI data is stored on the NVMe SSD at {root}."}
            return True
        found = detect(self.data_dir, self.mounts_file, self.sys_block)
        if found["kind"] == "ssd" and self.use_ssd:
            target = Path(found["mount"]) / "vds-ai"
            if self.pending_ssd != target:
                self.pending_ssd = target
                self.storage_status = {"state": "pending", "path": str(target), "message": (
                    f"NVMe SSD found at {found['mount']}. The AI system will transfer its data to the SSD "
                    "in the next cycle.")}
                self.events.append(self.storage_status["message"])
            else:
                self.transfer(target)
            return True
        self.storage_status = {
            "local-ssd": {"state": "ssd", "message": "AI data is stored on the NVMe SSD the system runs from."},
            "ssd-unmounted": {"state": "ssd_unmounted", "message": (
                "An NVMe SSD is fitted but not set up. To move AI data onto it, run: "
                "sudo bash iriv-edge/setup-ssd.sh")},
        }.get(found["kind"], {"state": "local", "message": "AI data is stored on the built-in eMMC."})
        self.storage_status["path"] = str(self.local)
        return True

    def transfer(self, target):
        self.storage_status = {"state": "transferring", "path": str(target), "message": (
            f"The AI system is transferring its data to the NVMe SSD at {target}. Analysis resumes when done.")}
        write_report(self.data_dir, {"generated_at": time.time(), "summary": self.storage_status["message"],
                                     "findings": [], "storage": self.storage_status})
        log.info(self.storage_status["message"])
        self.store.close()
        try:
            target.mkdir(parents=True, exist_ok=True)
            source = self.local / "learning.db"
            if source.exists():
                temporary = target / "learning.db.copy"
                with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
                    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Copied learning file failed its integrity check")
                os.replace(temporary, target / "learning.db")
            if (self.local / "reports").exists():
                shutil.copytree(self.local / "reports", target / "reports", dirs_exist_ok=True)
                for path in (self.local / "reports").rglob("*"):
                    copy = target / "reports" / path.relative_to(self.local / "reports")
                    if path.is_file() and (not copy.exists() or copy.stat().st_size != path.stat().st_size):
                        raise RuntimeError(f"Report file did not copy correctly: {path}")
            self.storage.set_root(target)
        except (OSError, RuntimeError, sqlite3.Error) as error:
            self.store = ProfileStore(self.local / "learning.db")
            self.storage_status = {"state": "failed", "path": str(self.local), "message": (
                f"Transfer of AI data to the SSD failed ({error}). AI data stays on the eMMC.")}
            self.use_ssd = False
            self.events.append(self.storage_status["message"])
            return
        for name in ("learning.db", "learning.db-wal", "learning.db-shm"):
            (self.local / name).unlink(missing_ok=True)
        shutil.rmtree(self.local / "reports", ignore_errors=True)
        self.store = ProfileStore(target / "learning.db")
        self.reports.root = target
        self.storage_status = {"state": "ssd", "path": str(target),
                               "message": f"AI data was transferred to the NVMe SSD at {target}."}
        self.events.append(self.storage_status["message"])

    # ------------------------------------------------------------ settings and feedback
    def load_settings(self, conn):
        self.assets = {(g, c): json.loads(s) for g, c, s in conn.execute(
            "SELECT gateway_id, channel, settings FROM asset_settings")}
        self.velocity = {(g, c): (v, ctx) for g, c, v, ctx in conn.execute(
            "SELECT gateway_id, channel, value, context FROM trends_latest WHERE metric_name='Velocity'")}
        self.units = {(g, c, m): json.loads(ctx or "{}").get("unit", "") for g, c, m, ctx in conn.execute(
            "SELECT gateway_id, channel, metric_name, context FROM trends_latest")}
        try:
            rows = conn.execute("SELECT gateway_id, channel, settings FROM ai_settings").fetchall()
            bearings = [json.loads(r[0]) for r in conn.execute("SELECT data FROM ai_bearings")]
            feedback = conn.execute(
                "SELECT id, gateway_id, channel, kind, since_ms, fault_id, verdict, user FROM ai_feedback "
                "WHERE id > ? ORDER BY id", (self.store.watermark("feedback"),)).fetchall()
        except sqlite3.OperationalError:  # dashboard older than the AI settings
            rows, bearings, feedback = [], [], []
        self.knowledge = Knowledge(self.knowledge_dir)
        self.knowledge.add_user_bearings(bearings)
        self.ai = {(g, c): json.loads(s) for g, c, s in rows if c}
        self.enabled = {g for g, c, s in rows if not c and json.loads(s).get("enabled")}
        for (gid, ch), settings in self.ai.items():
            requested = int(settings.get("relearn_at") or 0)
            if requested > self.store.watermark(f"relearn:{gid}/{ch}"):
                self.store.forget(gid, ch)
                self.store.set_watermark(f"relearn:{gid}/{ch}", requested)
                self.events.append(f"Relearning {gid} CH{ch} as requested in the dashboard.")
        for row in feedback:
            self.apply_feedback(*row)
        if feedback:
            self.store.set_watermark("feedback", feedback[-1][0])

    def apply_feedback(self, fid, gid, ch, kind, since_ms, fault_id, verdict, user):
        who = f" by {user}" if user else ""
        if verdict == "repaired":
            self.store.forget(gid, ch)
            self.trends.pop((gid, ch), None)
            self.events.append(f"Marked repaired{who}: {gid} CH{ch}. Relearning its new normal.")
            return
        state = self.store.state(gid, ch, kind)
        self.store.touch(gid, ch, kind)
        if verdict == "wrong":
            # A person says this is normal operation: learn it quickly and close the finding.
            state.update(accept=ACCEPT_SAMPLES, active=False, recent=[False, False, False])
            for key in ("detail", "since_ms", "report", "peak_history"):
                state.pop(key, None)
            self.events.append(f"Marked wrong{who}: {gid} CH{ch} {kind}. Learning this pattern as normal.")
            return
        # confirmed: keep the change pattern so the same fault is recognised again
        name = fault_id or kind
        match = next((m for m in (state.get("detail") or {}).get("matches", []) if m["id"] == fault_id), None)
        if match:
            name = match["name"]
        state["confirmed"] = {"fault": name, "by": user or "", "ms": int(time.time() * 1000)}
        vector = self.report_vector(state, kind)
        if vector is not None:
            self.store.add_label(gid, ch, kind, fault_id or kind, name, vector, int(time.time() * 1000))
        self.events.append(f"Confirmed{who}: {gid} CH{ch} {name}." + ("" if vector is not None else
                           " No spectrum evidence was available to learn its pattern."))

    @staticmethod
    def report_vector(state, kind):
        folder = state.get("report")
        if not folder or kind not in ("spectrum", "envelope"):
            return None
        try:
            return np.loadtxt(Path(folder) / f"{kind}.csv", delimiter=",", skiprows=1, ndmin=2)[:, 3]
        except (OSError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------ cycle
    def cycle(self, now_ms=None, budget_s=30.0):
        started = time.monotonic()
        now_ms = now_ms or int(time.time() * 1000)
        if not self.check_storage():
            return self.report(now_ms, 0, 0, 0.0)
        processed = samples = 0
        with closing(connect_readonly(self.db_path)) as conn:
            self.load_settings(conn)
            while time.monotonic() - started < budget_s:
                done_samples = self.process_samples(conn)
                done = done_samples + self.process_metrics(conn)
                processed, samples = processed + done, samples + done_samples
                if done == 0:
                    break
                if self.max_rate:  # scan quietly: never faster than max_rate samples per second
                    ahead = samples / self.max_rate - (time.monotonic() - started)
                    remaining = budget_s - (time.monotonic() - started)
                    if ahead > 0:
                        time.sleep(min(ahead, max(0.0, remaining)))
            backlog = self.backlog(conn)
        self.update_trends(now_ms)
        self.store.flush()
        report = self.report(now_ms, processed, backlog, time.monotonic() - started)
        when = now_ms / 1000
        if processed or self.events:
            self.reports.activity(f"analysed {processed} new samples and readings; "
                                  f"{len(report['findings'])} active findings; {self.storage_status['message']}", when)
            for event in self.events:
                self.reports.activity(event, when)
        self.events = []
        self.reports.prune(when)
        self.publish_tables()
        return report

    def backlog(self, conn):
        waiting = 0
        for source, query in (("learning_samples", "SELECT COUNT(*) FROM learning_samples WHERE id > ?"),
                              ("trends", "SELECT COUNT(*) FROM trends WHERE rowid > ?")):
            waiting += conn.execute(query, (self.store.watermark(source),)).fetchone()[0]
        return waiting

    def rpm_for(self, gid, ch, rpm):
        """Speed from the VDS speed sensor, or the fixed speed the user entered."""
        settings = self.ai.get((gid, ch), {})
        if settings.get("speed_source") == "fixed" and settings.get("fixed_rpm"):
            return float(settings["fixed_rpm"])
        if rpm and rpm > 0:
            return float(rpm)
        return float(settings["fixed_rpm"]) if settings.get("fixed_rpm") else None

    def info(self, gid, ch):
        return self.knowledge.machine(gid, ch, self.assets.get((gid, ch)), self.ai.get((gid, ch)))

    @staticmethod
    def accepted(state, changed):
        """After 'wrong' feedback, the next few changed samples are learned as the new normal."""
        if changed and state.get("accept"):
            state["accept"] -= 1
            return True
        return False

    # ------------------------------------------------------------ samples
    def process_samples(self, conn):
        rows = conn.execute(
            "SELECT id, gateway_id, channel, kind, timestamp_ms, rpm, data, sampling_freq, bandwidth, lines, context "
            "FROM learning_samples WHERE id > ? ORDER BY id LIMIT ?",
            (self.store.watermark("learning_samples"), BATCH)).fetchall()
        for row in rows:
            if row[1] not in self.enabled:
                continue
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

    def spectral(self, gid, ch, kind, freqs, x, signature, rpm, ms, source):
        """Shared learning and change detection for the normal and the envelope spectrum."""
        band = speed_band(rpm)
        state = self.store.state(gid, ch, kind)
        self.store.touch(gid, ch, kind)
        state["rpm"] = rpm
        reference = self.store.reference(gid, ch, kind, signature, band, self.learn_samples, self.learn_hours)
        if reference is None:
            self.store.learn(gid, ch, kind, signature, band, x, ms)
            self.learning(state, self.store.profile(gid, ch, kind, signature, band), ms)
            return
        wave = self.store.state(gid, ch, "waveform")
        normal = speed_tolerant(reference.mean, freqs)
        view = SpectrumView(freqs, x, normal, rpm, wave.get("features"), wave.get("normal"))
        z = reference.zscores(x, SPECTRUM_FLOOR, normal)
        peaks = [(hz, rise) for i, hz, rise in view.changed_peaks(PEAK_RISE_DB) if z[i] >= CHANGE_Z]
        floor = {band_name: view.band_rise(band_name) for band_name in ("all", "upper")}
        changed = bool(peaks) or (source == "spectrum" and max(floor.values()) >= FLOOR_RISE_DB)
        # A slowly growing fault must not be learned bit by bit as "normal".
        borderline = any(z[i] >= GREY_Z for i, _, _ in view.changed_peaks(GREY_RISE_DB))
        if self.accepted(state, changed):
            self.store.learn(gid, ch, kind, signature, band, x, ms, ACCEPT_WEIGHT)
            changed = False
        elif not changed and not borderline:
            self.store.learn(gid, ch, kind, signature, band, x, ms)
        detail = None
        if changed:
            info = self.info(gid, ch)
            matches = self.recognise(gid, ch, kind, 10 * (x - normal)) + diagnose(
                self.knowledge, view, info, source=source)
            detail = {"peaks": [[round(hz, 2), round(rise, 1)] for hz, rise in peaks],
                      "floor_db": {k: round(v, 1) for k, v in floor.items()}, "rpm": rpm,
                      "matches": sorted(matches, key=lambda m: -m["confidence"])}
            if peaks:  # growth of the strongest changed peak, for the trend sentence
                history = state.setdefault("peak_history", [])
                history.append([ms, round(peaks[0][1], 1)])
                del history[:-200]
        transition = self.observe(state, ms, changed, detail)
        self.after(gid, ch, kind, state, transition, ms,
                   {"name": kind, "freqs": freqs, "current": x, "normal": normal})

    def recognise(self, gid, ch, kind, rise_db):
        """Compare a change with faults people confirmed before, on this or any other channel."""
        best = {}
        for g, c, fault_id, name, vector, ms in self.store.labels(kind, len(rise_db)):
            score = similarity(rise_db, vector)
            if score >= LABEL_SIMILARITY and score > best.get(fault_id, {}).get("confidence", 0):
                where = "this sensor" if (g, c) == (gid, ch) else f"{g} CH{c}"
                best[fault_id] = {"id": "confirmed:" + fault_id, "confidence": round(score, 2),
                                  "name": f"{name} (seen before)",
                                  "evidence": [f"{score:.0%} the same change pattern as a fault confirmed on {where} "
                                               f"on {datetime.fromtimestamp(ms / 1000):%Y-%m-%d}"],
                                  "advice": "A person confirmed this pattern before; treat it with priority."}
        return list(best.values())

    def spectrum_sample(self, gid, ch, kind, ms, rpm, blob, fs, bandwidth, lines, context):
        rpm = self.rpm_for(gid, ch, rpm)
        x = spectrum_features(np.frombuffer(blob, "<f4"), lines)
        signature = settings_signature(context, "sensor_type", "sensitivity", "window",
                                       bandwidth=bandwidth, lines=lines)
        self.spectral(gid, ch, "spectrum", bin_frequencies(bandwidth, lines), x, signature, rpm, ms, "spectrum")

    def waveform_sample(self, gid, ch, kind, ms, rpm, blob, fs, bandwidth, lines, context):
        values = np.frombuffer(blob, "<f4")
        rpm = self.rpm_for(gid, ch, rpm)
        features = waveform_features(values)
        state = self.store.state(gid, ch, "waveform")
        self.store.touch(gid, ch, "waveform")
        state["features"] = {k: round(float(features[k]), 4) for k in WAVEFORM_NAMES}
        if features["clipped"]:
            # Saturated sensor input: report as data quality, never learn from it.
            transition = self.observe(state, ms, True, {"clipped": True})
            self.after(gid, ch, "waveform", state, transition, ms, {"waveform": state["features"]})
            return
        signature = settings_signature(context, "sensor_type", "sensitivity", sampling_freq=fs, length=len(values))
        try:
            freqs, envelope = envelope_features(values, fs)
            state.pop("envelope_note", None)
            self.spectral(gid, ch, "envelope", freqs, envelope, signature, rpm, ms, "envelope")
        except ValueError as reason:
            state["envelope_note"] = str(reason)
        vector = waveform_vector(features)
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
        if self.accepted(state, bool(changes)):
            self.store.learn(gid, ch, "waveform", signature, band, vector, ms, ACCEPT_WEIGHT)
            changes = []
        elif not changes:
            self.store.learn(gid, ch, "waveform", signature, band, vector, ms)
        transition = self.observe(state, ms, bool(changes), {
            "changes": changes, "impacts": any(c.startswith(("kurtosis", "crest")) for c in changes)})
        self.after(gid, ch, "waveform", state, transition, ms,
                   {"waveform": {"current": state["features"], "normal": state["normal"]}})

    # ------------------------------------------------------------ metrics
    def process_metrics(self, conn):
        mark = self.store.watermark("trends")
        newest = conn.execute("SELECT MAX(rowid) FROM trends").fetchone()[0] or 0
        if newest < mark:  # trends were cleared; start again
            mark = 0
        rows = conn.execute(
            "SELECT rowid, gateway_id, channel, metric_name, value, rpm, timestamp, context FROM trends "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?", (mark, BATCH * 5)).fetchall()
        for rowid, gid, ch, metric, value, rpm, stamp, context in rows:
            if gid not in self.enabled:
                continue
            try:
                if value is None or not math.isfinite(value) or value <= 0:
                    continue
                self.metric_sample(gid, ch, metric, value, self.rpm_for(gid, ch, rpm), parse_ms(stamp), context)
            except (ValueError, TypeError) as error:
                log.debug("Skipped trend %s: %s", rowid, error)
        if rows:
            self.store.set_watermark("trends", rows[-1][0])
        return len(rows)

    def metric_sample(self, gid, ch, metric, value, rpm, ms, context):
        kind = "metric:" + metric
        if metric in TREND_METRICS:
            self.store.add_hourly(gid, ch, metric, ms, value)
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
        state["normal_value"] = normal
        z = float(reference.zscores(x, METRIC_FLOOR)[0])
        ratio = value / normal
        changed = abs(z) >= CHANGE_Z and (ratio >= 1.25 or ratio <= 0.6)
        if self.accepted(state, changed):
            self.store.learn(gid, ch, kind, signature, band, x, ms, ACCEPT_WEIGHT)
            changed = False
        elif not changed:
            self.store.learn(gid, ch, kind, signature, band, x, ms)
        unit = json.loads(context or "{}").get("unit", "")
        transition = self.observe(state, ms, changed, {"value": value, "normal": normal, "unit": unit, "rpm": rpm})
        self.after(gid, ch, kind, state, transition, ms, None)

    # ------------------------------------------------------------ trends
    def update_trends(self, now_ms):
        """Refit each channel's Velocity (or Acceleration) trend once per hour."""
        hour = now_ms // 3_600_000
        if hour == self.trend_hour:
            return
        self.trend_hour = hour
        self.store.prune_hourly(now_ms - 30 * 86_400_000)
        self.trends = {}
        channels = {(g, c) for (g, c, k) in self.store._states if g in self.enabled}
        for gid, ch in channels:
            for metric in TREND_METRICS:
                points = self.store.hourly(gid, ch, metric, now_ms - TREND_DAYS * 86_400_000)
                trend = fit_trend(points)
                if trend:
                    trend.update(metric=metric, unit=self.units.get((gid, ch, metric), ""),
                                 hours=[p[0] for p in points], values=[round(p[1], 4) for p in points])
                    trend.update(self.projection(gid, ch, trend))
                    self.trends[(gid, ch)] = trend
                    break

    def projection(self, gid, ch, trend):
        """Days until the next ISO zone limit (or twice the learned normal) at the current rate."""
        info = self.info(gid, ch)
        limits = None
        if trend["metric"] == "Velocity":
            limits = self.knowledge.severity.get(info.get("iso_class"), {}).get("limits")
            if trend["unit"] != "mm/s":
                limits = None
        targets = [(f"ISO zone {zone}", limit) for zone, limit in zip("CD", (limits or [0, 0, 0])[1:])
                   if limits and trend["current"] < limit]
        normal = self.store.state(gid, ch, "metric:" + trend["metric"]).get("normal_value")
        if not limits and normal and trend["current"] < 2 * normal:
            targets = [("twice its learned normal", 2 * normal)]
        result = {"limits": limits}
        if trend["rising"] and targets:
            name, limit = targets[0]
            result.update(target=name, target_value=limit, days=round((limit - trend["current"]) / trend["slope"], 1))
        return result

    def trend_text(self, gid, ch):
        trend = self.trends.get((gid, ch))
        if not trend or not trend["rising"]:
            return ""
        text = (f" {trend['metric']} is rising {trend['slope']:.3g} {trend['unit']} per day "
                f"({trend['weekly']:+.0%} per week).")
        if trend.get("days") is not None:
            text += (f" At this rate it reaches {trend['target']} ({trend['target_value']:.3g} {trend['unit']}) "
                     f"{in_days(trend['days'])}.")
        return text

    # ------------------------------------------------------------ findings
    def observe(self, state, ms, changed, detail):
        """Report a change seen in 2 of the last 3 samples; clear it after 3 normal samples."""
        was = state.get("active", False)
        recent = (state.get("recent", []) + [bool(changed)])[-3:]
        state.update(phase="monitoring", recent=recent, last_ms=ms)
        state.pop("samples", None)
        if changed:
            state["detail"] = detail
            state.setdefault("since_ms", ms)
        if not any(recent):
            for key in ("since_ms", "detail", "report", "peak_history", "confirmed"):
                state.pop(key, None)
        # Open after 2 of 3 changed samples; once open, stay open until 3 normal samples in a row.
        state["active"] = (sum(recent) >= 2 or (was and any(recent))) and "detail" in state
        if state["active"] and not was:
            return "new"
        return "cleared" if was and not state["active"] else None

    def after(self, gid, ch, kind, state, transition, ms, evidence):
        """Write an evidence folder for each new finding and log when it clears."""
        if transition == "new":
            finding = self.describe(gid, ch, kind, state, ms)
            evidence = dict(evidence or {}, frequencies=frequency_table(self.info(gid, ch), state.get("rpm")
                                                                         or self.rpm_for(gid, ch, None)))
            folder = self.reports.finding(finding, self.info(gid, ch), evidence, when=ms / 1000)
            state["report"] = str(folder)
            self.events.append(f"New finding, report in {folder}: {finding['message'][:200]}")
        elif transition == "cleared":
            self.events.append(f"Cleared: {gid} CH{ch} {kind} is back to its learned normal.")

    def describe(self, gid, ch, kind, state, now_ms):
        info = self.info(gid, ch)
        where = f"{gid} CH{ch}"
        label = where if info["name"] == where else f"{info['name']} ({where})"
        d = state["detail"]
        severity, parts, title, fault_id = "info", [], "", None
        if kind in ("spectrum", "envelope"):
            rpm = d.get("rpm")
            for hz, rise in d["peaks"][:2]:
                order = f" ({hz / (rpm / 60):.2f}x)" if rpm else ""
                parts.append(f"peak at {hz:.1f} Hz{order} up {rise:.0f} dB")
            if kind == "spectrum" and d["floor_db"]["upper"] >= FLOOR_RISE_DB:
                parts.append(f"high-frequency noise floor up {d['floor_db']['upper']:.0f} dB")
            what = "spectrum" if kind == "spectrum" else "envelope spectrum (bearing impacts)"
            text = f"{label}: {what} changed from learned normal: " + "; ".join(parts or ["broadband rise"]) + "."
            title = f"{label}: {what} change"
            if d["matches"]:
                best = d["matches"][0]
                fault_id = best["id"]
                bearing = info.get("bearing_orders", {}).get("designation")
                bearing_fault = "bearing" in best["id"] or best["id"].startswith(("envelope_", "confirmed:"))
                name = best["name"] + (f" ({bearing})" if bearing and bearing_fault and "bearing" in best["name"].lower()
                                       else "")
                title = f"{label}: likely {name}"
                text += f" Likely {name}, {best['confidence']:.0%} match: {', '.join(best['evidence'])}. {best['advice']}"
                others = [m["name"] for m in d["matches"][1:3]]
                if others:
                    text += f" Also possible: {', '.join(others)}."
                if best["confidence"] >= 0.7:
                    severity = "warning"
            if d["peaks"] and d["peaks"][0][1] >= 12:
                severity = "warning"
            history = state.get("peak_history", [])
            if len(history) >= 4 and (history[-1][0] - history[0][0]) >= 6 * 3_600_000:
                days = (history[-1][0] - history[0][0]) / 86_400_000
                growth = (history[-1][1] - history[0][1]) / days
                if growth >= 0.5:
                    text += f" The peak is growing about {growth:.1f} dB per day."
            if not rpm:
                text += " No running speed is known, so faults cannot be named: enter a fixed RPM in the asset registry."
        elif kind == "waveform":
            if d.get("clipped"):
                title = f"{label}: clipped waveform"
                text = f"{label}: waveform is clipped. The sensor range may be too small; readings are not reliable."
            else:
                title = f"{label}: waveform change"
                text = f"{label}: waveform changed from learned normal: {', '.join(d['changes'])}."
                if d.get("impacts"):
                    text += " More impacts usually come from bearing damage, looseness or rubbing."
                    severity = "warning"
        else:
            metric = kind.split(":", 1)[1]
            ratio = d["value"] / d["normal"]
            title = f"{label}: {metric} change"
            text = (f"{label}: {metric} is {d['value']:.3g} {d['unit']}, {ratio:.1f}x its learned normal "
                    f"{d['normal']:.3g} {d['unit']}" + (f" at about {d['rpm']:.0f} RPM" if d.get("rpm") else "") + ".")
            severity = "warning" if ratio >= 2 else "info"
        text += self.trend_text(gid, ch)
        zone = self.zone(gid, ch, info)
        if zone:
            text += f" Velocity is in ISO zone {zone}."
            severity = "warning" if zone in "CD" else severity
        if state.get("confirmed"):
            confirmed = state["confirmed"]
            text += f" Confirmed by {confirmed['by'] or 'a person'}: {confirmed['fault']}."
            severity = "warning"
        days = (now_ms - state.get("since_ms", now_ms)) / 86_400_000
        if days >= 3:
            text += (" Changed for %d days; if this is the new normal after maintenance, use Relearn or Repaired "
                     "in the asset registry or the spectrum view." % days)
        score = max([p[1] for p in d.get("peaks", [])] + [abs(math.log(d["value"] / d["normal"])) * 10
                                                           if "value" in d else 0])
        return {"severity": severity, "gateway_id": gid, "channel": ch, "kind": kind, "score": round(score, 1),
                "title": title, "message": text, "since_ms": state.get("since_ms"), "report": state.get("report"),
                "fault_id": fault_id, "confirmed": bool(state.get("confirmed")), "detail": d}

    def zone(self, gid, ch, info):
        value, context = self.velocity.get((gid, ch), (None, None))
        if value is None:
            return None
        meta = json.loads(context or "{}")
        return self.knowledge.iso_zone(info, value, meta.get("unit"), meta.get("detection"))

    def trend_finding(self, gid, ch):
        trend = self.trends.get((gid, ch))
        if not trend or not trend["rising"] or trend.get("days") is None or trend["days"] > TREND_HORIZON_DAYS:
            return None
        info = self.info(gid, ch)
        label = f"{info['name']} ({gid} CH{ch})" if info["name"] != f"{gid} CH{ch}" else f"{gid} CH{ch}"
        return {"severity": "warning" if trend["days"] <= 14 else "info", "gateway_id": gid, "channel": ch,
                "kind": "trend", "score": round(100 / max(trend["days"], 1), 1),
                "title": f"{label}: {trend['metric']} rising",
                "message": f"{label}:" + self.trend_text(gid, ch) + " Plan an inspection before then."}

    def progress(self, channels):
        rows = []
        for (gid, ch) in sorted(channels):
            states = {k[2]: s for k, s in self.store.states() if k[:2] == (gid, ch)} if self.store else {}
            main = states.get("spectrum") or states.get("waveform") or next(iter(states.values()), {})
            info = self.info(gid, ch)
            notes = []
            if info.get("bearing_missing"):
                notes.append(f"Bearing {info['bearing_missing']} is not in the bearing library; "
                             "add it in the asset registry.")
            if "spectrum" in states and not states["spectrum"].get("rpm"):
                notes.append("This VDS reports no speed. Choose a fixed speed and enter the RPM.")
            if states.get("waveform", {}).get("envelope_note"):
                notes.append(states["waveform"]["envelope_note"] + ".")
            trend = self.trends.get((gid, ch))
            trend_text = "Not enough history yet"
            if trend:
                trend_text = (f"{trend['metric']} rising ({trend['weekly']:+.0%}/week)" if trend["rising"]
                              else f"{trend['metric']}: no steady rise")
                if trend.get("days") is not None:
                    trend_text += f", {trend['target']} {in_days(trend['days'])}"
            rows.append({"gateway_id": gid, "channel": ch, "name": info["name"],
                         "phase": main.get("phase", "waiting for data"),
                         "samples": main.get("samples"), "needed_samples": self.learn_samples,
                         "hours": main.get("hours"), "needed_hours": self.learn_hours, "trend": trend_text,
                         "changes": sum(1 for s in states.values() if s.get("active")), "notes": notes})
        return rows

    def report(self, now_ms, processed, backlog, elapsed):
        findings, channels = [], {}
        if self.store and self.storage_status["state"] != "ssd_missing":
            for (gid, ch, kind), state in self.store.states():
                if gid not in self.enabled:
                    continue
                channels.setdefault((gid, ch), set()).add(state.get("phase"))
                if state.get("active") and now_ms - state.get("last_ms", 0) <= MAX_AGE_S * 1000:
                    findings.append(self.describe(gid, ch, kind, state, now_ms))
        reported = {(f["gateway_id"], f["channel"]) for f in findings}
        for (gid, ch) in list(self.trends):
            finding = self.trend_finding(gid, ch) if (gid, ch) not in reported and gid in self.enabled else None
            if finding:
                findings.append(finding)
                reported.add((gid, ch))
        for (gid, ch) in self.velocity:
            if gid not in self.enabled or (gid, ch) in reported:
                continue
            info = self.info(gid, ch)
            zone = self.zone(gid, ch, info)
            if zone in ("C", "D"):
                value = self.velocity[(gid, ch)][0]
                findings.append({"severity": "warning", "gateway_id": gid, "channel": ch, "kind": "severity",
                                 "score": value, "title": f"{info['name']}: ISO zone {zone}",
                                 "message": f"{info['name']} ({gid} CH{ch}): Velocity {value:.2f} mm/s is in ISO "
                                 f"zone {zone} ({'restricted operation' if zone == 'C' else 'damage likely'})."})
        findings.sort(key=lambda f: (f["severity"] != "warning", -f["score"]))
        for (gid, ch) in self.ai:
            if gid in self.enabled:
                channels.setdefault((gid, ch), set())
        learning = sum(1 for p in channels.values() if "monitoring" not in p)
        if not self.enabled:
            summary = "AI analysis is off for every VDS. Turn it on per VDS in the asset registry."
        else:
            summary = (f"Monitoring {len(channels) - learning} channels, still learning {learning}; "
                       f"{processed} new samples analysed in {elapsed:.1f} s.")
        if self.storage_status["state"] == "ssd_missing":
            summary = self.storage_status["message"]
        if backlog >= BEHIND_WARNING:
            findings.insert(0, {"severity": "info", "score": 0, "title": "AI advisor is behind", "message": (
                f"AI advisor is {backlog} samples behind. It is catching up quietly in the background.")})
        bearings = sorted({b["designation"] for b in self.knowledge.bearings.values()})
        self.channels = channels
        return {"generated_at": time.time(), "summary": summary, "backlog": backlog,
                "channels": {"monitoring": len(channels) - learning, "learning": learning},
                "enabled_gateways": sorted(self.enabled), "storage": self.storage_status,
                "progress": self.progress(channels)[:500], "bearings": bearings,
                "findings": findings[:MAX_FINDINGS]}

    def publish_tables(self):
        """Fault frequency tables and trends (tables.json), rewritten only when they change."""
        tables, trends = {}, {}
        for (gid, ch) in sorted(getattr(self, "channels", {})):
            info = self.info(gid, ch)
            rpm = (self.store.state(gid, ch, "spectrum").get("rpm") if self.store else None) or self.rpm_for(gid, ch, None)
            tables[f"{gid}/{ch}"] = {"rpm": rpm, "bearing": info.get("bearing_orders", {}).get("designation"),
                                     "rows": frequency_table(info, rpm)}
            if (gid, ch) in self.trends:
                trends[f"{gid}/{ch}"] = {k: v for k, v in self.trends[(gid, ch)].items()}
        text = json.dumps({"tables": tables, "trends": trends}, sort_keys=True, default=float)
        if text != self.tables_text:
            self.tables_text = text
            atomic_json(self.local / "tables.json", json.loads(text))

    def close(self):
        if self.store:
            self.store.close()


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
    parser.add_argument("--max-rate", type=float, default=20, help="Most learning samples scanned per second")
    parser.add_argument("--learn-samples", type=int, default=48, help="Samples needed before monitoring starts")
    parser.add_argument("--learn-hours", type=float, default=12, help="Hours of operation needed before monitoring")
    parser.add_argument("--report-days", type=int, default=180, help="Days of report folders to keep")
    parser.add_argument("--no-ssd", action="store_true", help="Keep AI data on the eMMC even if an SSD is mounted")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show learning progress per channel and exit")
    parser.add_argument("--relearn", metavar="GATEWAY/CHANNEL", help="Forget what was learned for one channel")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    advisor = Advisor(args.data_dir, args.knowledge, args.learn_samples, args.learn_hours, args.max_rate,
                      use_ssd=not args.no_ssd, report_days=args.report_days)
    if args.status or args.relearn:
        if advisor.store is None:
            raise SystemExit("AI data is on an SSD that is not mounted.")
        if args.relearn:
            gid, _, ch = args.relearn.rpartition("/")
            advisor.store.forget(gid, ch)
            print(f"Forgot learning for {gid} CH{ch}; it relearns from the next samples.")
        else:
            print(status(advisor))
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
            advisor.close()
            return 0
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
