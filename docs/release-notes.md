## Linux download and container distribution

Download the `wilcoxon-vds-linux.tar.gz` asset and `SHA256SUMS` below.
The archive contains the application source, documentation, and Docker launchers.
Docker Engine with the Docker Compose plugin is required; Python is bundled in
the container and does not need to be installed on the Linux host.

```bash
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf wilcoxon-vds-linux.tar.gz
cd wilcoxon-vds
bash scripts/linux-start.sh --build
```

The launcher creates a private `.env` with a generated administrator password if
one is not already configured. Open http://127.0.0.1:8050 on the Linux host and
read `VDS_ADMIN_PASSWORD` in your local `.env` to sign in. Keep that file private.
For a remote host, use `ssh -L 8050:127.0.0.1:8050 user@linux-host` and open the same
address on your own computer.

The `--build` path works from this public download without registry credentials.
The container package is public: `bash scripts/linux-start.sh --image` pulls the
prebuilt image without a GitHub login. Images target Linux AMD64 and ARM64 (64-bit).

Features include asset/channel mapping, contextual measurements, continuous
scalar history, recording jobs, alarms and acknowledgments, diagnostic plots,
advisory health checks, verified backups, and reports. SQLite data persists in a
Docker volume. An MQTT broker and physical VDS device are configured separately.

Automated checks cover Python syntax, dependency compatibility, tests, Linux
launcher syntax, container startup, HTTP routes, and verified backup creation.
These software checks do not replace commissioning with actual VDS/PLC hardware.

Container package: https://github.com/dineshpalsingh1989/VDS-to-Dashboard/pkgs/container/wilcoxon-vds
