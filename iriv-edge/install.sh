#!/usr/bin/env bash
# Install VDS-to-Dashboard and its AI advisor on a Cytron IRIV Edge AI (CM5).
# Run on the device from the repository folder:  bash iriv-edge/install.sh
set -Eeuo pipefail

usage() {
  printf '%s\n' 'Usage: bash iriv-edge/install.sh [--image] [--with-hailo] [--no-advisor] [--data-dir PATH]' \
    '  --image        Pull the published container instead of building it on the device.' \
    '  --with-hailo   Install the AI HAT+ software (apt package hailo-all). Reboot afterwards.' \
    '  --no-advisor   Start only the dashboard.' \
    '  --data-dir     Dashboard data folder when not using the Docker volume (native install).'
}

mode=--build; hailo=0; advisor=1; data_dir=""
while (( $# )); do
  case "$1" in
    --image) mode=--image ;;
    --with-hailo) hailo=1 ;;
    --no-advisor) advisor=0 ;;
    --data-dir) data_dir="${2:?--data-dir needs a path}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
[[ "$(uname -m)" == aarch64 ]] || printf '%s\n' 'Note: this is not a 64-bit Arm system; continuing anyway.' >&2

if (( hailo )); then
  sudo apt-get update
  sudo apt-get install -y hailo-all
  printf '%s\n' 'Installed hailo-all. Reboot, then run this script again without --with-hailo.'
  exit 0
fi

if [[ -z "$data_dir" ]]; then
  bash scripts/linux-start.sh "$mode"
  data_dir="$(docker volume inspect wilcoxon-vds_vds-data --format '{{ .Mountpoint }}')"
fi
(( advisor )) || exit 0

sudo apt-get install -y python3-numpy
owner="$(sudo stat -c %u:%g "$data_dir")"
sudo install -d -o "${owner%:*}" -g "${owner#*:}" -m 0755 "$data_dir/ai"

# Keep an existing advisor configuration (for example a Hailo backend) on reinstall.
if [[ ! -e /etc/default/vds-ai-advisor ]]; then
  printf '%s\n' '# Extra arguments for iriv-edge/ai_advisor.py. Examples:' \
    '# ADVISOR_ARGS=--backend hailo --hef /opt/vds-models/spectrum_ae.hef --meta /opt/vds-models/spectrum_ae.json' \
    'ADVISOR_ARGS=--backend cpu' | sudo tee /etc/default/vds-ai-advisor >/dev/null
fi
sed -e "s#@REPO@#$root#g" -e "s#@DATA_DIR@#$data_dir#g" iriv-edge/vds-ai-advisor.service \
  | sudo tee /etc/systemd/system/vds-ai-advisor.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now vds-ai-advisor.service

if [[ -e /dev/hailo0 ]]; then
  printf '%s\n' 'AI HAT+ detected at /dev/hailo0.'
else
  printf '%s\n' 'No AI HAT+ detected; the advisor uses the CPU backend.'
fi
printf '%s\n' 'AI advisor running. Findings appear in the dashboard health panel.' \
  'Logs: journalctl -u vds-ai-advisor -f' \
  'Settings: /etc/default/vds-ai-advisor, then: sudo systemctl restart vds-ai-advisor'
