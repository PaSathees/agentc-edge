#!/usr/bin/env bash
# edge-hub: SD card as /data (ERASES THE CARD), then Tailscale. Safe to re-run.
# Run: ssh -t sathees@HOST sudo bash hub-data.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
DEV=/dev/mmcblk0

if ! findmnt -n /data >/dev/null; then
  [ -b "$DEV" ] || { echo "$DEV not found"; exit 1; }
  size_gb=$(( $(blockdev --getsize64 "$DEV") / 1000000000 ))
  [ "$size_gb" -ge 14 ] && [ "$size_gb" -le 17 ] || { echo "$DEV is $size_gb GB, expected 16"; exit 1; }
  findmnt -S "${DEV}p1" >/dev/null && { echo "${DEV}p1 is mounted"; exit 1; }

  echo "--- current contents of the card"
  lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEV"
  tmp=$(mktemp -d)
  if mount -o ro "${DEV}p1" "$tmp" 2>/dev/null; then
    ls -la "$tmp"
    df -h "$tmp" | tail -1
    umount "$tmp"
  fi
  rmdir "$tmp"

  read -r -p "Type ERASE SD to destroy everything on $DEV: " answer
  [ "$answer" = "ERASE SD" ] || { echo "cancelled"; exit 1; }

  wipefs -a "$DEV"
  echo ',,L' | sfdisk --quiet --label gpt "$DEV"
  udevadm settle
  mkfs.ext4 -F -q -L edge-data -m 0 "${DEV}p1"
  uuid=$(blkid -s UUID -o value "${DEV}p1")

  # nofail: a dead card must not stop the boot. +i: nothing can write to the empty mountpoint
  mkdir -p /data
  chattr +i /data
  grep -q ' /data ' /etc/fstab || \
    echo "UUID=$uuid /data ext4 noatime,commit=60,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
  systemctl daemon-reload
  mount /data
  chown "${SUDO_USER:?}:${SUDO_USER}" /data
fi

if ! command -v tailscale >/dev/null; then
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled

echo "--- check"
df -h /data | tail -1
echo "--- Open the link below to approve this device, if one appears"
tailscale up --hostname=edge-hub
tailscale ip -4
