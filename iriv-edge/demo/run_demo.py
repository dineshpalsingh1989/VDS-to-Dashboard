#!/usr/bin/env python3
"""IRIV Edge AI demo: a simulated plant, the real dashboard and the real AI advisor.

Simulates several days of VDS data for five sensors on three machines, feeds it
through the dashboard's own MQTT message handler, runs the AI advisor over it
hour by hour, and then serves the dashboard at http://127.0.0.1:8060. Open a sensor's
spectrum to see fault lines and the AI finding. Nothing connects to a broker, PLC or sensor.

What develops during the simulated days:
  * Cooling pump P-101 motor: an outer race defect on its 6205-2RS bearing.
    The envelope spectrum shows it first; later the normal spectrum too.
  * Cooling tower fan F-201: growing unbalance. Velocity rises steadily, so the
    AI estimates how many days until ISO zone C.
  * Conveyor gearbox G-301: mechanical looseness. Its VDS has no speed sensor,
    so the demo enters a fixed speed, as a user would in the asset registry.
  * Two healthy sensors that should stay quiet.

    python iriv-edge/demo/run_demo.py              # simulate 4 days, then open the dashboard
    python iriv-edge/demo/run_demo.py --no-server  # simulate and print the findings only
"""

import argparse
import json
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STEP_S = 600          # one VDS capture per sensor every 10 minutes
LINES, BANDWIDTH = 800, 1000.0
FS, SAMPLES = 4096, 4096
BPFO_6205 = 3.5848
MARKER = ".vds-ai-demo"

MACHINES = [
    dict(gid="PUMP-STATION-1", ch="1", machine="Cooling pump P-101", location="Motor drive end", machine_type="motor", end="DE",
         direction="horizontal", bearing="6205-2RS", rpm=1480, fault="bearing", power_kw=22, foundation="rigid"),
    dict(gid="PUMP-STATION-1", ch="2", machine="Cooling pump P-101", location="Pump bearing", machine_type="pump", end="DE",
         direction="horizontal", bearing="6203-2RS", blades=6, rpm=1480, fault=None, power_kw=22, foundation="rigid"),
    dict(gid="PUMP-STATION-1", ch="3", machine="Cooling tower fan F-201", location="Fan bearing", machine_type="fan", end="DE",
         direction="vertical", bearing="6205-2RS", blades=8, rpm=990, fault="unbalance", power_kw=22, foundation="rigid"),
    dict(gid="PUMP-STATION-1", ch="4", machine="Cooling tower fan F-201", location="Motor drive end", end="DE",
         machine_type="motor", direction="axial", rpm=990, fault=None, power_kw=22, foundation="rigid"),
    dict(gid="CONVEYOR-2", ch="1", machine="Conveyor gearbox G-301", location="Input shaft", machine_type="gearbox", end="DE",
         direction="radial", gear_teeth=23, rpm=1460, fixed_speed=True, fault="looseness", power_kw=30, foundation="rigid"),
]


def ramp(fraction, start):
    """0 before start, rising linearly to 1 at the end of the simulation."""
    return max(0.0, (fraction - start) / (1 - start))


def capture(machine, fraction, rng):
    """One VDS waveform message and one spectrum message for a sensor."""
    rpm = machine["rpm"] * (1 + 0.002 * rng.standard_normal())
    shaft = rpm / 60
    line = lambda hz: int(round(hz / (BANDWIDTH / LINES)))  # noqa: E731
    spectrum = 1e-4 * (1 + 0.2 * rng.random(LINES + 1))
    one_x = 1e-2
    t = np.arange(SAMPLES) / FS
    wave = 0.2 * rng.standard_normal(SAMPLES)
    velocity = 1.1 + 0.02 * rng.standard_normal()
    fault = machine["fault"]
    if fault == "unbalance":
        growth = ramp(fraction, 0.35)
        one_x *= 1 + 4 * growth
        velocity *= 1 + 1.0 * growth
    if fault == "bearing":
        early, late = ramp(fraction, 0.55), ramp(fraction, 0.85)
        since = t % (1 / (BPFO_6205 * shaft))
        wave += 0.8 * early * np.exp(-since * 400) * np.sin(2 * np.pi * 1200 * since)
        spectrum[line(BPFO_6205 * shaft)] += 8e-3 * late
        spectrum[line(2 * BPFO_6205 * shaft)] += 3e-3 * late
        velocity += 0.4 * late
    if fault == "looseness":
        loose = ramp(fraction, 0.7)
        for k in range(3, 9):
            spectrum[line(k * shaft)] += 4e-3 * loose
        for half in (1.5, 2.5, 3.5):
            spectrum[line(half * shaft)] += 2e-3 * loose
        velocity += 0.5 * loose
    spectrum[line(shaft)] += one_x
    spectrum[line(2 * shaft)] += 2e-3
    wave += 10 * math.sqrt(one_x) * np.sin(2 * np.pi * shaft * t)
    process = {
        "SensorType": "Acceleration(g)", "SensorSensitivity": 100,
        "Velocity": {"value": round(velocity, 3), "unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS"},
        "Acceleration": {"value": round(0.12 * velocity, 4), "unit": "g", "filter": "10Hz-10kHz", "detection": "RMS"},
    }
    if not machine.get("fixed_speed"):
        process["RPM"] = round(rpm, 1)
    return process, wave, spectrum


def feed(app, machine, process, wave, spectrum, ms):
    common = {"gateway_id": machine["gid"], "sensor_id": f"{machine['gid']}_{machine['ch']}", "timestamp": ms,
              "sampling_frequency": FS, "window": "Hanning", "process_data": process}
    for payload in (dict(common, duration=SAMPLES / FS, **{"Time waveform": np.round(wave, 5).tolist()}),
                    dict(common, bandwidth=BANDWIDTH, lines=LINES, **{"Power spectrum": spectrum.tolist()})):
        app.on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))


def prepare_output(path):
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        if not (path / MARKER).exists():
            raise SystemExit(f"{path} is not empty and was not made by this demo; choose another --data-dir.")
        shutil.rmtree(path)
    (path / "data").mkdir(parents=True)
    (path / "config").mkdir()
    (path / MARKER).write_text("Created by iriv-edge/demo/run_demo.py; safe to delete.\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=float, default=4, help="Simulated days of running (at least 1.5)")
    parser.add_argument("--port", type=int, default=8060)
    parser.add_argument("--data-dir", default=str(HERE / "output"), help="Demo output folder (recreated each run)")
    parser.add_argument("--no-server", action="store_true", help="Print the findings and exit")
    args = parser.parse_args(argv)
    if args.days < 1.5:
        parser.error("--days must be at least 1.5: the AI first learns for 12 hours of running")

    out = prepare_output(args.data_dir).resolve()
    os.environ.update(VDS_DATA_DIR=str(out / "data"), VDS_CONFIG_DIR=str(out / "config"), VDS_EDGE_AI="1",
                      VDS_LEARNING_INTERVAL_S=str(STEP_S), VDS_ADMIN_PASSWORD="", VDS_VIEWER_PASSWORD="")
    sys.path[:0] = [str(ROOT), str(ROOT / "iriv-edge")]
    import app  # noqa: E402  the real dashboard, with no MQTT, Modbus or OPC UA started
    from vds_dashboard import historian  # noqa: E402
    from ai_advisor import Advisor, write_report  # noqa: E402

    app.setup_database()
    normalize = app.normalize_packet
    app.normalize_packet = lambda payload: normalize(payload, received_at=payload["timestamp"] / 1000)
    with app.get_db_connection() as conn:
        for gid in sorted({m["gid"] for m in MACHINES}):
            historian.save_ai_gateway(conn, gid, True)
        for m in MACHINES:
            historian.save_ai_channel(conn, m["gid"], m["ch"], dict(
                machine=m["machine"], location=m["location"], machine_type=m["machine_type"],
                direction=m["direction"], bearing=m.get("bearing", ""), gear_teeth=m.get("gear_teeth"),
                blades=m.get("blades"), end=m.get("end", ""), power_kw=m["power_kw"], foundation=m["foundation"], line_frequency_hz=50,
                speed_source="fixed" if m.get("fixed_speed") else "vds",
                fixed_rpm=m["rpm"] if m.get("fixed_speed") else None))
    conn.close()

    advisor = Advisor(out / "data", mounts_file=out / "no-mounts", sys_block=out / "no-sys-block")
    rng = np.random.default_rng(2026)
    steps = int(args.days * 86400 / STEP_S)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - steps * STEP_S * 1000
    print(f"Simulating {args.days:g} days of {len(MACHINES)} VDS sensors ({steps} captures each)...")
    for step in range(steps + 1):
        ms = start_ms + step * STEP_S * 1000
        for machine in MACHINES:
            feed(app, machine, *capture(machine, step / steps, rng), ms)
        if step % 6 == 0 or step == steps:  # the AI scans every simulated hour
            report = advisor.cycle(now_ms=ms, budget_s=1e9)
        if step % 72 == 0:
            print(f"  day {step * STEP_S / 86400:4.1f}: {report['summary']}")
    write_report(out / "data", report)

    print(f"\nAI findings after {args.days:g} simulated days:")
    for finding in report["findings"]:
        print(f"- [{finding['severity']}] {finding['message']}\n")
    if not report["findings"]:
        print("- none")
    root = advisor.storage.root()
    print(f"Report folders: {root / 'reports'}")
    print(f"Everything from this run: {out}")
    if args.no_server:
        advisor.close()
        return report

    def keep_scanning():
        """Keep the plant live: new readings every 20 s, and an AI scan so feedback buttons take effect."""
        while True:
            time.sleep(20)
            try:
                now_ms = int(time.time() * 1000)
                for machine in MACHINES:
                    feed(app, machine, *capture(machine, 1.0, rng), now_ms)
                write_report(out / "data", advisor.cycle())
            except Exception as error:  # noqa: BLE001 - keep the demo server alive
                print("AI scan skipped:", error)

    threading.Thread(target=keep_scanning, daemon=True).start()
    from waitress import serve  # noqa: E402
    print(f"\nOpen http://127.0.0.1:{args.port} and press a sensor's Spectrum button (Ctrl+C to stop)")
    serve(app.app.server, host="127.0.0.1", port=args.port, threads=4)


if __name__ == "__main__":
    main()
