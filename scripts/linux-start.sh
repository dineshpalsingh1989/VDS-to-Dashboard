#!/usr/bin/env bash
# Start the dashboard without installing Python on the Linux host.
set -Eeuo pipefail

usage() {
  printf '%s\n' 'Usage: bash scripts/linux-start.sh [--build|--image]' \
    '  --build  Build from the downloaded source (default; no registry login).' \
    '  --image  Pull the published ghcr.io container instead.'
}

mode="${1:---build}"
case "$mode" in
  --help|-h) usage; exit 0 ;;
  --build|--image) ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
command -v docker >/dev/null || { printf '%s\n' 'Install Docker Engine and the Docker Compose plugin first: https://docs.docker.com/engine/install/' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null

umask 077
if [[ ! -e .env ]]; then
  cp .env.example .env
fi
if ! grep -Eq '^VDS_ADMIN_PASSWORD=.+$' .env && [[ -z "${VDS_ADMIN_PASSWORD:-}" ]]; then
  password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  if grep -q '^VDS_ADMIN_PASSWORD=' .env; then
    sed -i "s/^VDS_ADMIN_PASSWORD=.*/VDS_ADMIN_PASSWORD=$password/" .env
  else
    printf '\nVDS_ADMIN_PASSWORD=%s\n' "$password" >> .env
  fi
  unset password
  printf '%s\n' 'Created an administrator password in .env. It is not printed to the terminal.'
fi
chmod 600 .env

file=docker/compose.yaml
if [[ "$mode" == --image ]]; then
  file=docker/compose.image.yaml
  if ! docker compose --env-file .env -f "$file" pull; then
    printf '%s\n' 'Image download failed. Check package visibility/authentication, or run this script with --build.' >&2
    exit 1
  fi
  docker compose --env-file .env -f "$file" up -d --wait --wait-timeout 120
else
  docker compose --env-file .env -f "$file" up -d --build --wait --wait-timeout 120
fi

printf '%s\n' 'Dashboard is healthy. Default address: http://127.0.0.1:8050' \
  'Login: VDS_ADMIN_USER and VDS_ADMIN_PASSWORD from .env (or your shell environment).' \
  'Configure your MQTT broker in the dashboard; this bundle does not start a broker.' \
  "Logs: docker compose --env-file .env -f $file logs --tail=100 dashboard" \
  "Stop: docker compose --env-file .env -f $file down (keep the data volume)."
