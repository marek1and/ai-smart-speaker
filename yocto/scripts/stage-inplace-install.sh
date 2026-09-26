#!/bin/bash
# Stage the tryboot in-place installer on a RUNNING Raspberry Pi (the speaker,
# or a spare Pi for testing) — no SD card removal, no case opening.
#
# It copies the installer kernel + initramfs into a separate directory on the
# boot partition (selected via os_prefix) and writes tryboot.txt. You then
# insert a USB stick holding aispeaker-image*.wic.bz2 and run
# `sudo reboot '0 tryboot'`. The firmware boots the installer ONCE (tryboot
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

# Everything the installer boots from goes into its own directory, selected by
# os_prefix below. Nothing of the running system is overwritten: copying the
# Yocto overlays over $BOOT/overlays used to leave the current OS booting its
# own kernel with another kernel's overlays after an aborted flash, which broke
# the "power-cycle returns to the current system" promise.
PREFIX=aispeaker-installer
DEST="$BOOT/$PREFIX"
rm -rf "$DEST"
mkdir -p "$DEST"

install -m 0644 "$KERNEL" "$DEST/kernel.img"
install -m 0644 "$INITRD" "$DEST/initramfs.img"
# os_prefix also applies to the device tree, overlays and cmdline.txt, so each
# of them has to exist under $DEST: the build's own when provided, otherwise a
# copy of what the current system boots with.
if ls "$SRC"/*.dtb >/dev/null 2>&1; then
    install -m 0644 "$SRC"/*.dtb "$DEST/"
else
    install -m 0644 "$BOOT"/*.dtb "$DEST/"
fi
if [ -d "$SRC/overlays" ]; then
    cp -r "$SRC/overlays" "$DEST/"
elif [ -d "$BOOT/overlays" ]; then
    cp -r "$BOOT/overlays" "$DEST/"
fi
[ -f "$BOOT/cmdline.txt" ] && install -m 0644 "$BOOT/cmdline.txt" "$DEST/cmdline.txt"

# With the tryboot flag set the firmware reads tryboot.txt INSTEAD of
# config.txt (not on top of it), so this file is the entire boot config for
# the installer run; config.txt stays untouched for the normal boot.
cat > "$BOOT/tryboot.txt" <<EOF
# One-shot in-place installer boot (RPi5 tryboot). Written by
# stage-inplace-install.sh; read instead of config.txt only on a tryboot.
[all]
arm_64bit=1
enable_uart=1
os_prefix=$PREFIX/
kernel=kernel.img
initramfs initramfs.img followkernel
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
