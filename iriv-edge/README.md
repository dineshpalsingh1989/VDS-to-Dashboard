# IRIV Edge AI (CM5) deployment

This folder runs VDS-to-Dashboard on the
[Cytron IRIV Edge AI CM5](https://my.cytron.io/p-iriv-edge-ai-cm5) and adds an
**AI advisor** that can use the Raspberry Pi **AI HAT+** fitted inside it.

| Device feature | Use in this project |
|---|---|
| Raspberry Pi CM5, 8/16 GB RAM, eMMC, fanless DIN-rail case, 10–30 V DC | Runs the dashboard, MQTT client, SQLite historian, Modbus/OPC UA gateways |
| 2× Gigabit Ethernet | One port to the VDS sensor network, one to the plant/office network |
| AI HAT+ on PCIe: Hailo-8L 13 TOPS or Hailo-8 26 TOPS | Optional spectrum anomaly model (see below) |
| Isolated RS485 | Available to the existing Modbus gateway work; not used by this folder |
| Isolated DI/DO, LED drivers, buzzer | **Deliberately not used.** The advisor never drives outputs |
| 2× MIPI CSI camera ports | Future vision ideas (see below) |

## What is in this folder

| File | Purpose |
|---|---|
| `install.sh` | One-command setup on the device: dashboard container plus AI advisor service |
| `ai_advisor.py` | Scores each channel against its own history and writes findings for the dashboard |
| `hailo_backend.py` | Runs a compiled autoencoder on the AI HAT+ |
| `train_autoencoder.py` | Trains that autoencoder on a PC from your recorded spectra |
| `vds-ai-advisor.service` | Hardened systemd unit: no network, read-only historian |

## Install

On the IRIV, using Cytron's Bookworm image, with the repository copied to the device:

```bash
bash iriv-edge/install.sh            # build the dashboard on the device
bash iriv-edge/install.sh --image    # or pull the published container
```

The dashboard starts at `http://<iriv-ip>:8050` once `VDS_BIND_ADDRESS` in `.env`
allows it (default is local only). Findings from the advisor appear in the
dashboard's **health panel** as "AI advisor".

```bash
journalctl -u vds-ai-advisor -f       # advisor log
sudo systemctl restart vds-ai-advisor # after editing /etc/default/vds-ai-advisor
```

## What the AI advisor does

It works out of the box on the CPU, with no AI HAT+ software needed.

- **Metric check.** Each channel's latest Acceleration, Velocity, Displacement,
  crest factor and other readings are compared with the last 7 days of the *same*
  channel. Only readings with the same filter, unit and detection settings, and
  roughly the same RPM (±10 %), are compared. A large jump is reported with the
  usual value.
- **Spectrum check.** When you record spectra in the dashboard, the newest spectrum
  is compared band by band with earlier recordings. A new or growing peak is
  reported with its frequency, for example *energy near 500 Hz is 18 dB above its
  usual level*.

It does **not** diagnose faults, stop machines, or raise dashboard alarms. Alarm
rules in the dashboard stay the deterministic source of alarms.

**Limits worth knowing**

- Spectra are only stored while a recording runs, so the spectrum check needs at
  least 10 recordings with the same settings.
- A rolling 7-day comparison catches sudden changes. Slow wear over months is
  better tracked with the dashboard's saved baselines and alarm limits.
- A channel needs 20 comparable readings before it is scored.

## Using the AI HAT+

Vibration data is small. The CPU check above needs no accelerator. The AI HAT+
becomes useful when you want a model that learns the **shape** of a healthy
machine's spectrum. An autoencoder can flag combinations that no single threshold
would catch.

1. **Install the HAT software** on the IRIV, then reboot:
   ```bash
   bash iriv-edge/install.sh --with-hailo
   hailortcli fw-control identify     # shows HAILO8L or HAILO8
   ```
2. **Record healthy data.** In the dashboard, record for several days while the
   machine is known to be in good condition.
3. **Export the spectra** on the IRIV:
   ```bash
   sudo python3 iriv-edge/ai_advisor.py --data-dir "$(docker volume inspect wilcoxon-vds_vds-data --format '{{ .Mountpoint }}')" --export-dataset spectra.npy
   ```
4. **Train on a PC:**
   ```bash
   pip install torch onnx numpy
   python iriv-edge/train_autoencoder.py spectra.npy --out model
   ```
5. **Compile for the HAT** with the Hailo Dataflow Compiler. It needs an x86 Linux PC
   and a free Hailo Developer Zone account. Use `hailo8l` for the 13 TOPS HAT and
   `hailo8` for 26 TOPS. Check these commands against your compiler version:
   ```bash
   hailo parser onnx model/spectrum_ae.onnx --hw-arch hailo8l
   hailo optimize spectrum_ae.har --hw-arch hailo8l --calib-set-path model/calibration.npy
   hailo compiler spectrum_ae_optimized.har --hw-arch hailo8l
   ```
6. **Copy and enable** on the IRIV. Put `spectrum_ae.hef` and `model/spectrum_ae.json`
   in `/opt/vds-models/`, then set this in `/etc/default/vds-ai-advisor`:
   ```bash
   ADVISOR_ARGS=--backend hailo --hef /opt/vds-models/spectrum_ae.hef --meta /opt/vds-models/spectrum_ae.json
   ```
   Restart the service. The health panel then says "AI HAT+" instead of "CPU".

The Hailo backend has been checked against the HailoRT Python API but has **not yet
been run on an AI HAT+**. Test it on the device before relying on it. The CPU
backend and the training export are covered by the test suite.

## Other things the AI HAT+ could do here

The Hailo chips are built mainly for camera models, and the IRIV has two camera
ports. These ideas are not implemented. Each would stay advisory, like the
vibration advisor:

- **Gauge and display reading.** Read analogue pressure or temperature gauges next
  to the machine and trend them beside vibration.
- **Area awareness.** Note when a person is inside a guarded zone while vibration
  is high. It would log this only, never interlock.
- **Visual condition checks.** Spot belt misalignment, leaks or smoke. Thermal
  imaging would need a USB thermal camera.

## Safety

The advisor follows the project rules in `AGENTS.md`. Advisory agents report
findings and must not issue machine commands. The service has no network access.
It opens the historian read-only, and it can reach no device except
`/dev/hailo0`. The dashboard caps advisor findings at "warning", so they cannot
appear as alarms.
