#!/bin/bash
# Bakes the site configuration from provision-data.sh (WiFi profile, NFS units,
# /data skeleton) into a copy of the image. For the tryboot in-place install:
# the installer writes the card from a USB stick, so provision-data.sh never
# gets to the card afterwards, and a speaker without a WiFi profile has no way
# in (no passwords, no console login).
#
# Site config comes from yocto/local/ exactly as for provision-data.sh. The
# output contains the WiFi PSK — keep it off shared storage.
#
# Usage: sudo ./make-provisioned-image.sh [image.wic.bz2] [output.wic.bz2]
#   defaults: the latest aispeaker-image in the deploy dir ->
#             aispeaker-image-raspberrypi5.provisioned.wic.bz2 next to it
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY="$SCRIPT_DIR/../build/tmp/deploy/images/raspberrypi5"

IN="${1:-$DEPLOY/aispeaker-image-raspberrypi5.rootfs.wic.bz2}"
# the name must keep matching the installer's aispeaker-image*.wic.bz2 pattern
OUT="${2:-$DEPLOY/aispeaker-image-raspberrypi5.provisioned.wic.bz2}"
BMAP="${IN%.bz2}.bmap"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (losetup + mount)"; exit 1; }
[ -f "$IN" ] || { echo "Image not found: $IN"; exit 1; }
[ -f "$BMAP" ] || { echo "Block map not found: $BMAP"; exit 1; }

WORK="$(mktemp -d "$(dirname "$(readlink -f "$OUT")")/provision.XXXXXX")"
LOOP=""
cleanup() {
    [ -n "$LOOP" ] && losetup -d "$LOOP"
    rm -rf "$WORK"
}
trap cleanup EXIT

RAW="$WORK/image.wic"
# bmaptool writes only the mapped blocks, so the raw image stays sparse
bmaptool copy --bmap "$BMAP" "$IN" "$RAW"

LOOP="$(losetup --find --show --partscan "$RAW")"
# provision-data.sh finds the data partition by its label via lsblk
udevadm settle
"$SCRIPT_DIR/provision-data.sh" "$LOOP"
losetup -d "$LOOP"
LOOP=""

echo "Compressing to $OUT ..."
bzip2 -c "$RAW" > "$OUT.tmp"
# the installer runs the same test before it touches the card
bzip2 -t "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
[ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:$SUDO_GID" "$OUT"

echo "OK: $OUT"
echo "Put only this image on the USB stick: the installer takes the first aispeaker-image*.wic.bz2 it finds."
