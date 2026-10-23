# IRIV Edge AI demo

See the AI working without an IRIV, a broker or sensors. The demo simulates a small
plant, feeds the data through the real dashboard's message handler, runs the real
AI advisor over it hour by hour, and then opens the dashboard.

```bash
pip install -r requirements.txt          # once, from the repository folder
python iriv-edge/demo/run_demo.py         # about 1-2 minutes to simulate 4 days
```

On Windows you can instead double-click `iriv-edge\demo\run_demo.cmd`. It uses
[uv](https://docs.astral.sh/uv/) when installed, so Python itself is not needed.

Wait until the window says **Open http://127.0.0.1:8060**, then open that address.
The page only works while that window stays open. Press Ctrl+C to stop.

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

## Things to try

- On the dashboard, open **PUMP-STATION-1**, then **CH1 → Spectrum**. You see the
  bearing lines (BPFO, BPFI, BSF, FTF), the peaks matched to them, and the AI's
  finding. Press **Confirmed**, **Wrong** or **Repaired**; the AI scans every
  20 seconds in the demo, so the effect shows quickly.
- In the spectrum, type another bearing number (for example 6308) to see where its
  lines fall, switch harmonics, or use the log scale.
- Open **Asset registry**, choose a sensor and look at the **AI analysis** section.
  Typing a bearing number shows its frequencies in Hz.
- **Health Agents** shows every sensor's AI status and trend. **Reports and backups**
  lists the AI reports; the folders themselves are in
  `iriv-edge/demo/output/data/ai/reports/`.

Everything is synthetic. Nothing connects to a broker, PLC or real sensor, and the
output folder is safe to delete.
