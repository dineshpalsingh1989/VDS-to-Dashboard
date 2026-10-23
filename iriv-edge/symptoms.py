"""Match spectrum and waveform changes against the fault library in knowledge/.

Everything here is deterministic and explainable: each suggested fault lists
the evidence that matched, so a person can check it on the diagnostics page.
"""

import csv
import json
import math
import re
from pathlib import Path

import numpy as np

BEARING_KEYS = ("bpfo", "bpfi", "bsf", "ftf")
CHANGE_TYPES = {"rise", "harmonics", "floor_rise", "nonsync_rise", "waveform_rise"}
EXPRESSION = re.compile(r"^(\d+(?:\.\d+)?)?(x|line|bpfo|bpfi|bsf|ftf|gmf|bpf)$")


def bearing_orders(balls, ball_diameter, pitch_diameter, contact_angle_deg=0.0):
    """Defect frequencies as multiples of shaft speed (stationary outer ring)."""
    r = ball_diameter / pitch_diameter * math.cos(math.radians(contact_angle_deg))
    return {
        "bpfo": balls / 2 * (1 - r),
        "bpfi": balls / 2 * (1 + r),
        "bsf": pitch_diameter / (2 * ball_diameter) * (1 - r * r),
        "ftf": 0.5 * (1 - r),
    }


def designation_keys(text):
    text = str(text).strip().upper()
    return {text, text.split("-")[0]} - {""}


def load_bearings(path):
    bearings = {}
    if not Path(path).exists():
        return bearings
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            try:
                if all(row.get(k) for k in BEARING_KEYS):
                    orders = {k: float(row[k]) for k in BEARING_KEYS}
                else:
                    orders = bearing_orders(int(row["balls"]), float(row["ball_diameter_mm"]),
                                            float(row["pitch_diameter_mm"]), float(row.get("contact_angle_deg") or 0))
            except (KeyError, ValueError):
                continue
            orders["designation"] = row["designation"]
            for key in designation_keys(row["designation"]):
                bearings.setdefault(key, orders)
    return bearings


class Knowledge:
    def __init__(self, folder):
        folder = Path(folder)
        self.faults = json.loads((folder / "faults.json").read_text(encoding="utf-8"))["faults"]
        self.bearings = load_bearings(folder / "bearings.csv")
        severity = folder / "severity.json"
        self.severity = json.loads(severity.read_text(encoding="utf-8"))["classes"] if severity.exists() else {}
        machines = folder / "machines.json"
        config = json.loads(machines.read_text(encoding="utf-8")) if machines.exists() else {}
        self.defaults = config.get("defaults", {})
        self.channels = config.get("channels", {})

    def machine(self, gid, ch, asset=None):
        """Machine facts for a channel: machines.json first, then the dashboard asset registry."""
        asset = asset or {}
        info = dict(self.defaults)
        info.update(self.channels.get(f"{gid}/{ch}", {}))
        label = " / ".join(asset.get(k) for k in ("site", "machine", "location") if asset.get(k))
        info.setdefault("name", label or f"{gid} CH{ch}")
        if asset.get("direction"):
            info.setdefault("direction", asset["direction"])
        if "bearing" not in info:
            # Typing the bearing number into the asset registry location also works.
            for token in re.split(r"[\s,;/()]+", f"{asset.get('machine', '')} {asset.get('location', '')}"):
                if token and designation_keys(token) & self.bearings.keys():
                    info["bearing"] = token
                    break
        bearing = info.get("bearing")
        if isinstance(bearing, dict):
            try:
                info["bearing_orders"] = {k: float(bearing[k]) for k in BEARING_KEYS}
                info["bearing_orders"]["designation"] = bearing.get("designation", "custom")
            except (KeyError, TypeError, ValueError):
                pass
        elif bearing:
            for key in designation_keys(bearing):
                if key in self.bearings:
                    info["bearing_orders"] = self.bearings[key]
                    break
        return info

    def iso_zone(self, info, velocity, unit, detection):
        limits = self.severity.get(info.get("iso_class"), {}).get("limits")
        if not limits or unit != "mm/s" or str(detection).upper() != "RMS" or velocity is None:
            return None
        return "ABCD"[sum(velocity > limit for limit in limits)]


class SpectrumView:
    """The current spectrum next to its learned normal, in profile bins."""

    def __init__(self, freqs, current, normal, rpm, waveform=None, waveform_normal=None):
        self.freqs, self.x, self.mean = freqs, current, normal
        self.rpm = rpm if rpm and rpm > 0 else None
        self.width = freqs[1] - freqs[0] if len(freqs) > 1 else freqs[0]
        self.rise = 10 * (current - normal)
        self.waveform, self.waveform_normal = waveform, waveform_normal

    def window(self, hz):
        if hz is None or hz <= 0 or hz > self.freqs[-1] + self.width:
            return None
        tolerance = max(1.5 * self.width, 0.02 * hz)
        idx = np.nonzero(np.abs(self.freqs - hz) <= tolerance)[0]
        return idx if idx.size else None

    def measure(self, hz):
        """(rise dB versus normal, prominence dB over the local floor) near hz."""
        idx = self.window(hz)
        if idx is None:
            return None
        peak = idx[np.argmax(self.x[idx])]
        lo, hi = max(0, idx[0] - 25), min(len(self.x), idx[-1] + 26)
        around = np.r_[self.x[lo:idx[0]], self.x[idx[-1] + 1:hi]]
        floor = np.median(around) if around.size else np.median(self.x)
        return float(self.rise[idx].max()), float(10 * (self.x[peak] - floor))

    def band_rise(self, band):
        part = self.rise if band == "all" else self.rise[len(self.rise) // 2:]
        return float(np.median(part))

    def changed_peaks(self, min_db=6.0, min_prominence=6.0, limit=5):
        peaks = []
        for i in np.argsort(self.rise)[::-1][:50]:
            if self.rise[i] < min_db:
                break
            if any(abs(i - j) <= 2 for j, *_ in peaks):
                continue
            measured = self.measure(self.freqs[i])
            if measured and measured[1] >= min_prominence:
                peaks.append((int(i), float(self.freqs[i]), float(self.rise[i])))
            if len(peaks) >= limit:
                break
        return peaks


def frequency(expression, view, info):
    match = EXPRESSION.match(str(expression))
    if not match:
        raise ValueError(f"Unknown frequency {expression!r} in faults.json")
    factor = float(match.group(1) or 1)
    token = match.group(2)
    shaft = view.rpm / 60 if view.rpm else None
    if token == "line":
        base = info.get("line_frequency_hz")
    elif token == "x":
        base = shaft
    elif token in BEARING_KEYS:
        orders = info.get("bearing_orders")
        base = shaft * orders[token] if shaft and orders else None
    elif token == "gmf":
        base = shaft * info["gear_teeth"] if shaft and info.get("gear_teeth") else None
    else:  # bpf
        base = shaft * info["blades"] if shaft and info.get("blades") else None
    return factor * base if base else None


def is_synchronous(hz, view, info):
    """True when hz is a half-order of running speed or a multiple of line frequency."""
    shaft = view.rpm / 60
    order = hz / shaft
    # 1.2 % allows for RPM measurement error at higher orders, yet keeps 3.6x apart from 3.5x.
    if abs(order * 2 - round(order * 2)) * shaft / 2 <= max(1.5 * view.width, 0.012 * hz):
        return True
    line = info.get("line_frequency_hz")
    return bool(line) and abs(hz / line - round(hz / line)) * line <= max(1.5 * view.width, 0.01 * hz)


def check(item, view, info):
    """Return (matched, text) or (None, '') when the evidence cannot be evaluated."""
    kind = item["type"]
    if kind == "direction":
        direction = str(info.get("direction", "")).lower()
        if not direction:
            return None, ""
        ok = any(word in direction for word in item["any"])
        return ok, f"{direction} sensor" if ok else ""
    if kind in ("waveform", "waveform_rise"):
        if not view.waveform:
            return None, ""
        value = view.waveform.get(item["feature"])
        if kind == "waveform_rise":
            if not view.waveform_normal:
                return None, ""
            normal = view.waveform_normal.get(item["feature"])
            ok = value - normal >= item["min_change"]
            return ok, f"{item['feature']} {normal:.1f} to {value:.1f}" if ok else ""
        ok = value >= item.get("min", -math.inf) and value <= item.get("max", math.inf)
        return ok, f"{item['feature']} {value:.1f}" if ok else ""
    if kind == "floor_rise":
        rise = view.band_rise(item["band"])
        ok = rise >= item["min_db"]
        where = "high-frequency" if item["band"] == "upper" else "broadband"
        return ok, f"{where} noise floor up {rise:.0f} dB" if ok else ""
    if kind == "nonsync_rise":
        if not view.rpm:
            return None, ""
        for _, hz, rise in view.changed_peaks(item["min_db"]):
            if not is_synchronous(hz, view, info):
                return True, f"new non-synchronous peak at {hz:.1f} Hz ({hz / (view.rpm / 60):.2f}x), up {rise:.0f} dB"
        return False, ""

    hz = frequency(item.get("at") or item.get("of"), view, info)
    if hz is None:
        return None, ""
    label = item.get("at") or item.get("of")
    if kind == "rise":
        measured = view.measure(hz)
        if measured is None:
            return None, ""
        ok = measured[0] >= item["min_db"] and measured[1] >= 3
        return ok, f"{label} ({hz:.1f} Hz) up {measured[0]:.0f} dB" if ok else ""
    if kind == "present":
        measured = view.measure(hz)
        if measured is None:
            return None, ""
        ok = measured[1] >= item["min_prominence_db"]
        return ok, f"clear peak at {label}" if ok else ""
    if kind == "ratio":
        a, b = view.window(hz), view.window(frequency(item["to"], view, info))
        if a is None or b is None:
            return None, ""
        ratio = 10 ** ((view.x[a].max() - view.x[b].max()) / 2)  # power to amplitude
        ok = ratio >= item.get("min", 0) and ratio <= item.get("max", math.inf)
        return ok, f"{label}/{item['to']} amplitude ratio {ratio:.2f}" if ok else ""
    if kind == "harmonics":
        found = 0
        for k in range(1, 11):
            measured = view.measure(k * hz)
            if measured is None:
                break
            found += measured[1] >= item["min_prominence_db"]
        ok = found >= item["count"]
        return ok, f"{found} harmonics of {label}" if ok else ""
    if kind == "sidebands":
        spacing = frequency(item["spacing"], view, info)
        if spacing is None:
            return None, ""
        sides = [view.measure(hz + sign * spacing) for sign in (-1, 1)]
        if any(s is None for s in sides):
            return None, ""
        ok = all(s[1] >= item["min_prominence_db"] for s in sides)
        return ok, f"{item['spacing']} sidebands around {label}" if ok else ""
    raise ValueError(f"Unknown evidence type {kind!r} in faults.json")


def requirements_met(fault, view, info):
    for need in fault.get("requires", []):
        if need == "rpm" and not view.rpm:
            return False
        if need == "bearing" and not info.get("bearing_orders"):
            return False
        if need == "line_frequency" and not info.get("line_frequency_hz"):
            return False
        if need in ("gear_teeth", "blades") and not info.get(need):
            return False
    if "bearing" in fault.get("unless", []) and info.get("bearing_orders"):
        return False
    types = fault.get("machine_types")
    return not types or info.get("machine_type") in types


def diagnose(knowledge, view, info, min_score=0.6):
    """Faults whose symptoms match the current change, best first."""
    matches = []
    for fault in knowledge.faults:
        if not requirements_met(fault, view, info):
            continue
        total = score = 0.0
        texts, key_ok, changed = [], True, False
        for item in fault["evidence"]:
            ok, text = check(item, view, info)
            if ok is None:
                key_ok = key_ok and not item.get("key")
                continue
            if item.get("negate"):
                ok, text = not ok, ""
            total += item.get("weight", 1)
            if ok:
                score += item.get("weight", 1)
                if text:
                    texts.append(text)
                changed = changed or (item["type"] in CHANGE_TYPES and not item.get("negate"))
            elif item.get("key"):
                key_ok = False
        if total and key_ok and changed and score / total >= fault.get("min_score", min_score):
            matches.append({"id": fault["id"], "name": fault["name"], "confidence": round(score / total, 2),
                            "evidence": texts, "advice": fault.get("advice", "")})
    return sorted(matches, key=lambda m: -m["confidence"])
