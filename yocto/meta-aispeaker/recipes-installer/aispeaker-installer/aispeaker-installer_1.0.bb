SUMMARY = "initramfs /init that flashes the SD card from a USB stick"
DESCRIPTION = "PID1 for the in-place installer initramfs. Runs entirely from \
RAM, so it can overwrite the whole SD card (/dev/mmcblk0) with the aispeaker \
image streamed from a USB stick, then reboots into the freshly written system."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://init"

S = "${UNPACKDIR}"

do_install() {
    install -d ${D}
    install -m 0755 ${S}/init ${D}/init
}

FILES:${PN} = "/init"

# everything /init calls must be in the initramfs
RDEPENDS:${PN} = "busybox"
