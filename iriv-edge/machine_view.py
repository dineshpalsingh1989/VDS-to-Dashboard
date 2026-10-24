"""Compare the sensors on one machine with each other.

A single sensor sees a level; several sensors on the same machine show where the
vibration is strongest and in which direction, which points at the cause:

* axial nearly as high as radial at the same bearing: misalignment or a bent shaft;
* vertical far above horizontal at the same bearing: looseness, soft foot or a weak base;
* one end far above the other: the source sits at that end (coupling side or the free end).

Inputs are overall velocity RMS readings in mm/s. Deterministic and advisory only.
"""

AXIAL_RATIO = 0.75      # axial / radial at the same bearing
VERTICAL_RATIO = 2.0    # vertical / horizontal at the same bearing
END_RATIO = 2.0         # one end / the other end, same part and direction
STRONG = {"axial": 1.0, "vertical": 3.0, "end": 3.0}
MIN_LEVEL = 0.5         # mm/s; below this, ratios are noise
PARTS = {"motor": "motor", "pump": "pump", "fan": "fan", "gearbox": "gearbox", "compressor": "compressor",
         "other": "machine"}
END_NAMES = {"DE": "drive end", "NDE": "non-drive end"}


def label(point):
    return f"{PARTS.get(point['part'], point['part'] or 'machine')} {END_NAMES.get(point['end'], '')}".strip()


def compare(points):
    """points: dicts with gid, ch, part, end, direction (horizontal/vertical/axial) and velocity (mm/s).

    Returns notes: {"kind", "supports", "ratio", "strong", "channels", "text"}.
    """
    usable = [p for p in points if p.get("velocity") is not None and p.get("direction") in ("horizontal", "vertical", "axial")]
    notes = []
    bearings = {}
    for p in usable:
        bearings.setdefault((p.get("part"), p.get("end")), {}).setdefault(p["direction"], p)
    for (part, end), by_direction in bearings.items():
        if not end:
            continue
        axial, radial = by_direction.get("axial"), [by_direction[d] for d in ("horizontal", "vertical") if d in by_direction]
        if axial and radial:
            top = max(radial, key=lambda p: p["velocity"])
            if max(axial["velocity"], top["velocity"]) >= MIN_LEVEL and top["velocity"] > 0:
                ratio = axial["velocity"] / top["velocity"]
                if ratio >= AXIAL_RATIO:
                    notes.append({"kind": "axial", "supports": "misalignment", "ratio": round(ratio, 2),
                                  "strong": ratio >= STRONG["axial"], "channels": [_key(axial), _key(top)],
                                  "text": f"At the {label(axial)}, axial vibration is {axial['velocity']:.2f} mm/s "
                                          f"(CH{axial['ch']}), {ratio:.0%} of the {top['direction']} "
                                          f"{top['velocity']:.2f} mm/s (CH{top['ch']}). High axial vibration points to "
                                          "misalignment or a bent shaft."})
        h, v = by_direction.get("horizontal"), by_direction.get("vertical")
        if h and v and v["velocity"] >= MIN_LEVEL and h["velocity"] > 0:
            ratio = v["velocity"] / h["velocity"]
            if ratio >= VERTICAL_RATIO:
                notes.append({"kind": "vertical", "supports": "looseness", "ratio": round(ratio, 2),
                              "strong": ratio >= STRONG["vertical"], "channels": [_key(v), _key(h)],
                              "text": f"At the {label(v)}, vertical vibration is {ratio:.1f} times horizontal "
                                      f"({v['velocity']:.2f} against {h['velocity']:.2f} mm/s). Machines are usually "
                                      "stiffer vertically, so this points to looseness, soft foot or a weak base."})
    by_part = {}
    for p in usable:
        if p.get("end") in END_NAMES:
            by_part.setdefault((p.get("part"), p["direction"]), {})[p["end"]] = p
    for (part, direction), ends in by_part.items():
        de, nde = ends.get("DE"), ends.get("NDE")
        if not (de and nde) or max(de["velocity"], nde["velocity"]) < MIN_LEVEL or min(de["velocity"], nde["velocity"]) <= 0:
            continue
        high, low = (de, nde) if de["velocity"] >= nde["velocity"] else (nde, de)
        ratio = high["velocity"] / low["velocity"]
        if ratio < END_RATIO:
            continue
        where = ("near the coupling: check alignment and the coupling" if high["end"] == "DE"
                 else f"at the free end: check the {PARTS.get(part, 'machine')}'s non-drive end bearing"
                 + (" and cooling fan" if part == "motor" else ""))
        notes.append({"kind": "end", "supports": "coupling side" if high["end"] == "DE" else "free end",
                      "ratio": round(ratio, 2), "strong": ratio >= STRONG["end"], "channels": [_key(high), _key(low)],
                      "text": f"On the {PARTS.get(part, 'machine')}, the {END_NAMES[high['end']]} ({direction}) is "
                              f"{ratio:.1f} times the {END_NAMES[low['end']]} ({high['velocity']:.2f} against "
                              f"{low['velocity']:.2f} mm/s). The source is {where}."})
    return notes


def _key(point):
    return [point["gid"], point["ch"]]
