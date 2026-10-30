SUMMARY = "A/B system updates with RAUC and the RPi5 firmware's tryboot"
DESCRIPTION = "The RAUC bootloader backend that drives autoboot.txt, the \
post-install handler that fills the new slot's boot partition, the \
aispeaker-update command (install + tryboot), the health check that confirms \
a slot, and the hardware watchdog that resets a hung system."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://rpi-tryboot-backend \
    file://install-boot-partition \
    file://aispeaker-update \
    file://aispeaker-health-check \
    file://aispeaker-health-check.service \
    file://10-aispeaker-watchdog.conf \
"

S = "${UNPACKDIR}"

inherit systemd

SYSTEMD_SERVICE:${PN} = "aispeaker-health-check.service"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    install -d ${D}${nonarch_libdir}/aispeaker-update
    install -m 0755 ${S}/rpi-tryboot-backend ${D}${nonarch_libdir}/aispeaker-update/
    install -m 0755 ${S}/install-boot-partition ${D}${nonarch_libdir}/aispeaker-update/

    install -d ${D}${sbindir}
    install -m 0755 ${S}/aispeaker-update ${D}${sbindir}/
    install -m 0755 ${S}/aispeaker-health-check ${D}${sbindir}/

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/aispeaker-health-check.service ${D}${systemd_system_unitdir}/

    install -d ${D}${systemd_unitdir}/system.conf.d
    install -m 0644 ${S}/10-aispeaker-watchdog.conf ${D}${systemd_unitdir}/system.conf.d/
}

FILES:${PN} = " \
    ${nonarch_libdir}/aispeaker-update \
    ${sbindir} \
    ${systemd_system_unitdir} \
    ${systemd_unitdir}/system.conf.d \
"

RDEPENDS:${PN} = "rauc dosfstools networkmanager-nmcli"
