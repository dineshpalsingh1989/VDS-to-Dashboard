"""Report folders the user can open to see what the AI did and why.

<root>/reports/YYYY-MM-DD/
    activity.log                          one line per cycle that did work
    HHMMSS_<gateway>_CH<n>_<kind>/        one folder per new finding
        report.md                         plain-language report
        finding.json                      the finding as data
        spectrum.csv                      current vs learned normal, per frequency
        waveform.json                     waveform features vs normal

The dashboard shows the latest reports from <data>/ai/index.json, which is kept
next to the dashboard even when the folders themselves live on the SSD.
"""

import csv
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

INDEX_SIZE = 50


def atomic_json(path, data):
    path = Path(path)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=1, default=str)
        os.chmod(temporary, 0o644)  # the dashboard container reads it as another user
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def safe(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text))[:40] or "x"


def markdown(finding, info):
    when = datetime.fromtimestamp(finding.get("created", time.time())).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {finding['title']}",
        "",
        f"- **When:** {when}",
        f"- **Machine:** {info.get('name', '')}",
        f"- **Sensor:** {finding['gateway_id']} channel {finding['channel']}",
        f"- **Severity:** {finding['severity']}",
        f"- **Check type:** {finding['kind']}",
    ]
    if info.get("bearing"):
        lines.append(f"- **Bearing:** {info['bearing'] if isinstance(info['bearing'], str) else 'custom'}")
    lines += ["", "## What the AI saw", "", finding["message"], ""]
    matches = (finding.get("detail") or {}).get("matches") or []
    if matches:
        lines += ["## Likely causes", "", "| Fault | Match | Evidence |", "|---|---|---|"]
        lines += [f"| {m['name']} | {m['confidence']:.0%} | {', '.join(m['evidence'])} |" for m in matches]
        lines += ["", "## What to check", ""] + [f"- **{m['name']}:** {m['advice']}" for m in matches] + [""]
    lines += [
        "## Files in this folder",
        "",
        "- `finding.json`: this finding as data.",
        "- `spectrum.csv` or `envelope.csv`: current and learned-normal level per frequency.",
        "- `fault_frequencies.csv`: every fault frequency for this machine at its running speed.",
        "- `waveform.json`: waveform features against normal, if a waveform was involved.",
        "",
        "This report is advisory. Confirm with an inspection before maintenance.",
    ]
    return "\n".join(lines) + "\n"


class Reports:
    def __init__(self, root, index_dir, keep_days=180):
        self.root, self.index_path = Path(root), Path(index_dir) / "index.json"
        self.keep_days = keep_days
        self._pruned_day = None

    def day_dir(self, when=None):
        folder = self.root / "reports" / datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def activity(self, text, when=None):
        when = when or time.time()
        with open(self.day_dir(when) / "activity.log", "a", encoding="utf-8") as stream:
            stream.write(f"{datetime.fromtimestamp(when):%H:%M:%S} {text}\n")

    def finding(self, finding, info, evidence=None, when=None):
        when = when or time.time()
        name = f"{datetime.fromtimestamp(when):%H%M%S}_{safe(finding['gateway_id'])}_CH{finding['channel']}_{safe(finding['kind'])}"
        folder = self.day_dir(when) / name
        suffix = 1
        while folder.exists():
            suffix += 1
            folder = folder.with_name(f"{name}_{suffix}")
        folder.mkdir()
        finding = dict(finding, created=when, folder=str(folder))
        (folder / "report.md").write_text(markdown(finding, info), encoding="utf-8")
        atomic_json(folder / "finding.json", finding)
        evidence = evidence or {}
        if "freqs" in evidence:
            rows = np.column_stack([evidence["freqs"], 10 * evidence["current"], 10 * evidence["normal"],
                                    10 * (evidence["current"] - evidence["normal"])])
            np.savetxt(folder / f"{evidence.get('name', 'spectrum')}.csv", rows, delimiter=",", fmt="%.4f",
                       header="frequency_hz,current_db,normal_db,change_db", comments="")
        if "waveform" in evidence:
            atomic_json(folder / "waveform.json", evidence["waveform"])
        if evidence.get("frequencies"):
            with open(folder / "fault_frequencies.csv", "w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frequency_hz", "multiple_of_running_speed", "name", "used_to_detect"])
                for row in evidence["frequencies"]:
                    writer.writerow([row["hz"], "" if row["order"] is None else row["order"], row["name"],
                                     row["used_for"]])
        self._index(finding, folder)
        return folder

    def _index(self, finding, folder):
        try:
            entries = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        entry = {key: finding.get(key) for key in ("gateway_id", "channel", "kind", "severity", "title", "created")}
        entry["folder"] = str(folder)
        entry["markdown"] = (folder / "report.md").read_text(encoding="utf-8")[:12000]
        atomic_json(self.index_path, ([entry] + entries)[:INDEX_SIZE])

    def prune(self, now=None):
        """Remove day folders older than keep_days, once per day."""
        today = datetime.fromtimestamp(now or time.time()).date()
        if self._pruned_day == today or self.keep_days <= 0:
            return
        self._pruned_day = today
        cutoff = today - timedelta(days=self.keep_days)
        for folder in (self.root / "reports").glob("????-??-??"):
            try:
                if datetime.strptime(folder.name, "%Y-%m-%d").date() < cutoff:
                    shutil.rmtree(folder)
            except (ValueError, OSError):
                continue
