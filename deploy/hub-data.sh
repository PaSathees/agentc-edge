#!/usr/bin/env bash
# edge-hub storage, then Tailscale. Safe to re-run.
#   /data   = partition 3 of the internal SSD that holds /. In the Debian installer make
#             EFI 512 MiB, / 32 GiB ext4, and a third partition on the rest, left unused.
#   /backup = SD card, nightly copy of energy.db (energy-backup.timer). Skipped with no card.
# Run from a copy of deploy/: ssh -t YOUR_USER@HOST sudo bash deploy/hub-data.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
user=${SUDO_USER:?}
opts=noatime,commit=60,nofail,x-systemd.device-timeout=10s

confirm() {  # device words
  lsblk -o NAME,SIZE,TRAN,MODEL,FSTYPE,LABEL "$1"
  read -r -p "Type $2 to destroy everything on $1: " answer
  [ "$answer" = "$2" ] || { echo "cancelled"; exit 1; }
}

add_mount() {  # dir uuid
  mkdir -p "$1"
  mountpoint -q "$1" || chattr +i "$1"   # nothing can write to the empty mountpoint
  grep -q " $1 " /etc/fstab || echo "UUID=$2 $1 ext4 $opts 0 2" >> /etc/fstab
  systemctl daemon-reload
  mountpoint -q "$1" || mount "$1"
  chown "$user:$user" "$1"
}

# /data: third partition of the SSD. nofail: a dead disk must not stop the boot
if ! findmnt -n /data >/dev/null; then
  disk=/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
  [ "$(lsblk -dno TRAN "$disk")" = sata ] || { echo "/ is not on a SATA disk ($disk)"; exit 1; }
  part=$(lsblk -lnpo NAME,TYPE "$disk" | awk '$2 == "part" {print $1}' | sed -n 3p)
  [ -n "$part" ] || { echo "$disk has no third partition, make one in the installer"; exit 1; }
  case "$(lsblk -no FSTYPE "$part")" in
    ext4) ;;
    "") confirm "$part" "ERASE DATA"; mkfs.ext4 -F -q -L edge-data -m 0 "$part" ;;
    *) echo "$part holds another filesystem, stopping"; exit 1 ;;
  esac
  add_mount /data "$(blkid -s UUID -o value "$part")"
fi

# /backup: SD card. An ext4 card is kept as is (old files stay); anything else is erased
card=/dev/mmcblk0
if [ -b "$card" ] && ! findmnt -n /backup >/dev/null; then
  if [ "$(lsblk -no FSTYPE "${card}p1" 2>/dev/null)" != ext4 ]; then
    confirm "$card" "ERASE SD"
    wipefs -a "$card"
    echo ',,L' | sfdisk --quiet --label gpt "$card"
    udevadm settle
    mkfs.ext4 -F -q -m 0 "${card}p1"
  fi
  e2label "${card}p1" edge-backup
  uuid=$(blkid -s UUID -o value "${card}p1")
  add_mount /backup "$uuid"
  # mount /backup again when the card is put back in
  sed "s/YOUR-CARD-UUID/$uuid/" "$here/99-backup-card.rules" > /etc/udev/rules.d/99-backup-card.rules
  udevadm control --reload
fi
if grep -q ' /backup ' /etc/fstab; then
  install -m 755 "$here/energy-backup.sh" /usr/local/bin/energy-backup
  install -m 644 "$here/energy-backup.service" "$here/energy-backup.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now energy-backup.timer
fi

if ! command -v tailscale >/dev/null; then
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled

echo "--- check"
df -h /data /backup 2>/dev/null | tail -n +2
systemctl list-timers energy-backup.timer --no-pager --no-legend || true
echo "--- Open the link below to approve this device, if one appears"
tailscale up --hostname=edge-hub
tailscale ip -4
