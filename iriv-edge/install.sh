#!/usr/bin/env bash
# Install VDS-to-Dashboard and its AI advisor on a Cytron IRIV Edge AI (CM5).
# Run on the device from the repository folder:  bash iriv-edge/install.sh
set -Eeuo pipefail

usage() {
  printf '%s\n' 'Usage: bash iriv-edge/install.sh [--image] [--with-hailo] [--no-advisor] [--data-dir PATH]' \
    '  --image        Pull the published container instead of building it on the device.' \
    '  --with-hailo   Install the AI HAT+ software (hailo-all) so the neural model runs on the HAT. Reboot afterwards.' \
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
  # Turn on learning samples (one spectrum and waveform per channel every 10 minutes)
  # unless .env already chooses an interval.
  ( umask 077; [[ -e .env ]] || cp .env.example .env )
  # This is an IRIV Edge AI: turn on the dashboard's AI features.
  if ! grep -Eq '^VDS_EDGE_AI=1$' .env; then
    sed -i '/^VDS_EDGE_AI=/d' .env
    printf '\nVDS_EDGE_AI=1\n' >> .env
    printf '%s\n' 'Enabled the AI analysis page in .env (VDS_EDGE_AI=1).'
  fi
  if ! grep -Eq '^VDS_LEARNING_INTERVAL_S=[1-9]' .env; then
    sed -i '/^VDS_LEARNING_INTERVAL_S=/d' .env
    printf '\nVDS_LEARNING_INTERVAL_S=600\n' >> .env
    printf '%s\n' 'Enabled AI learning samples in .env (VDS_LEARNING_INTERVAL_S=600).'
  fi
  bash scripts/linux-start.sh "$mode"
  data_dir="$(docker volume inspect wilcoxon-vds_vds-data --format '{{ .Mountpoint }}')"
fi
(( advisor )) || exit 0

sudo apt-get install -y python3-numpy
owner="$(sudo stat -c %u:%g "$data_dir")"
sudo install -d -o "${owner%:*}" -g "${owner#*:}" -m 0755 "$data_dir/ai"

# Keep an existing advisor configuration on reinstall.
if [[ ! -e /etc/default/vds-ai-advisor ]]; then
  printf '%s\n' '# Extra arguments for iriv-edge/ai_advisor.py, for example a longer learning period:' \
    '# ADVISOR_ARGS=--learn-samples 144 --learn-hours 48' \
    'ADVISOR_ARGS=' | sudo tee /etc/default/vds-ai-advisor >/dev/null
fi
sed -e "s#@REPO@#$root#g" -e "s#@DATA_DIR@#$data_dir#g" iriv-edge/vds-ai-advisor.service \
  | sudo tee /etc/systemd/system/vds-ai-advisor.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now vds-ai-advisor.service

if compgen -G '/sys/block/nvme*' >/dev/null && ! lsblk -nro MOUNTPOINT /dev/nvme0n1 2>/dev/null | grep -q .; then
  printf '%s\n' 'An NVMe SSD is fitted but not mounted. To keep AI data on it: sudo bash iriv-edge/setup-ssd.sh'
fi
printf '%s\n' 'AI advisor running. Next, open "AI analysis" in the dashboard to switch the AI on' \
  'for each VDS and describe each sensor (machine, location, bearing, speed).' \
  'It learns each channel first (about 12 hours of running), then quietly reports changes' \
  'and likely faults in the AI page and the health panel.' \
  "Progress: sudo python3 iriv-edge/ai_advisor.py --data-dir $data_dir --status" \
  'Logs: journalctl -u vds-ai-advisor -f' \
  'Settings: /etc/default/vds-ai-advisor, then: sudo systemctl restart vds-ai-advisor'
