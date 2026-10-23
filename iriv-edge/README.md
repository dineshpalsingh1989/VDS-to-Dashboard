# IRIV Edge AI (CM5) deployment

This folder runs VDS-to-Dashboard on the
[Cytron IRIV Edge AI CM5](https://my.cytron.io/p-iriv-edge-ai-cm5) and adds a
**self-learning AI advisor**. The advisor learns what normal vibration looks like
for every VDS channel, watches the spectrum and waveform for changes, and matches
each change against a library of machine fault symptoms.

| Device feature | Use in this project |
|---|---|
| Raspberry Pi CM5, 8/16 GB RAM, eMMC, fanless DIN-rail case, 10–30 V DC | Dashboard, MQTT, SQLite historian, gateways and the AI advisor |
| 2× Gigabit Ethernet | One port to the VDS sensor network, one to the plant/office network |
| AI HAT+ (Hailo-8L 13 TOPS or Hailo-8 26 TOPS) | Not needed for vibration; kept free for camera work (see below) |
| Isolated DI/DO, LED drivers, buzzer | **Deliberately not used.** The advisor never drives outputs |

## What is in this folder

| Path | Purpose |
|---|---|
| `install.sh` | One-command setup: dashboard container, learning samples, advisor service |
| `ai_advisor.py` | The advisor: learns, detects changes, writes findings for the dashboard |
| `learning.py` | Self-learning profiles per channel, speed and setting |
| `symptoms.py` | Matches changes against the fault library |
| `bearing_tool.py` | Adds bearings and shows their defect frequencies |
| `knowledge/` | Fault symptoms, bearings, ISO severity zones, your machine list. See [knowledge/README.md](knowledge/README.md) |
| `vds-ai-advisor.service` | Hardened systemd unit |

## Install

On the IRIV, using Cytron's Bookworm image, from the repository folder:

```bash
bash iriv-edge/install.sh            # build the dashboard on the device
bash iriv-edge/install.sh --image    # or pull the published container
```

The installer sets `VDS_LEARNING_INTERVAL_S=600` in `.env`. The dashboard then
keeps one spectrum and one waveform per channel every 10 minutes for the advisor.
Then describe your machines and bearings in `knowledge/machines.json`. Copy
`knowledge/machines.example.json` to start.

```bash
sudo python3 iriv-edge/ai_advisor.py --data-dir <data> --status   # learning progress per channel
journalctl -u vds-ai-advisor -f                                    # advisor log
```

For a native install without Docker, set `VDS_LEARNING_INTERVAL_S=600` in the
dashboard's environment and run `install.sh --data-dir <dashboard data folder>`.

## How it learns

1. **Learning.** For each channel it builds a picture of normal, separately for each
   running speed (10 % bands) and each measurement setting. That picture covers the
   spectrum shape, waveform level and impacts, and every overall reading. A channel
   starts monitoring after 48 samples spanning at least 12 hours of running. Change
   this with `--learn-samples` and `--learn-hours`.
2. **Monitoring.** Each new sample is compared with its learned normal. It looks for
   new or growing peaks, a raised noise floor, more impacts (kurtosis, crest factor)
   and jumps in overall readings. A change must show in 2 of the last 3 samples
   before it is reported.
3. **Explaining.** Each change is matched against `knowledge/faults.json` using
   running speed, bearing frequencies, gear teeth, blades, line frequency and
   sensor direction. The finding names the most likely fault with the evidence
   behind it.
4. **Keeps learning, safely.** Normal samples keep refining the picture, so slow
   seasonal drift is followed. Samples that look abnormal are never learned, so a
   growing fault cannot become "normal". After a repair, relearn the channel:

```bash
sudo python3 iriv-edge/ai_advisor.py --data-dir <data> --relearn GW1/1
```

This is the advisor's actual output for a simulated machine that developed an
outer race fault on a 6205-2RS bearing:

> Fan F-1 motor DE (GW1 CH1): spectrum changed from learned normal: peak at 90.0 Hz
> (3.60x) up 17 dB; peak at 178.8 Hz (7.15x) up 13 dB. Likely Bearing outer race
> defect (6205-2RS), 100% match: bpfo (89.6 Hz) up 17 dB, 2 harmonics of bpfo,
> kurtosis 29.1. Plan bearing inspection. Check lubrication now; outer race damage
> usually progresses steadily, so trend it weekly.

## Many VDS units: why it does not hang

The advisor is built so that more sensors make it work longer, never freeze.

- **Separate process, low priority.** It runs outside the dashboard with a low CPU
  and disk priority. It is capped at half a CPU core and 1 GB of memory, so MQTT
  ingestion and the dashboard always come first.
- **Each sample is read once.** It remembers where it stopped and only reads new
  samples. Learning uses running averages, so history is never re-scanned.
- **Fixed sample rate.** The dashboard keeps one spectrum and waveform per channel
  per interval, however fast sensors publish. Each channel trims its own samples
  older than `VDS_LEARNING_RETENTION_HOURS`, which defaults to 6, so storage stays bounded.
- **Time budget.** Each cycle stops after half its interval. If it ever falls
  behind, it catches up in later cycles and shows its backlog in the health panel.
- **Separate learning file.** The learned profiles live in `ai/learning.db`. The
  historian is opened read-only, so the advisor never competes with dashboard writes.

Measured on a desktop PC for 1000 channels (250 VDS units with 4 channels each).
Expect a CM5 to be several times slower than this PC; that still leaves large headroom.

| Workload | Needed | Measured |
|---|---|---|
| Spectrum (800 lines) and waveform (4096 points), every 10 min | about 3 samples/s | about 4300 samples/s |
| Spectrum (3200 lines) and waveform (16384 points), every 10 min | about 3 samples/s | about 1900 samples/s |
| Overall readings, 6 per channel every minute | about 100/s | about 36000/s |

Storage for learning samples is about channels × (spectrum + waveform size) ×
retention ÷ interval. For 1000 channels with 3200-line spectra and 16k
waveforms at the defaults, that is about 2.9 GB. Raise `VDS_LEARNING_INTERVAL_S`
or lower `VDS_LEARNING_RETENTION_HOURS` for very large sites. The learned
profiles themselves take about 15–30 MB for 1000 channels.

## Limits worth knowing

- It names the likely fault from symptoms. It does not replace an analyst's
  confirmation, phase readings or an inspection.
- Bearing faults are only named when the bearing is known. Without it, a new
  non-synchronous peak is still reported as a possible bearing defect.
- Machines that run at many speeds take longer to learn, because each speed band
  learns separately.
- Very early bearing damage often shows only in envelope (demodulated) spectra.
  The VDS publishes a power spectrum, so detection starts once the defect peaks
  or impacts appear there.

## The AI HAT+

Vibration learning runs on the CM5 processor; the measurements above show it has
plenty of capacity. The AI HAT+ can only run models that were trained and
compiled on a PC, and cannot learn on the device, so it is not used for this.
It is a good fit for camera work on the IRIV's two camera ports. None of these
ideas are built yet:

- **Gauge reading.** Read analogue pressure and temperature gauges and trend them
  beside vibration.
- **Visual checks.** Spot leaks, smoke or belt misalignment.
- **Area awareness.** Log when a person is near a machine whose vibration is
  high. This would only log, never interlock.

`install.sh --with-hailo` installs the HAT software for that future work.

## Safety

The advisor follows the project rules in `AGENTS.md`: advisory agents report
findings and must not issue machine commands. The service has no network access
and no device access. It opens the historian read-only and writes only to
`<data>/ai/`. The dashboard caps advisor findings at "warning", so they never
appear as alarms.
