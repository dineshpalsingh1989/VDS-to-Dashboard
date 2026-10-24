## VDS-to-Dashboard 1.2.0

**Plug and play.** A VDS130 that publishes for the first time waits on the new
**VDS130** page; click **Allow** and it starts running on Home. Nothing it sends is
used before that. Existing units keep running after the update.

**One-line install.** On 64-bit Linux:
`curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/VDS-to-Dashboard/main/scripts/install.sh | bash`
installs Docker if needed, downloads this release (checksum checked) and starts the
dashboard; on a Cytron IRIV Edge AI it also installs the AI. Run it again to update.

**One-command update.** `bash scripts/update.sh` fetches the latest version, keeps
`.env`, the database, recordings, backups and the AI's learning, and restarts the
dashboard the way it was started.

**Analysis from Home.** Spectrum or Waveform on a channel card opens the full analysis
for that channel, live first: trend history, spectrum with bearing defect lines,
waveform with turn markers, an impact finder that names the matching fault frequency,
an earlier waveform behind today's, orbit, and waterfall. It opens about twice as fast
as before, and Home no longer redraws every card every two seconds.

**Settings.** One Settings entry with tabs for Device, MQTT, Ethernet, Modbus TCP,
OPC UA, Data storage, the new **Database & files** (database, tables, retention,
backups, AI files) and Debug log.

**IRIV Edge AI.** Per-channel AI setup (machine type, ISO class from power and
foundation, speed, sensor position), a brain status and a flashing bell on each
channel card, plain-language explanations with a picture of normal against now in
mm/s, comparison of the sensors on one machine, and a neural second opinion on the
Hailo AI HAT+ (CPU when no HAT is fitted).

### Download

```bash
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf wilcoxon-vds-linux.tar.gz
cd wilcoxon-vds
bash scripts/linux-start.sh
```

Open http://127.0.0.1:8050 and sign in with `admin` and the `VDS_ADMIN_PASSWORD` the
launcher saved in `.env`. `bash scripts/linux-start.sh --image` pulls the prebuilt
Linux AMD64/ARM64 image instead of building. Docker Engine with the Compose plugin is
required; Python is inside the container.
