SUMMARY = "RAM installer initramfs — flashes the aispeaker image onto the SD card"
DESCRIPTION = "Tiny cpio.gz initramfs booted once via RPi5 tryboot. Contains \
busybox + the /init flasher (see aispeaker-installer) and the storage modules \
needed to read a USB stick and write /dev/mmcblk0. Build alongside \
aispeaker-image; stage with scripts/stage-inplace-install.sh."
LICENSE = "MIT"

PACKAGE_INSTALL = " \
    busybox \
    kmod \
    kernel-modules \
    aispeaker-installer \
    base-passwd \
"

# no getty/ssh/features — this is a single-shot appliance flasher
IMAGE_FEATURES = ""
IMAGE_LINGUAS = ""

export IMAGE_BASENAME = "aispeaker-installer-initramfs"
IMAGE_NAME_SUFFIX = ""
IMAGE_FSTYPES = "cpio.gz"

# /init from aispeaker-installer is PID1; no systemd in the initramfs
INITRAMFS_IMAGE_BUNDLE = "0"
INITRAMFS_MAXSIZE = "262144"

inherit image

# the initramfs owns /init directly
BAD_RECOMMENDATIONS += "busybox-syslog"
