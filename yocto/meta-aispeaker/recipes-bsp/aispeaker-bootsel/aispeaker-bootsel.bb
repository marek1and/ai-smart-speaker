SUMMARY = "Contents of the bootsel partition: the firmware's A/B switch"
DESCRIPTION = "autoboot.txt for the first FAT partition. A normal boot uses the \
boot partition in [all], a tryboot (reboot '0 tryboot') the one in [tryboot], \
once. Slot A boots from p2, slot B from p3; the RAUC backend rewrites the file \
when an update is installed and when it is marked good."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://autoboot.txt"
S = "${UNPACKDIR}"

inherit deploy nopackages

do_deploy() {
    install -d ${DEPLOYDIR}/aispeaker-bootsel
    install -m 0644 ${S}/autoboot.txt ${DEPLOYDIR}/aispeaker-bootsel/
}
addtask deploy after do_install before do_build
