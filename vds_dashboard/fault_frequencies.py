"""Bearing library and machine fault frequencies, shared by the dashboard and the IRIV AI advisor.

The library is iriv-edge/knowledge/bearings.csv plus bearings added in the
dashboard. Defect frequencies are stored as multiples of shaft speed, so they
turn into Hz at whatever speed the machine runs.
"""

import csv
import math
import re
from pathlib import Path

BEARING_KEYS = ("bpfo", "bpfi", "bsf", "ftf")
LIBRARY = Path(__file__).resolve().parent.parent / "iriv-edge" / "knowledge" / "bearings.csv"
EXPRESSION = re.compile(r"^(\d+(?:\.\d+)?)?(x|line|bpfo|bpfi|bsf|ftf|gmf|bpf)$")

# Rows of the fault frequency table: (expression, name, what it detects)
TABLE = (
    ("0.5x", "Half running speed", "Looseness, rub"),
    ("1x", "Running speed", "Unbalance"),
    ("2x", "2x running speed", "Misalignment, looseness"),
    ("3x", "3x running speed", "Misalignment, looseness"),
    ("2line", "2x mains frequency", "Motor electrical faults"),
    ("ftf", "Cage (FTF)", "Bearing cage defect"),
    ("bsf", "Ball spin (BSF)", "Rolling element defect"),
    ("2bsf", "2x ball spin", "Rolling element defect"),
    ("bpfo", "Outer race (BPFO)", "Outer race defect"),
    ("2bpfo", "2x outer race", "Outer race defect"),
    ("bpfi", "Inner race (BPFI)", "Inner race defect, with 1x sidebands"),
    ("2bpfi", "2x inner race", "Inner race defect"),
    ("bpf", "Blade/vane pass", "Impeller or fan problems"),
    ("2bpf", "2x blade/vane pass", "Impeller or fan problems"),
    ("gmf", "Gear mesh", "Gear wear or misalignment, with 1x sidebands"),
    ("2gmf", "2x gear mesh", "Gear wear"),
)

# Marker families for the spectrum plot: (key, label, colour, [(token, short name)])
FAMILIES = (
    ("speed", "Running speed", "#6c757d", [("x", "x")]),
    ("bearing", "Bearing", "#d62728", [("bpfo", "BPFO"), ("bpfi", "BPFI"), ("bsf", "BSF"), ("ftf", "FTF")]),
    ("gear", "Gear / blade pass", "#2ca02c", [("gmf", "GMF"), ("bpf", "BPF")]),
    ("electrical", "Electrical", "#1f77b4", [("line", "Line")]),
)
FAMILY_COLOURS = {"BPFO": "#d62728", "BPFI": "#ff7f0e", "BSF": "#9467bd", "FTF": "#8c564b"}
# Fault lines that belong to the part a sensor is on: a motor has no gear mesh or blade pass,
# a pump or fan has no gear mesh, and mains (electrical) lines belong to motors.
PART_FAMILIES = {"motor": ("speed", "bearing", "electrical"), "pump": ("speed", "bearing", "gear"),
                 "fan": ("speed", "bearing", "gear"), "compressor": ("speed", "bearing", "gear"),
                 "gearbox": ("speed", "bearing", "gear")}
GEAR_LABELS = {"gearbox": "Gear mesh", "pump": "Vane pass", "fan": "Blade pass", "compressor": "Vane / blade pass"}


def families_for(part):
    """(key, label) of the fault line families that make sense for the part; all of them when unknown."""
    keys = PART_FAMILIES.get(part or "", tuple(key for key, *_ in FAMILIES))
    return [(key, GEAR_LABELS.get(part, label) if key == "gear" else label) for key, label, _c, _t in FAMILIES
            if key in keys]


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
    """Lookup keys for a bearing number; seals and clearance suffixes do not change the frequencies.

    "6205-2RS", "6205ZZ", "6205 DDU C3" and "6205" all find the 6205.
    """
    text = re.sub(r"\s+", "", str(text or "")).upper()
    keys = {text, text.split("-")[0]}
    base = re.match(r"^([A-Z]{0,4}\d{3,6})", text)
    if base:
        keys.add(base.group(1))
    return {key for key in keys if len(key) >= 3}


def load_bearings(path=LIBRARY, extra=()):
    """Library rows keyed by every lookup key. Entries in extra (added in the dashboard) win."""
    bearings = {}
    rows = []
    if Path(path).exists():
        with open(path, newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    for row in rows:
        try:
            if all(row.get(k) for k in BEARING_KEYS):
                orders = {k: float(row[k]) for k in BEARING_KEYS}
            else:
                orders = bearing_orders(int(row["balls"]), float(row["ball_diameter_mm"]),
                                        float(row["pitch_diameter_mm"]), float(row.get("contact_angle_deg") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        orders.update(designation=row["designation"], source=row.get("source", ""))
        for key in designation_keys(row["designation"]):
            bearings.setdefault(key, orders)
    for entry in extra:
        try:
            orders = {k: float(entry[k]) for k in BEARING_KEYS}
        except (KeyError, TypeError, ValueError):
            continue
        orders.update(designation=entry.get("designation", ""), source=entry.get("source", "Added in the dashboard"))
        for key in designation_keys(orders["designation"]):
            bearings[key] = orders
    return bearings


def find_bearing(bearings, text):
    """The library entry for what a user typed, or None."""
    for key in sorted(designation_keys(text), key=len, reverse=True):
        if key in bearings:
            return bearings[key]
    return None


def frequency_hz(token, rpm, info, factor=1.0):
    """Hz of x, line, bpfo, bpfi, bsf, ftf, gmf or bpf for one machine, or None if unknown."""
    shaft = rpm / 60 if rpm and rpm > 0 else None
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


def expression_hz(expression, rpm, info):
    match = EXPRESSION.match(str(expression))
    if not match:
        raise ValueError(f"Unknown frequency {expression!r}")
    return frequency_hz(match.group(2), rpm, info, float(match.group(1) or 1))


def frequency_table(info, rpm):
    """Every fault frequency of one machine at its current speed, lowest first."""
    rows = []
    for expression, name, used_for in TABLE:
        hz = expression_hz(expression, rpm, info)
        if hz:
            rows.append({"name": name, "hz": round(hz, 2), "order": round(hz * 60 / rpm, 3) if rpm else None,
                         "used_for": used_for})
    return sorted(rows, key=lambda row: row["hz"])


def marker_lines(info, rpm, families, harmonics, fmax):
    """Fault frequency markers for a spectrum plot: [(family key, label, Hz, colour)]."""
    lines = []
    allowed = {key for key, _label in families_for(info.get("machine_type"))}
    for key, _label, colour, tokens in FAMILIES:
        if key not in families or key not in allowed:
            continue
        for token, short in tokens:
            count = max(harmonics, 1) if token != "line" else 3
            for k in range(1, count + 1):
                factor = 2 * k if token == "line" else k
                hz = frequency_hz(token, rpm, info, factor)
                if hz and hz <= fmax:
                    name = (f"{k}x" if token == "x" else f"{factor}x {short}" if token == "line"
                            else short if k == 1 else f"{k}x{short}")
                    lines.append((key, name, hz, FAMILY_COLOURS.get(short, colour)))
    return lines


def nearest_fault(hz, lines, tolerance=0.02):
    """Name of the marker within 2 % (or 1.5 Hz) of hz, if any."""
    best = None
    for _key, name, line_hz, _colour in lines:
        distance = abs(hz - line_hz)
        if distance <= max(tolerance * line_hz, 1.5) and (best is None or distance < best[0]):
            best = (distance, name)
    return best[1] if best else ""
