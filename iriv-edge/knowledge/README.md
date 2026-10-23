# Knowledge folder

The AI advisor reads these files every time it starts. Edit them, then run
`sudo systemctl restart vds-ai-advisor`.

| File | What it holds |
|---|---|
| `faults.json` | Fault symptom library: unbalance, misalignment, looseness, bearing defects, gear, blade pass, electrical, cavitation and impacting |
| `bearings.csv` | Bearing defect frequencies (BPFO, BPFI, BSF, FTF) as multiples of shaft speed |
| `severity.json` | ISO 20816-3 velocity zones A–D |
| `machines.example.json` | Template describing each VDS channel; copy it to `machines.json` |

## Describe your machines

Copy `machines.example.json` to `machines.json`. Add one entry per channel,
named `GATEWAY_ID/CHANNEL`. The most useful fields are:

- **`bearing`:** the bearing number. This turns "unknown peak" into "outer race defect".
- **`machine_type`:** some faults only apply to one type. Cavitation is pump-only,
  and electrical faults are motor-only.
- **`direction`:** radial or axial. Misalignment shows more strongly in the axial direction.
- **`gear_teeth` and `blades`:** used for gear mesh and blade pass frequencies.
- **`iso_class`:** turns on ISO severity zones for that channel.

You can skip `machines.json` and type the bearing number into the dashboard's
asset registry instead, for example "DE bearing 6205-2RS" in the location field.
The advisor finds it there too.

## Add bearings

Only bearings with published geometry are included, so every frequency here can
be traced to a source. Add your own bearings from the manufacturer's catalogue
or online bearing frequency calculator:

```bash
# From geometry: number of balls, ball diameter and pitch diameter in mm
python3 iriv-edge/bearing_tool.py 6206-2RS --balls 9 --ball-diameter 9.525 --pitch-diameter 46 --source "SKF catalogue"
# Or from published defect frequencies (multiples of shaft speed)
python3 iriv-edge/bearing_tool.py 22216-E --orders 7.62 10.38 3.21 0.42 --source "manufacturer calculator"
# Show the frequencies in Hz at a running speed
python3 iriv-edge/bearing_tool.py 6205-2RS --rpm 1480
```

A designation like `6205-2RS` also matches `6205`, because seals do not change
the defect frequencies.

## Add or tune a fault

Each fault in `faults.json` lists evidence items with a weight. The advisor
only suggests a fault when all of these hold:

1. The spectrum or waveform has **changed** from what it learned as normal.
2. Every evidence item marked `key` matches.
3. The matched weight is at least 60 % of the weight it could check.

Frequencies are written as `1x`, `2x`, `0.5x`, `line`, `2line`, `bpfo`,
`2bpfi`, `bsf`, `ftf`, `gmf` or `bpf`. Evidence types:

| Type | Meaning |
|---|---|
| `rise` | Peak at a frequency is at least `min_db` above the learned normal |
| `present` | Clear peak at a frequency, `min_prominence_db` over the local noise floor |
| `ratio` | Amplitude ratio between two frequencies, `min` and/or `max` |
| `harmonics` | At least `count` clear harmonics of a frequency |
| `sidebands` | Peaks on both sides of a frequency at `spacing` |
| `floor_rise` | Noise floor up by `min_db`, over `all` or the `upper` half of the spectrum |
| `nonsync_rise` | New peak that is not a multiple of running speed or line frequency |
| `waveform` | Waveform feature (`kurtosis`, `crest`, `skewness`) within `min`/`max` |
| `waveform_rise` | Waveform feature increased by `min_change` from normal |
| `direction` | Sensor direction contains one of the words in `any` |

Add `"negate": true` to require that evidence is absent. Use `requires`,
`unless` and `machine_types` to limit where a fault applies. The test suite
checks that every rule uses known evidence types.
