#!/usr/bin/env bash
# Update VDS-to-Dashboard to the latest version and restart it, in one command:
#   bash scripts/update.sh
# Keeps .env (passwords, settings), the database volume, recordings, backups and the AI's learning.
# Works for a git clone (git pull) and for a downloaded release (fetches the latest release bundle).
# On a Cytron IRIV Edge AI (VDS_EDGE_AI=1 in .env) it also updates and restarts the AI advisor.
set -Eeuo pipefail

repo="dineshpalsingh1989/VDS-to-Dashboard"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

before="$(cat VERSION 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ -d .git ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    printf '%s\n' 'Local changes to tracked files were found. Commit or stash them, then run the update again:' >&2
    git status --short --untracked-files=no >&2
    exit 1
  fi
  git pull --ff-only
else
  # Release bundle: download the latest one, check its checksum, and unpack it over this folder.
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  base="https://github.com/$repo/releases/latest/download"
  curl -fsSL "$base/wilcoxon-vds-linux.tar.gz" -o "$work/wilcoxon-vds-linux.tar.gz"
  curl -fsSL "$base/SHA256SUMS" -o "$work/SHA256SUMS"
  (cd "$work" && sha256sum --ignore-missing -c SHA256SUMS)
  tar -xzf "$work/wilcoxon-vds-linux.tar.gz" -C "$work"
  # .env and anything you added (compiled Hailo models, recordings) are not in the bundle, so they stay.
  cp -a "$work/wilcoxon-vds/." "$root/"
fi
after="$(cat VERSION 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf 'Updated from %s to %s.\n' "$before" "$after"

if grep -Eq '^VDS_EDGE_AI=1$' .env 2>/dev/null; then
  # IRIV Edge AI: the installer is safe to run again; it rebuilds the dashboard and restarts the advisor.
  bash iriv-edge/install.sh
elif grep -Eq '^VDS_START_MODE=image$' .env 2>/dev/null; then
  bash scripts/linux-start.sh --image
else
  bash scripts/linux-start.sh --build
fi
printf '%s\n' 'Update finished. Your settings, data and AI learning were kept.'
