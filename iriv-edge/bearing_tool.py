#!/usr/bin/env python3
"""Add a bearing to knowledge/bearings.csv and show its defect frequencies.

From the manufacturer's catalogue geometry:
    python3 bearing_tool.py 6206-2RS --balls 9 --ball-diameter 9.525 --pitch-diameter 46 --source "catalogue"
Or straight from published defect frequencies (multiples of shaft speed):
    python3 bearing_tool.py 22216 --orders 7.62 10.38 3.21 0.42 --source "manufacturer calculator"
Show the frequencies at a running speed:
    python3 bearing_tool.py 6205-2RS --rpm 1480
"""

import argparse
import csv
from pathlib import Path

from symptoms import BEARING_KEYS, bearing_orders, designation_keys, load_bearings

CSV = Path(__file__).resolve().parent / "knowledge" / "bearings.csv"
FIELDS = ["designation", "type", "balls", "ball_diameter_mm", "pitch_diameter_mm", "contact_angle_deg",
          *BEARING_KEYS, "source"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("designation")
    parser.add_argument("--balls", type=int)
    parser.add_argument("--ball-diameter", type=float, help="mm")
    parser.add_argument("--pitch-diameter", type=float, help="mm; about (bore + outside diameter) / 2")
    parser.add_argument("--contact-angle", type=float, default=0.0, help="degrees")
    parser.add_argument("--orders", type=float, nargs=4, metavar=("BPFO", "BPFI", "BSF", "FTF"))
    parser.add_argument("--type", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--rpm", type=float, help="Also print the frequencies in Hz at this speed")
    parser.add_argument("--file", default=str(CSV))
    args = parser.parse_args(argv)

    known = load_bearings(args.file)
    geometry = (args.balls, args.ball_diameter, args.pitch_diameter)
    if args.orders:
        orders = dict(zip(BEARING_KEYS, args.orders))
    elif all(geometry):
        orders = bearing_orders(*geometry, args.contact_angle)
    else:
        found = next((known[k] for k in designation_keys(args.designation) if k in known), None)
        if not found:
            parser.error("Unknown bearing: give --balls, --ball-diameter and --pitch-diameter, or --orders")
        orders = found
        print(f"{args.designation} is already listed as {found['designation']}.")

    for key in BEARING_KEYS:
        hz = f"  = {orders[key] * args.rpm / 60:.2f} Hz at {args.rpm:g} RPM" if args.rpm else ""
        print(f"{key.upper():5} {orders[key]:.4f} x shaft speed{hz}")

    if args.orders or all(geometry):
        if designation_keys(args.designation) & {k for k, v in known.items()
                                                 if v["designation"].upper() == args.designation.upper()}:
            parser.error(f"{args.designation} is already in {args.file}; edit the file to change it")
        row = {"designation": args.designation, "type": args.type, "balls": args.balls or "",
               "ball_diameter_mm": args.ball_diameter or "", "pitch_diameter_mm": args.pitch_diameter or "",
               "contact_angle_deg": args.contact_angle if all(geometry) else "",
               **{k: f"{orders[k]:.4f}" for k in BEARING_KEYS}, "source": args.source}
        with open(args.file, "a", newline="", encoding="utf-8") as stream:
            csv.DictWriter(stream, FIELDS).writerow(row)
        print(f"Added {args.designation} to {args.file}. Restart vds-ai-advisor to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
