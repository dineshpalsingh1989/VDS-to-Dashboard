# Wilcoxon VDS Condition Monitor

MQTT ingestion, a SQLite historian, measurement-aware alarms, diagnostic plots,
and advisory health agents for Wilcoxon VDS130 data. The dashboard also exposes
selected values and data quality through Modbus TCP and OPC UA.

![Dashboard with four vibration channels, RPM, measurement settings, and a velocity trend](docs/images/dashboard.png)

Screenshots use synthetic pump measurements and demonstration identifiers. They
show the actual application; they are not measurements from production equipment.
Click any screenshot to view it at full size.

## How it works

```mermaid
flowchart LR
    Sensors["IEPE sensors + tachometer"] --> VDS["Wilcoxon VDS130"]
    VDS -->|MQTT messages| Broker["Your MQTT broker"]
    Broker --> App["VDS Condition Monitor"]
    App --> DB[("SQLite historian")]
    App --> UI["Browser dashboard"]
    DB --> Analysis["Alarms, baselines, health checks"]
    Analysis --> UI
    DB --> Files["Reports, exports, verified backups"]
    App --> Gateway["Modbus TCP / OPC UA"]
    Gateway --> PLC["PLC / SCADA"]
```

The VDS publishes measurements to your broker. The application validates each
message, preserves its measurement settings, and stores scalar history in SQLite.
Recording jobs also retain raw waveforms and spectra. Operators review trends,
alarm events, and advisory findings through the browser. Docker packages the
application; the database remains SQLite on a persistent volume.

## Visual walkthrough

### 1. Connect and identify the measurement point

Configure the broker in **MQTT Settings** and restart the application. When data
arrives, open **Asset registry**, select a channel, and enter the site, machine,
bearing location, direction, and expected publishing interval. Click **Save asset**.

![Asset registry mapping a VDS channel to a pump bearing and publishing interval](docs/images/asset-registry.png)

### 2. Configure and review alarms

Open **Alarm management**, choose a channel and metric, and check the displayed
unit, filter, and detection method. Set machine-specific thresholds, recovery
hysteresis, and consecutive captures, then enable and save the rule. The event
table records transitions; select an event and click **Acknowledge selected event**
to record the operator's review. The screenshot's thresholds are demonstration values.

![Alarm settings and a persisted warning event with measurement context](docs/images/alarm-management.png)

### 3. Compare spectra and recorded captures

Start a recording in **Data Storage** to retain raw captures. Open **Vibration
diagnostics**, choose a channel, and use **Capture current baseline** when its
operating condition is suitable as a reference. Compare the current spectrum to
that baseline, inspect RPM harmonics, or enable the order axis. The waterfall
shows compatible recorded spectra over time. The screenshot is zoomed to 0–500 Hz.

![Current and baseline spectra with RPM harmonics and a waterfall of recorded captures](docs/images/diagnostics.png)

### 4. Review health and export results

Open **Health Agents** for advisory findings. Use **History Explorer** for trends
and CSV exports, or **Reports and backups** to select a UTC day, download a report,
and create an integrity-checked database backup. Administrator access is required
for changes and acknowledgments; viewer accounts can inspect permitted read-only views.

### The six monitoring checks

| Check | What it reports |
| --- | --- |
| Connectivity | MQTT connection failures |
| Sensor | Missing or stale measurements after three expected publishing intervals |
| Data quality | Missing measurement settings and invalid array lengths |
| Storage | Less than 10% free disk space |
| Baseline | Changes of at least 50% against a compatible saved baseline |
| Health | An overall healthy result when no issues are found |

These are deterministic checks, not independent AI processes. Daily reports,
backups, and retention run in a separate maintenance task. `AGENTS.md` contains
development instructions for coding assistants; it does not start runtime agents.

## Run locally

Tested with Python 3.14:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Open http://127.0.0.1:8050. On Linux, use `.venv/bin/python` instead.
The application uses Waitress with one process and eight HTTP threads.
Configure the broker in MQTT Settings, then restart the application to apply
broker changes. Restart Application restarts Python; it does not reboot the host.

## Docker

```powershell
Copy-Item .env.example .env
# Edit .env and set a strong VDS_ADMIN_PASSWORD before the next command.
docker compose up -d --build
```

Compose reads `.env`; running `python app.py` directly does not load it.
The image runs as UID 10001 with dropped capabilities. Compose uses a read-only
root filesystem, a temporary /tmp, and a persistent named volume at /data.
SQLite, backups, reports, and broker configuration survive container replacement.
The dashboard is published on 127.0.0.1:8050 by default. `/healthz` checks HTTP
process liveness and does not claim that sensors or the broker are connected.

This stack uses SQLite, not PostgreSQL. It does not start an MQTT broker.
Configure the address of your existing broker; `localhost` inside the container
means the container itself. Docker Desktop can reach a host broker using
`host.docker.internal` when the host broker permits that connection.

Industrial protocol ports are not published by the default Compose file. For a
trusted OT network deployment, explicitly configure `VDS_PROTOCOL_HOST`, an
unprivileged Modbus port such as 1502, and the required port mappings. OPC UA and
Modbus endpoints are separate from the dashboard login. No privileged container
or host networking is needed for the default dashboard deployment. Host time
administration is intentionally unavailable inside the restricted container.

Stop the stack with `docker compose down`. Do not add `--volumes` unless you
intend to remove its persisted data.

## Commission a device

1. Configure VDS publishing to your MQTT broker and subscribe through MQTT Settings.
2. Open Asset registry. Map each gateway/channel to site, machine, bearing location,
   and measurement direction. Enter the device's expected publishing interval.
3. Check Health Agents and the measurement cards for timestamp, metadata, and
   array-length issues before using the data for alarms or comparisons.
4. Open Alarm management. Select a metric, review its published unit, filter, and
   detection method, then set machine-specific warning/alert thresholds,
   hysteresis, and consecutive-capture persistence. Initial thresholds are examples.
5. Start a recording to retain raw waveforms and spectra. Scalar history is
   continuous, whether or not a recording job is active.
6. Open Vibration diagnostics to save a baseline, overlay spectra, inspect RPM
   harmonics/order axes, and view a waterfall of compatible recorded spectra.

Freshness is based on three missed publishing intervals, not the browser refresh
rate. The default interval is 60 seconds, so the default stale threshold is 180
seconds. Asset settings describe the VDS; they do not change device firmware.

## Measurement integrity

The parser follows the MQTT content documented in the
[VDS130 setup manual, page 10](https://wilcoxon.com/wp-content/uploads/2026/03/VDS130-setup-instructions-97047A.pdf).
It preserves metric units, detection, and filters; sensor type/sensitivity; capture
and receipt timestamps; and waveform/spectrum acquisition settings when supplied.
`StandardDeviation` is read from the device, rather than replaced by a different
calculation. Missing metrics remain missing, rather than becoming zero.

Waveform length is checked against duration x sampling frequency when supplied;
spectra require lines + 1 samples including the DC bin. Invalid lengths produce
quality findings and are not presented with fabricated plot axes. The test
fixtures contain small synthetic examples shaped after the documented schema;
they are not recordings from the user's hardware.

Duplicate captures do not advance alarm persistence. Older captures may enter
history but do not replace newer live metric values. Alarm state and transitions
are durable. A unit/filter/detection change puts a rule into an unknown state until
reviewed. Acknowledgment records the user and time; it does not clear a fault.

History plots separate incompatible measurement settings, and CSV/Excel exports
include context. Legacy rows without context remain explicitly unknown. Baseline
spectrum comparisons require matching acquisition settings. The published power
spectrum is plotted in device units without an assumed amplitude conversion.
The MQTT schema may omit the window function; verify unchanged VDS settings when
that field is unavailable. RPM order plots use the RPM attached to the capture;
they are not tachometer-resampled order tracking.

Health Agents provide connectivity, data-quality, freshness, storage, and
baseline-change findings. Baseline changes of 50% or more are advisory and include
reference/current values. Reports group measurements by their settings. None of
these features classifies mechanical faults or issues machine commands.

## Storage, backup, and retention

Scheduled maintenance runs once per UTC day, starting on application launch:

- Create a SQLite backup and check its integrity before enabling any pruning.
- Generate the previous UTC day's Markdown report under `reports/`.
- Apply configured retention; 0 means keep all data.

Backups live under `backups/` in VDS_DATA_DIR. The Reports and backups page also
creates a verified manual backup and provides on-demand reports, including today.
Download backups to separate storage; local copies do not protect against disk loss.
Automatic backups/reports themselves are retained until archived by the operator.

Raw/scalar pruning preserves data covered by active recording jobs. Alarm retention
removes only acknowledged events. Retention does not reinterpret legacy local
timestamps. New context includes epoch capture times and UTC report/export fields.
A future migration of legacy timestamps requires knowing their original timezone.

To restore, stop the application, preserve the current database and its sidecars,
then place a verified backup at VDS_DATA_DIR/vibration_trends.db with ownership
that permits the service user to write. Do not combine a restored database with
old -wal/-shm sidecars. Restart and verify channels, rules, and recording jobs.

The upgrade adds tables and context columns without dropping historical tables.
Plain legacy pickled configuration is restricted to non-executable objects and
migrated to JSON. Old dashboard alarm thresholds remain in configuration but must
be re-entered in Alarm management with verified measurement settings.

## Environment and access

| Variable | Default | Purpose |
| --- | --- | --- |
| VDS_HOST / VDS_PORT | 127.0.0.1 / 8050 | Native HTTP bind address/port |
| VDS_ADMIN_USER / VDS_ADMIN_PASSWORD | admin / empty | Password is required for remote access |
| VDS_VIEWER_USER / VDS_VIEWER_PASSWORD | viewer / empty | Optional read-only account |
| VDS_PROTOCOL_HOST | 127.0.0.1 | Industrial protocol interface |
| VDS_MODBUS_PORT | 502 | Set 1502 for an unprivileged deployment |
| VDS_DATA_DIR | device_data beside app.py | Database, backups, reports |
| VDS_CONFIG_DIR | directory containing app.py | MQTT/Modbus configuration |
| VDS_TREND_RETENTION_DAYS | 0 | Scalar history retention |
| VDS_RAW_RETENTION_DAYS | 0 | Waveform/spectrum retention |
| VDS_ALARM_RETENTION_DAYS | 0 | Acknowledged alarm retention |
| VDS_BIND_ADDRESS | 127.0.0.1 | Compose host publication address |

Use HTTPS through a reverse proxy for remote access: HTTP Basic credentials need
encrypted transport. Viewer writes are blocked on the server even if controls
remain visible. OPC UA currently uses anonymous access; bind industrial listeners
to a trusted network. Dashboard credentials do not protect these protocols.
Run one application process, not multiple WSGI workers with separate device state.

Modbus uses big-endian IEEE-754 float32, two holding registers per mapping. In
addition to metrics/RPM, choose Data Age (seconds; -1 is unknown), Data Valid (0/1),
and Communication Status (0/1) for a channel. Always inspect quality alongside
numeric values; a numeric fallback of zero is not proof of a valid measurement.
OPC UA adds read-only DataAgeSeconds, DataValid, CommunicationStatus, and per-metric
Settings JSON nodes under each channel. IDs are stable within the registered
namespace. These gateways remain monitoring interfaces, not machinery protection.

## Development and replay

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m compileall -q app.py runtime.py vds.py historian.py diagnostics.py workspace_views.py monitoring.py replay.py
.venv\Scripts\python replay.py tests/fixtures/vds130_messages.json --data-dir .review/replay --rebase-time --record-raw
```

Replay refuses an existing database and starts no hardware services. Captured
JSON can replace the synthetic fixture for commissioning tests. Keep credentials
out of captures. Tests use temporary databases and mock protocol listeners; they
do not connect to physical sensors or PLCs. CI runs tests and builds the image.

Modules: vds.py normalizes measurements; historian.py persists context, rules,
backups and retention; diagnostics.py builds plots/exports; workspace_views.py
contains the new pages; monitoring.py provides advisory agents; app.py integrates
hardware services and the original dashboard.

PyModbus is pinned to 2.5.3 until a separately tested v3 API migration is completed.
Licensed under MIT; see LICENSE.
