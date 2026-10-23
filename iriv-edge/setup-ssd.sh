#!/usr/bin/env bash
# Prepare the IRIV Edge AI's M.2 NVMe SSD for AI data and mount it at /mnt/vds-ssd.
# The AI advisor notices the mounted SSD, announces it, and moves its data there.
#
#   sudo bash iriv-edge/setup-ssd.sh               # blank SSD: partition and format (asks to confirm)
#   sudo bash iriv-edge/setup-ssd.sh --use-existing  # SSD already formatted: only mount it
set -Eeuo pipefail

device=/dev/nvme0n1
mountpoint=/mnt/vds-ssd
mode=format
case "${1:-}" in
  "") ;;
  --use-existing) mode=existing ;;
  --help|-h) sed -n '2,7p' "$0"; exit 0 ;;
  *) printf 'Unknown option %s\n' "$1" >&2; exit 2 ;;
esac

[[ $EUID -eq 0 ]] || { printf '%s\n' 'Run with sudo.' >&2; exit 1; }
[[ -b $device ]] || { printf 'No NVMe SSD found at %s.\n' "$device" >&2; exit 1; }
if findmnt -rn -S "$device" >/dev/null || lsblk -nro MOUNTPOINT "$device" | grep -q .; then
  printf '%s\n' "The SSD is already mounted:" >&2
  lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$device" >&2
  printf '%s\n' 'The AI advisor uses a mounted SSD automatically. Nothing to do.' >&2
  exit 0
fi
lsblk -o NAME,SIZE,MODEL,FSTYPE "$device"

if [[ $mode == format ]]; then
  if [[ -n "$(lsblk -nro FSTYPE "$device" | tr -d '[:space:]')" ]] || [[ $(lsblk -nr "$device" | wc -l) -gt 1 ]]; then
    printf '%s\n' 'This SSD already has partitions or data. It will not be formatted.' \
      'To keep its data and only mount it: sudo bash iriv-edge/setup-ssd.sh --use-existing' >&2
    exit 1
  fi
  printf 'This ERASES %s and creates one ext4 partition for AI data.\n' "$device"
  read -r -p "Type ERASE $(basename "$device") to continue: " answer
  [[ $answer == "ERASE $(basename "$device")" ]] || { printf '%s\n' 'Cancelled.'; exit 1; }
  parted -s "$device" mklabel gpt mkpart vds-ai ext4 1MiB 100%
  partprobe "$device"
  udevadm settle
  mkfs.ext4 -q -L vds-ai "${device}p1"
fi

partition="${device}p1"
[[ -b $partition ]] || { printf 'Expected partition %s was not found.\n' "$partition" >&2; exit 1; }
uuid="$(blkid -s UUID -o value "$partition")"
mkdir -p "$mountpoint"
if ! grep -q "UUID=$uuid" /etc/fstab; then
  printf 'UUID=%s %s auto defaults,nofail,noatime 0 2\n' "$uuid" "$mountpoint" >> /etc/fstab
fi
systemctl daemon-reload
mount "$mountpoint"
printf 'SSD mounted at %s. The AI advisor will announce it in the dashboard and then\n' "$mountpoint"
printf '%s\n' 'transfer its data onto the SSD within two cycles (about two minutes).'
