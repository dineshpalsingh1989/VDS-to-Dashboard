#!/usr/bin/env bash
# One-line install (and update) of VDS-to-Dashboard on 64-bit Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/VDS-to-Dashboard/main/scripts/install.sh | bash
#
# It installs Docker if it is missing, downloads the latest release (checksum checked), starts the
# dashboard with a new administrator password in .env, and prints the address. On a Cytron IRIV
# Edge AI (Raspberry Pi CM5) it also installs the AI advisor. Run it again to update; settings,
# data and the AI's learning are kept.
#
# Options (after "bash -s --" when piped):  --dir PATH   install folder (default ~/vds-to-dashboard)
#                                           --iriv       force the IRIV Edge AI install
#                                           --no-iriv    plain dashboard even on a CM5
#                                           --build      build the image on this computer instead of downloading it
set -Eeuo pipefail

repo="dineshpalsingh1989/VDS-to-Dashboard"
release="${VDS_RELEASE_BASE:-https://github.com/$repo/releases/latest/download}"
source_zip="${VDS_SOURCE_TARBALL:-https://github.com/$repo/archive/refs/heads/main.tar.gz}"
dir="${VDS_INSTALL_DIR:-$HOME/vds-to-dashboard}"
iriv=auto
mode=--image
user="${USER:-$(id -un)}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nInstall stopped: %s\n' "$*" >&2; exit 1; }

while (( $# )); do
  case "$1" in
    --dir) dir="${2:?--dir needs a folder}"; shift ;;
    --iriv) iriv=yes ;;
    --no-iriv) iriv=no ;;
    --build) mode=--build ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

[[ "$(uname -s)" == Linux ]] || die "this installer is for Linux. On Windows, see the README."
case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  *) die "a 64-bit system is needed (found $(uname -m)). On a Raspberry Pi, use Raspberry Pi OS 64-bit." ;;
esac
command -v curl >/dev/null || die "curl is missing: sudo apt-get install -y curl"
sudo_cmd=""
if (( EUID != 0 )); then
  command -v sudo >/dev/null || die "run as root or install sudo"
  sudo_cmd="sudo"
fi

# 1. Docker
if ! command -v docker >/dev/null; then
  say "Installing Docker (Docker's official installer)..."
  curl -fsSL https://get.docker.com | $sudo_cmd sh
fi
if ! docker compose version >/dev/null 2>&1; then
  say "Installing the Docker Compose plugin..."
  $sudo_cmd apt-get update -qq && $sudo_cmd apt-get install -y -qq docker-compose-plugin \
    || die "install the Docker Compose plugin: https://docs.docker.com/compose/install/linux/"
fi
docker_group=""
if ! docker info >/dev/null 2>&1; then
  $sudo_cmd systemctl enable --now docker >/dev/null 2>&1 || true
  if ! docker info >/dev/null 2>&1; then
    $sudo_cmd docker info >/dev/null 2>&1 || die "Docker is installed but not running: sudo systemctl start docker"
    # Let this user run Docker from now on; this shell is not in the group yet, so use it through sg below.
    $sudo_cmd usermod -aG docker "$user"
    docker_group="docker"
  fi
fi

# 2. Download the latest release (checksum checked), or the current source if there is no release yet
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
say "Downloading the latest VDS-to-Dashboard..."
if curl -fsSL "$release/wilcoxon-vds-linux.tar.gz" -o "$work/bundle.tar.gz" \
    && curl -fsSL "$release/SHA256SUMS" -o "$work/SHA256SUMS"; then
  cp "$work/bundle.tar.gz" "$work/wilcoxon-vds-linux.tar.gz"
  (cd "$work" && sha256sum --ignore-missing -c SHA256SUMS >/dev/null) || die "the download's checksum does not match; try again"
  tar -xzf "$work/bundle.tar.gz" -C "$work"
  unpacked="$work/wilcoxon-vds"
else
  printf '%s\n' 'No release download found; using the current source from GitHub instead.'
  curl -fsSL "$source_zip" -o "$work/source.tar.gz" || die "could not download from GitHub; check the internet connection"
  tar -xzf "$work/source.tar.gz" -C "$work"
  unpacked="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi
[[ -f "$unpacked/scripts/linux-start.sh" ]] || die "the download does not look like VDS-to-Dashboard"

# 3. Install or update in place: .env, compiled models and anything you added are kept
if [[ -f "$dir/.env" ]]; then
  say "Updating the existing install in $dir (settings and data are kept)..."
else
  say "Installing into $dir..."
fi
mkdir -p "$dir"
cp -a "$unpacked/." "$dir/"
cd "$dir"

# 4. Start: IRIV Edge AI gets the AI advisor too
if [[ "$iriv" == auto ]]; then
  iriv=no
  if grep -qi "compute module 5" /proc/device-tree/model 2>/dev/null || grep -Eq '^VDS_EDGE_AI=1$' .env 2>/dev/null; then
    iriv=yes
  fi
fi
run() {
  if [[ -z "$docker_group" ]]; then
    "$@"
  elif command -v sg >/dev/null; then
    sg "$docker_group" -c "$(printf '%q ' "$@")"
  else
    $sudo_cmd "$@"
  fi
}
if [[ "$iriv" == yes ]]; then
  say "Cytron IRIV Edge AI found: installing the dashboard and the AI advisor..."
  run bash iriv-edge/install.sh $( [[ "$mode" == --image ]] && echo --image )
else
  say "Starting the dashboard..."
  run bash scripts/linux-start.sh "$mode" || { [[ "$mode" == --image ]] && run bash scripts/linux-start.sh --build; }
fi

say "VDS-to-Dashboard is running."
printf '%s\n' \
  "  Open:      http://127.0.0.1:8050   (from another computer: ssh -L 8050:127.0.0.1:8050 $user@$(hostname))" \
  "  Login:     admin, password:  grep VDS_ADMIN_PASSWORD $dir/.env" \
  "  Next:      Settings > MQTT, then allow your VDS130 on the VDS130 page when it appears." \
  "  Update:    run this same line again, or: bash $dir/scripts/update.sh"
