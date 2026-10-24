#!/usr/bin/env bash
# One command to compile the neural model for both Hailo chips (AI HAT+ 13 and 26 TOPS).
# Needs an x86-64 Linux PC with Docker and Hailo's Dataflow Compiler wheel in iriv-edge/hailo/dfc/.
# Writes iriv-edge/models/vibration_brain_hailo8.hef and vibration_brain_hailo8l.hef; commit both.
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
[[ "$(uname -m)" == x86_64 ]] || { echo "Hailo's compiler runs on x86-64 Linux only, not on the IRIV." >&2; exit 1; }
compgen -G 'iriv-edge/hailo/dfc/*.whl' >/dev/null || {
  echo "Put Hailo's Dataflow Compiler wheel (from the Hailo Developer Zone) in iriv-edge/hailo/dfc/ first." >&2; exit 1; }
docker build -f iriv-edge/hailo/Dockerfile -t vds-hailo-compiler .
docker run --rm -u "$(id -u):$(id -g)" -v "$root:/repo" vds-hailo-compiler "$@"
ls -l iriv-edge/models/*.hef
echo "Next: git add iriv-edge/models/*.hef && git commit -m 'Add compiled Hailo models', then pull on the IRIV."
