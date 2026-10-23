# IRIV Edge AI demo

See the AI working without an IRIV, a broker or sensors. The demo simulates a small
plant, feeds the data through the real dashboard's message handler, runs the real
AI advisor over it hour by hour, and then opens the dashboard.

```bash
pip install -r requirements.txt          # once, from the repository folder
python iriv-edge/demo/run_demo.py         # about 1-2 minutes to simulate 4 days
```

Then open **http://127.0.0.1:8060/ai**. Press Ctrl+C to stop.

| Option | Meaning |
|---|---|
| `--days 4` | Simulated days of running (at least 1.5; the AI learns for 12 hours first) |
| `--port 8060` | Dashboard port, on this computer only |
| `--no-server` | Print the findings and exit |
| `--data-dir PATH` | Where to keep the run (default `iriv-edge/demo/output`, recreated each run) |

## What happens in the simulated plant

| Sensor | Machine | What develops |
|---|---|---|
| PUMP-STATION-1 CH1 | Cooling pump P-101, motor drive end, bearing 6205-2RS | Outer race defect. The envelope spectrum shows it first, the normal spectrum later |
| PUMP-STATION-1 CH2 | Cooling pump P-101, pump bearing | Healthy |
| PUMP-STATION-1 CH3 | Cooling tower fan F-201, fan bearing | Growing unbalance; Velocity rises toward ISO zone C |
| PUMP-STATION-1 CH4 | Cooling tower fan F-201, motor drive end | Healthy |
| CONVEYOR-2 CH1 | Conveyor gearbox G-301, 23 teeth | Mechanical looseness. This VDS has no speed sensor, so a fixed 1460 RPM is entered |

## Things to try in the AI page

- Read the **active findings**, then press **Confirmed**, **Wrong** or **Repaired**.
  The AI scans every 20 seconds in the demo, so the effect shows quickly.
- Choose a sensor to see its **fault frequency table** and its **trend** with the
  projection to ISO zone C.
- **Add a bearing**, then enter it as a sensor's bearing number.
- Open a report under **AI reports**, or open the folders themselves in
  `iriv-edge/demo/output/data/ai/reports/`.

Everything is synthetic. Nothing connects to a broker, PLC or real sensor, and the
output folder is safe to delete.
