#!/usr/bin/env bash
# edge-hub base setup: Debian on a pen drive, always on, few writes. Safe to re-run.
# Run: ssh -t sathees@HOST sudo bash hub-setup.sh, then reboot.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ -s "/home/${SUDO_USER:?}/.ssh/authorized_keys" ] || { echo "no SSH key installed, stopping"; exit 1; }

# Fewer writes: no swap
[ -e /etc/fstab.orig ] || cp /etc/fstab /etc/fstab.orig
swap_dev=$(awk '$3 == "swap" && $1 !~ /^#/ {print $1}' /etc/fstab | head -1)
if [ -n "$swap_dev" ]; then
  real_dev=$(findfs "$swap_dev")
  swapoff -a
  sed -i -E '/^[^#].*[[:space:]]swap[[:space:]]/s/^/#/' /etc/fstab
  wipefs -a "$real_dev"
fi
echo 'RESUME=none' > /etc/initramfs-tools/conf.d/resume

# Fewer writes: root mount options
sed -i -E '/[[:space:]]\/[[:space:]]+ext4/{/noatime/!s/errors=remount-ro/noatime,commit=60,errors=remount-ro/}' /etc/fstab

# Fewer writes: logs in RAM (lost on reboot)
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/edge.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=64M
EOF
rm -rf /var/log/journal

# Always on: ignore the lid, never sleep, no graphical target
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/edge.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl set-default multi-user.target

# Boots without an NVRAM entry; no USB autosuspend on the root stick; blank the screen
echo 'grub-efi-amd64 grub2/force_efi_extra_removable boolean true' | debconf-set-selections
mkdir -p /etc/default/grub.d
cat > /etc/default/grub.d/edge.cfg <<'EOF'
GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT usbcore.autosuspend=-1 consoleblank=60 systemd.swap=0"
EOF
dpkg-reconfigure -f noninteractive grub-efi-amd64
update-grub
update-initramfs -u

# SSH keys only
cat > /etc/ssh/sshd_config.d/edge.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sshd -t

apt-get update
apt-get install -y --no-install-recommends sqlite3 rsync curl ca-certificates

echo "--- check"
grep -E 'noatime|swap' /etc/fstab
ls /boot/efi/EFI/BOOT
echo "Done. Now run: sudo reboot"
