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
| `storage.py` | Detects the NVMe SSD and keeps AI data there when mounted |
| `reports.py` | Writes the daily report folders |
| `setup-ssd.sh` | Prepares and mounts a blank NVMe SSD (asks before erasing) |
| `bearing_tool.py` | Adds bearings and shows their defect frequencies |
| `knowledge/` | Fault symptoms, bearings, ISO severity zones, your machine list. See [knowledge/README.md](knowledge/README.md) |
| `vds-ai-advisor.service` | Hardened systemd unit |

## Install

On the IRIV, using Cytron's Bookworm image, from the repository folder:

```bash
bash iriv-edge/install.sh            # build the dashboard on the device
bash iriv-edge/install.sh --image    # or pull the published container
```

Then open **AI analysis** in the dashboard sidebar:

1. **Switch the AI on per VDS.** Only switched-on units are analysed, and only
   they keep learning samples (one spectrum and waveform per channel every
   10 minutes, set by `VDS_LEARNING_INTERVAL_S`).
2. **Describe each sensor.** Enter the machine name, sensor location, machine type,
   direction, bearing number, gear teeth or blades, mains frequency and ISO class.
   The page shows whether the VDS reports a speed. If it has no speed sensor,
   choose **Fixed speed** and type the RPM, so the AI can still find running-speed
   and bearing frequencies.
3. **Watch learning progress** and read the **AI reports** on the same page. Viewers
   can read everything there, but only an administrator can change it.

`knowledge/machines.json` still works for scripted setups. Settings typed into the
AI page take priority over it.

```bash
sudo python3 iriv-edge/ai_advisor.py --data-dir <data> --status   # learning progress per channel
journalctl -u vds-ai-advisor -f                                    # advisor log
```

For a native install without Docker, run `install.sh --data-dir <dashboard data folder>`.

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
   growing fault cannot become "normal". After a repair, press **Relearn this
sensor** in the AI page.
5. **Quiet background scanning.** It scans at most 20 learning samples per second
   (`--max-rate`), at low CPU and disk priority, so it never competes with the dashboard.

## Report folders

Each day the AI works it writes a folder you can open if anything looks wrong:

```
<AI data>/reports/2026-10-23/
    activity.log                         what it analysed each cycle, new and cleared findings
    143005_GW1_CH1_spectrum/             one folder per new finding
        report.md                        plain-language report: what changed, likely causes, what to check
        finding.json                     the finding as data
        spectrum.csv                     current and learned-normal level per frequency
        waveform.json                    waveform features against normal (waveform findings)
```

The 50 latest reports can also be read in the AI page. Folders older than 180 days
are removed (`--report-days`).

## NVMe SSD

The IRIV has an M.2 slot for an NVMe SSD, which is faster and survives far more
writes than the built-in eMMC.

- **SSD already mounted.** The advisor notices it and announces in the dashboard
  that it will transfer its data to the SSD. On the next cycle it copies its
  learning file and report folders, verifies the copy and switches over. Learning
  is kept.
- **Blank SSD.** The dashboard says an SSD is fitted but not set up. Run
  `sudo bash iriv-edge/setup-ssd.sh`. It refuses to touch a disk that has data
  and asks you to type `ERASE nvme0n1` before formatting. Use `--use-existing`
  to mount an SSD that is already formatted.
- **SSD removed later.** AI analysis pauses with a warning instead of starting
  over on the eMMC, and resumes when the SSD is back.

The move covers the AI's own data. The dashboard database stays where Docker
keeps it.

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
- **AI switch per VDS.** Only VDS units you switch on are sampled and analysed.
- **Time budget and rate cap.** Each cycle stops after half its interval, and scanning is capped at 20 samples per second. If it ever falls
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
and no device access. It opens the historian read-only and writes only to its
own folder (`<data>/ai/`, or the SSD). The dashboard caps advisor findings at "warning", so they never
appear as alarms.
