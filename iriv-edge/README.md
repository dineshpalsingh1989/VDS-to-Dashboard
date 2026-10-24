# IRIV Edge AI (CM5) deployment

This folder runs VDS-to-Dashboard on the
[Cytron IRIV Edge AI CM5](https://my.cytron.io/p-iriv-edge-ai-cm5) and adds a
**self-learning AI advisor**. The advisor learns what normal vibration looks like
for every VDS channel, watches the spectrum, envelope spectrum, waveform and
readings for changes, and matches each change against a library of machine fault
symptoms.

**The AI is an IRIV Edge AI feature only.** Other installs of the dashboard show no
AI controls and keep no learning samples. `install.sh` turns the AI on by setting
`VDS_EDGE_AI=1`.

| Device feature | Use in this project |
|---|---|
| Raspberry Pi CM5, 8/16 GB RAM, eMMC, fanless DIN-rail case, 10–30 V DC | Dashboard, MQTT, SQLite historian, gateways and the AI advisor |
| 2× Gigabit Ethernet | One port to the VDS sensor network, one to the plant/office network |
| M.2 NVMe slot | Optional SSD for AI data (see below) |
| AI HAT+ (Hailo-8L 13 TOPS or Hailo-8 26 TOPS) | Not needed for vibration; kept free for camera work (see below) |
| Isolated DI/DO, LED drivers, buzzer | **Deliberately not used.** The advisor never drives outputs |

## Try it first: the demo

```bash
python iriv-edge/demo/run_demo.py
```

This simulates four days of a small plant through the real dashboard and the real
AI, then serves the dashboard at http://127.0.0.1:8060. Press a sensor's
**Spectrum** button. See [demo/README.md](demo/README.md).

## What is in this folder

| Path | Purpose |
|---|---|
| `install.sh` | One-command setup: dashboard container, AI features on, advisor service |
| `ai_advisor.py` | The advisor: learns, detects changes, writes findings for the dashboard |
| `learning.py` | Self-learning profiles, envelope analysis, hourly trends |
| `symptoms.py` | Matches changes against the fault library |
| `storage.py` | Detects the NVMe SSD and keeps AI data there when mounted |
| `reports.py` | Writes the daily report folders |
| `setup-ssd.sh` | Prepares and mounts a blank NVMe SSD (asks before erasing) |
| `bearing_tool.py` | Adds bearings from the command line |
| `knowledge/` | Fault symptoms, bearing library (69 bearings), ISO severity zones. See [knowledge/README.md](knowledge/README.md) |
| `demo/` | Simulated plant to see the AI working, without hardware |
| `vds-ai-advisor.service` | Hardened systemd unit |

## Install

On the IRIV, using Cytron's Bookworm image, from the repository folder:

```bash
bash iriv-edge/install.sh            # build the dashboard on the device
bash iriv-edge/install.sh --image    # or pull the published container
```

Then, in the dashboard:

1. **Home: set up each channel with its gear icon.** Every channel card shows a
   small brain with what the AI is doing (AI off, Needs setup, Learning 40%,
   Watching, or the fault it found) and a gear that opens the **AI setup**.
   The AI will not learn a channel until it has three things, and the "AI on"
   switch stays locked until they are filled in:
   - the **part the sensor is on** (motor, pump, fan, gearbox, compressor or other);
   - the machine's **power in kW and its foundation** (rigid or flexible). These
     give the ISO vibration class: up to 15 kW is ISO 10816-1 class I, 15-300 kW
     is ISO 20816-3 group 2, above 300 kW is group 1. The window shows the zone
     limits A to D in mm/s;
   - a **running speed**: the VDS speed sensor must report one, or choose
     **Fixed speed** and type the RPM.

   **Where is the sensor?** Give every channel on one machine the same machine
   name, then set each channel's end (DE drive end or NDE free end) and direction
   (H horizontal, V vertical, A axial). Two channels can sit at the same end in
   different directions. A small drawing of motor, coupling and driven part shows
   each channel at its place. With this the AI compares the sensors of a machine:
   axial nearly as high as radial points to misalignment, vertical far above
   horizontal to looseness or a weak base, and one end far above the other to
   where the source is. Strong patterns become their own finding; otherwise they
   are added to the other findings under "Across the machine".

   The bearing number and mains frequency make the diagnosis sharper. Gear teeth
   appear only for a gearbox, blades or vanes only for a pump or fan. Saving with
   "AI on" also turns the AI on for that VDS. The same settings are in the asset
   registry, where a missing bearing can be added.
2. **The bell: why the AI thinks there is an issue.** When the AI has a finding,
   a bell flashes on the channel. It opens a plain-language explanation: what the
   AI thinks is wrong and how sure it is, what it saw (for example "at 88.8 Hz the
   vibration went from 0.42 to 2.6 mm/s, about 6 times normal"), a picture of the
   learned normal spectrum against today's in mm/s with the changed peaks circled
   and the matching fault frequency named, why it thinks so, what to do, and the
   Confirmed, Wrong and Repaired buttons. Rising readings and ISO alarms show the
   velocity trend over the ISO zones instead.
3. **Dashboard: open a sensor's Spectrum.** The spectrum shows the fault frequency
   lines (running speed, bearing, gear or blade pass, mains) and the strongest
   peaks with what each one matches.
4. **Health Agents** shows AI findings and learning progress for every sensor.
   **Reports and backups** lists the AI's report folders.

Viewers can read everything, but only an administrator can change settings or
give feedback.

For a native install without Docker, set `VDS_EDGE_AI=1` in the dashboard's
environment and run `install.sh --data-dir <dashboard data folder>`.

```bash
sudo python3 iriv-edge/ai_advisor.py --data-dir <data> --status   # learning progress per channel
journalctl -u vds-ai-advisor -f                                    # advisor log
```

## What the AI does

1. **Learns normal.** For each channel it builds a picture of normal, separately for
   each running speed (10 % bands) and measurement setting: the spectrum, the
   envelope spectrum, waveform level and impacts, and every overall reading.
   Monitoring starts after 48 samples spanning at least 12 hours of running.
2. **Spots changes.** It looks for new or growing peaks, a raised noise floor, more
   impacts (kurtosis, crest factor) and jumps in readings. A peak that only moves
   a little with speed is not a change. A finding opens after 2 of 3 changed
   samples and closes after 3 normal samples in a row.
3. **Envelope analysis for early bearing damage.** From each raw waveform it
   computes an envelope (demodulated) spectrum. Bearing defects make small impacts
   that show there first, often weeks before the normal spectrum. This needs the
   waveform sampled at 2 kHz or more; Health Agents says so when it is not.
4. **Names the likely fault.** Each change is matched against `knowledge/faults.json`
   using running speed, bearing frequencies, gear teeth, blades, mains frequency
   and sensor direction, and against faults people confirmed before.
5. **Trend and time to action.** It fits a trend to each channel's hourly Velocity
   (or Acceleration) over the window that fits best, from 1 to 7 days. A steady
   rise gets an estimate such as "reaches ISO zone C (2.8 mm/s) in about 9 days",
   or "twice its learned normal" when no ISO class is set. This also catches slow
   wear that never looks like a sudden change.
6. **Learns from people.** Each finding in the spectrum view has three buttons:
   - **Confirmed** keeps the fault's change pattern. If the same pattern appears
     again, on this or any sensor, the finding says "seen before" and names it.
   - **Wrong (normal operation)** closes the finding and quickly learns the pattern
     as normal.
   - **Repaired** makes the AI relearn the machine's new normal.
7. **Never learns a fault by accident.** Samples with changes, even borderline ones,
   do not teach the normal picture, so a slowly growing fault cannot become "normal".
8. **Scans quietly.** At most 20 learning samples per second (`--max-rate`), at low
   CPU and disk priority, so it never competes with the dashboard.

This is the AI's actual output from the demo, for a 6205-2RS bearing with an outer
race defect. The envelope spectrum found it about 13 simulated hours before the
normal spectrum did:

> Cooling pump P-101 / Motor drive end (PUMP-STATION-1 CH1): envelope spectrum
> (bearing impacts) changed from learned normal: peak at 88.0 Hz (3.55x) up 19 dB;
> peak at 176.0 Hz (7.11x) up 16 dB. Likely Early bearing outer race defect
> (6205-2RS), 100% match: bpfo (88.7 Hz) up 19 dB, 5 harmonics of bpfo. Found in the
> envelope spectrum, usually weeks before it shows in the normal spectrum.

## Report folders

Each day the AI works it writes a folder you can open if anything looks wrong:

```
<AI data>/reports/2026-10-23/
    activity.log                         what it analysed, new and cleared findings, feedback
    143005_GW1_CH1_envelope/             one folder per new finding
        report.md                        plain-language report: what changed, likely causes, what to check
        finding.json                     the finding as data
        envelope.csv or spectrum.csv     current and learned-normal level per frequency
        fault_frequencies.csv            every fault frequency of this machine at its speed
        waveform.json                    waveform features against normal (waveform findings)
```

The 50 latest reports can also be read on the dashboard's Reports and backups page. Folders older than 180 days
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

## Many VDS units: why it does not hang

The advisor is built so that more sensors make it work longer, never freeze.

- **Separate process, low priority.** It runs outside the dashboard, capped at half
  a CPU core and 1 GB of memory, so MQTT ingestion and the dashboard always come first.
- **Each sample is read once.** Learning uses running averages, so history is
  never re-scanned.
- **Fixed sample rate.** The dashboard keeps one spectrum and waveform per channel
  per interval, however fast sensors publish, and trims each channel's samples
  older than `VDS_LEARNING_RETENTION_HOURS` (default 6).
- **AI switch per VDS.** Only VDS units you switch on are sampled and analysed.
- **Time budget and rate cap.** Each cycle stops after half its interval. If it
  falls behind, it catches up later and shows its backlog in the health panel.
- **Separate learning file.** The historian is opened read-only, so the advisor
  never competes with dashboard writes.

Measured on a desktop PC for 1000 channels (250 VDS units with 4 channels each),
including envelope analysis. Expect a CM5 to be several times slower than this PC;
that still leaves large headroom.

| Workload | Needed | Measured |
|---|---|---|
| Spectrum (800 lines) and waveform (4096 points at 4 kHz), every 10 min | about 3 samples/s | about 1850 samples/s |
| Spectrum (3200 lines) and waveform (16384 points at 25.6 kHz), every 10 min | about 3 samples/s | about 860 samples/s |
| Overall readings, 6 per channel every minute | about 100/s | about 36000/s |

Storage for learning samples is about channels × (spectrum + waveform size) ×
retention ÷ interval. For 1000 channels with 3200-line spectra and 16k
waveforms at the defaults, that is about 2.9 GB. The learned profiles take about
20–30 MB for 1000 channels.

## Limits worth knowing

- It names the likely fault from symptoms. It does not replace an analyst's
  confirmation, phase readings or an inspection.
- Bearing faults are only named when the bearing is known. Without it, a new
  non-synchronous peak is still reported as a possible bearing defect.
- The bearing library covers the common deep groove series 60xx, 62xx and 63xx.
  Other makers' versions of the same number can differ slightly in ball count;
  add the exact bearing when you know it.
- Machines that run at many speeds take longer to learn, because each speed band
  learns separately. Trends mix all speeds.
- Envelope analysis needs waveforms sampled at 2 kHz or more.
- Time to action assumes the current rate continues. Wear often speeds up, so
  treat it as the latest sensible date, not a guarantee.

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
own folder (`<data>/ai/`, or the SSD). The dashboard caps advisor findings at
"warning", so they never appear as alarms. Feedback buttons only change what the
AI learns.
