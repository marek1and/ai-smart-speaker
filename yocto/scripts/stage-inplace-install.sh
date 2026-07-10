#!/bin/bash
# Stage the tryboot in-place installer on a RUNNING Raspberry Pi (the speaker,
# or a spare Pi for testing) — no SD card removal, no case opening.
#
# It copies the installer kernel + initramfs to the boot partition and writes
# tryboot.txt. You then insert a USB stick holding aispeaker-image*.wic.bz2 and
# run `sudo reboot '0 tryboot'`. The firmware boots the installer ONCE (tryboot
# is one-shot: a power-cycle without a successful flash returns to the current
# system), the installer flashes the SD card from USB and reboots into Yocto.
#
# Run this ON the target over SSH. Copy the build artifacts to it first, e.g.:
#   scp build/tmp/deploy/images/raspberrypi5/{Image,*.dtb,\
#       aispeaker-installer-initramfs*.cpio.gz} pi@host:/tmp/installer/
#   scp -r .../overlays pi@host:/tmp/installer/        # if your overlays are needed
#
# Usage: sudo ./stage-inplace-install.sh /tmp/installer [/boot/firmware]
set -euo pipefail

SRC="${1:?usage: $0 <dir with Image + *initramfs*.cpio.gz + dtb/overlays> [bootdir]}"
BOOT="${2:-/boot/firmware}"   # Raspberry Pi OS mounts the FAT boot here

[ -d "$BOOT" ] || { echo "Boot partition dir not found: $BOOT"; exit 1; }

KERNEL="$(ls "$SRC"/Image "$SRC"/Image-* 2>/dev/null | head -n1)"
INITRD="$(ls "$SRC"/*initramfs*.cpio.gz 2>/dev/null | head -n1)"
[ -n "$KERNEL" ] || { echo "No kernel Image in $SRC"; exit 1; }
[ -n "$INITRD" ] || { echo "No *initramfs*.cpio.gz in $SRC"; exit 1; }

echo "Kernel:    $KERNEL"
echo "Initramfs: $INITRD"

install -m 0644 "$KERNEL" "$BOOT/tryboot-kernel.img"
install -m 0644 "$INITRD" "$BOOT/tryboot-initramfs.img"
# device tree + overlays: reuse whatever the current firmware already loads;
# tryboot.txt only overrides kernel/initramfs, the rest stays as-is
[ -d "$SRC/overlays" ] && cp -r "$SRC/overlays" "$BOOT/" || true

cat > "$BOOT/tryboot.txt" <<EOF
# One-shot in-place installer boot (RPi5 tryboot). Written by
# stage-inplace-install.sh; ignored on a normal boot.
[all]
arm_64bit=1
enable_uart=1
kernel=tryboot-kernel.img
initramfs tryboot-initramfs.img followkernel
EOF

sync
echo
echo "Staged into $BOOT."
echo "Next:"
echo "  1) plug a USB stick containing aispeaker-image*.wic.bz2 into the Pi"
echo "  2) sudo reboot '0 tryboot'"
echo
echo "Watch progress on the serial console (enable_uart=1) if you have it."
echo "If the flash does not complete, just power-cycle: tryboot is one-shot and"
echo "the current system boots again untouched."
