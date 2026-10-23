"""Serve the real UI with invented data for the static demo exporter only.

Uses a fresh temporary directory. Never starts MQTT, Modbus, OPC UA, maintenance,
or the application's production entry point.
"""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
temporary = None if os.environ.get("VDS_DEMO_EXPORT_ROOT") else tempfile.TemporaryDirectory(prefix="vds-demo-export-")
fixture_root = Path(os.environ["VDS_DEMO_EXPORT_ROOT"]) if temporary is None else Path(temporary.name)
os.environ.update(
    VDS_DATA_DIR=str(fixture_root / "data"),
    VDS_CONFIG_DIR=str(fixture_root / "config"),
    VDS_ADMIN_PASSWORD="",
    VDS_VIEWER_PASSWORD="",
    VDS_PROTOCOL_HOST="127.0.0.1",
)
Path(os.environ["VDS_CONFIG_DIR"]).mkdir(parents=True)

import app  # noqa: E402
from vds_dashboard import historian  # noqa: E402
import numpy as np  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
from vds_dashboard.diagnostics import spectrum_figure, waterfall_figure  # noqa: E402
from plotly.utils import PlotlyJSONEncoder  # noqa: E402
from waitress import create_server  # noqa: E402

gid = "VDS-DEMO-01"
epoch = time.time()
meta = {"unit": "mm/s", "filter": "10Hz-1kHz", "detection": "RMS"}
with closing(app.get_db_connection()) as conn, conn:
    for ch in ("1", "2", "3", "4"):
        historian.save_asset(conn, gid, ch, dict(
            site="Example plant / Utilities", machine="Cooling water pump P-103",
            location="Drive-end bearing" if ch in ("1", "2") else "Pump bearing",
            direction={"1": "Horizontal", "2": "Vertical", "3": "Axial", "4": "Horizontal"}[ch],
            publish_interval=60,
        ))
    historian.save_rule(conn, gid, "1", dict(enabled=True, metric="Velocity", warn=3,
                                            alert=5, hysteresis=.3, persistence=2, metadata=meta))
    historian.save_ai_gateway(conn, gid, True)
    for ch, location in (("1", "Motor drive end"), ("2", "Motor drive end"), ("3", "Pump bearing"), ("4", "Pump bearing")):
        historian.save_ai_channel(conn, gid, ch, dict(
            machine="Cooling water pump P-103", location=location, machine_type="motor" if ch in ("1", "2") else "pump",
            direction={"1": "horizontal", "2": "vertical", "3": "axial", "4": "horizontal"}[ch],
            bearing="6205-2RS" if ch in ("1", "2") else "", speed_source="vds", line_frequency_hz=50,
            blades=None if ch in ("1", "2") else 6, iso_class="group2_rigid"))

app.active_jobs[gid] = {"job_id": "synthetic-demo", "end_time": datetime.now() + timedelta(hours=1)}
normalizer = app.normalize_packet
app.normalize_packet = lambda payload: normalizer(payload, received_at=payload["timestamp"] / 1000 + 1)
fs, lines, bandwidth = 2048, 500, 500
x, freq = np.arange(1024) / fs, np.arange(lines + 1)
baselines, captures = {}, {ch: [] for ch in ("1", "2", "3", "4")}
for index in range(12):
    capture = int((epoch - (11 - index) * 60) * 1000)
    for ch in ("1", "2", "3", "4"):
        scale = 1 + index / 14 if ch == "1" else 1 + .03 * np.sin(index)
        process = dict(
            RPM=1440, SensorType="Acceleration(g)", SensorSensitivity=100,
            Acceleration=dict(value=round(.12 * scale, 3), unit="g", filter="10Hz-10kHz", detection="RMS"),
            Velocity=dict(value=round((2.2 if ch == "1" else 1.3 + int(ch) * .25) * scale, 3), **meta),
            Displacement=dict(value=round(18 * scale, 2), unit="µm", filter="10Hz-1kHz", detection="RMS"),
            CrestFactor=dict(value=3.2, unit="ratio", filter="10Hz-10kHz", detection="ratio"),
            TruePeak=dict(value=round(.4 * scale, 3), unit="g", filter="10Hz-10kHz", detection="PEAK"),
            StandardDeviation=dict(value=round(.12 * scale, 3), unit="g", filter="10Hz-10kHz", detection="STD"),
        )
        common = dict(gateway_id=gid, sensor_id=f"{gid}_{ch}", timestamp=capture,
                      sampling_frequency=fs, window="Hanning", process_data=process)
        values = .0005 + .001 * np.abs(np.sin(freq / 33))
        for center, amplitude in ((24, .2 * scale), (48, .08 * scale), (72, .04), (340, .01 * scale)):
            values += amplitude * np.exp(-.5 * ((freq - center) / 2) ** 2)
        for payload in (
            dict(common, duration=.5, **{"Time waveform": (.12 * scale * np.sin(2 * np.pi * 24 * x) + .04 * np.sin(2 * np.pi * 48 * x)).tolist()}),
            dict(common, bandwidth=bandwidth, lines=lines, **{"Power spectrum": values.tolist()}),
        ):
            app.on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))
        data = app.data_storage[gid]["channels"][ch]
        snapshot = dict(copy.deepcopy(data["spectrum_context"]), spectrum_data=data["spectrum_data"],
                        metrics=data["metrics"], metadata=data["metadata"], label=datetime.fromtimestamp(capture / 1000).isoformat())
        snapshot["waveform"] = dict(copy.deepcopy(data["waveform_context"]), waveform_data=data["waveform_data"])
        captures[ch].append(snapshot)
        if index == 0:
            baselines[ch] = snapshot

with closing(app.get_db_connection()) as conn, conn:
    for ch, baseline in baselines.items():
        conn.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", (gid, ch, json.dumps(baseline)))
    conn.execute("INSERT OR REPLACE INTO recording_jobs VALUES (?,?,?,?,?,?)",
                 ("synthetic-demo", gid, datetime.now() - timedelta(minutes=12), datetime.now() + timedelta(hours=1), 1, "active"))

# Run the real AI advisor once over the synthetic data, so the AI page shows genuine
# learning progress. Fake mount tables keep it away from the exporting host's disks.
sys.path.insert(0, str(root / "iriv-edge"))
from ai_advisor import Advisor, write_report  # noqa: E402
advisor = Advisor(os.environ["VDS_DATA_DIR"], mounts_file=fixture_root / "no-mounts", sys_block=fixture_root / "no-sys")
write_report(os.environ["VDS_DATA_DIR"], advisor.cycle())
advisor.close()

# These functions supply demonstration host information, never the exporting host.
app.get_ip_address = lambda: "192.0.2.10"
app.get_pi_serial = lambda: "DEMO-EDGE-01"
app.get_uptime = lambda: "02:14:00"
app.get_memory_usage = lambda: ("4.0 GB", "2.8 GB")
app.get_disk_usage = lambda: ("32.0 GB", "24.0 GB")
app.get_cpu_temp = lambda: "42.0 °C (sample)"
app.get_cpu_load = lambda: "8.0% (sample)"
app.get_system_time_settings = lambda: ([{"label": "UTC", "value": "UTC"}], "UTC", False)
app.get_ethernet_settings = lambda: dict(ip_address="192.0.2.10", subnet_mask="255.255.255.0", gateway="192.0.2.1", dns_servers="192.0.2.1")
app.psutil.disk_usage = lambda _: SimpleNamespace(percent=25)
app.mqtt_connection_status = {"status": "connected", "message": "Synthetic demo only; no broker connection"}
app.initial_device_config[gid] = {"trends": {"1": True}, "labels": {ch: "Cooling water pump" for ch in ("1", "2", "3", "4")}}
app.app.layout.children[2].data = app.initial_device_config
original_freshness = app.freshness
app.freshness = lambda data: original_freshness(data, epoch)
original_assess = app.assess_health
app.assess_health = lambda *args, **kwargs: original_assess(*args, now=epoch, **kwargs)

# Only synthetic values are used when constructing pages that display settings.
app.BROKER, app.PORT, app.TOPIC = "broker.example.invalid", 1883, "wilcoxon/demo"
app.load_mqtt_config = lambda: dict(BROKER=app.BROKER, PORT=app.PORT, TOPIC=app.TOPIC, USERNAME="", PASSWORD="")
app.log_messages.clear()
app.log_messages.append("Synthetic viewer preview. No MQTT broker, PLC, or sensor is connected.")

bank = {"rpm": 1440, "gateway": gid, "channels": {}, "figures": {}, "capturedAt": datetime.fromtimestamp(epoch).isoformat()}
for ch in ("1", "2", "3", "4"):
    data = app.data_storage[gid]["channels"][ch]
    bank["channels"][ch] = {key: data[key] for key in ("metrics", "metadata", "asset")}
    bank["channels"][ch]["captures"] = captures[ch]
    bank["channels"][ch]["baseline"] = baselines[ch]
    bank["channels"][ch]["history"] = {metric: list(data["trends"][metric]) for metric in app.TRENDED_METRICS}
    bank["channels"][ch]["times"] = [str(t) for t in data["trend_x"]["Velocity"]]
    for orders in (False, True):
        bank["figures"][f"diagnostics:{ch}:{int(orders)}"] = spectrum_figure(captures[ch][-1], baselines[ch], orders)
    bank["figures"][f"waterfall:{ch}"] = waterfall_figure(captures[ch])
    for metric in app.TRENDED_METRICS:
        fig = go.Figure(go.Scatter(x=bank["channels"][ch]["times"], y=list(data["trends"][metric]), line=dict(color={"1":"blue","2":"green","3":"orange","4":"purple"}[ch], shape="spline")))
        fig.update_layout(title=f"CH{ch} - {metric}", template="plotly_white", margin=dict(l=40,r=20,t=40,b=30), height=250,
                          yaxis_title=f"{metric} ({data['metadata'][metric]['unit']})")
        bank["figures"][f"trend:{ch}:{metric}"] = fig
        day = datetime.fromtimestamp(epoch).date().isoformat()
        bank["figures"][f"history:{ch}:{metric}"] = app.load_history(1, day, day, gid, ch, metric)
    bank["figures"][f"waveform:{ch}"] = go.Figure(go.Scatter(x=x, y=data["waveform_data"], mode="lines")).update_layout(
        title=f"Waveform ({data['timestamp']})", xaxis_title="Time (s)", yaxis_title="Published waveform (sensor units)")
    bank["figures"][f"spectrum:{ch}"] = go.Figure(go.Scatter(x=freq, y=data["spectrum_data"], mode="lines")).update_layout(
        title=f"Spectrum ({data['timestamp']})", xaxis_title="Hz", yaxis_title="Amp")

bank["figures"]["trend:RPM"] = go.Figure(go.Scatter(x=bank["channels"]["1"]["times"], y=[1440] * 12, line=dict(color="cyan", shape="spline"))).update_layout(
    title="RPM Trend", template="plotly_white", margin=dict(l=40,r=20,t=40,b=30), height=250, yaxis_title="RPM")

@app.app.server.get("/__demo__/bank")
def export_bank():
    return app.app.server.response_class(json.dumps(bank, cls=PlotlyJSONEncoder), mimetype="application/json")

server = create_server(app.app.server, host="127.0.0.1", port=0)
print("DEMO_URL=http://127.0.0.1:" + str(server.effective_port), flush=True)
try:
    server.run()
finally:
    server.close()
    if temporary:
        temporary.cleanup()
