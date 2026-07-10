SUMMARY = "System configuration: journald in RAM, NM without WiFi powersave, /data layout"
DESCRIPTION = "Configuration for a read-only rootfs with minimal SD card wear: \
journal in RAM, NetworkManager/Bluetooth state bind-mounted from /data, \
/data directory skeleton created at boot, optional generic NFS share for \
recordings/logs (server address is site-specific and configured at \
provisioning time, not in git)."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://10-journald-volatile.conf \
    file://10-aispeaker-nm.conf \
    file://10-aispeaker-sshd.conf \
    file://aispeaker-data-setup.sh \
    file://aispeaker-data-setup.service \
    file://var-lib-NetworkManager.mount \
    file://var-lib-bluetooth.mount \
"

S = "${UNPACKDIR}"

inherit systemd

SYSTEMD_SERVICE:${PN} = " \
    aispeaker-data-setup.service \
    var-lib-NetworkManager.mount \
    var-lib-bluetooth.mount \
"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    # journal in RAM (no log writes to the SD card)
    install -d ${D}${sysconfdir}/systemd/journald.conf.d
    install -m 0644 ${S}/10-journald-volatile.conf ${D}${sysconfdir}/systemd/journald.conf.d/

    # WiFi without powersave, WiFi is the default uplink
    install -d ${D}${sysconfdir}/NetworkManager/conf.d
    install -m 0644 ${S}/10-aispeaker-nm.conf ${D}${sysconfdir}/NetworkManager/conf.d/

    # SSH: key-only, root login key-only (administration)
    install -d ${D}${sysconfdir}/ssh/sshd_config.d
    install -m 0644 ${S}/10-aispeaker-sshd.conf ${D}${sysconfdir}/ssh/sshd_config.d/

    # boot time: nothing on the image may block on the network — the app
    # reconnects on its own, so wait-online is masked
    install -d ${D}${sysconfdir}/systemd/system
    ln -s /dev/null ${D}${sysconfdir}/systemd/system/NetworkManager-wait-online.service

    install -d ${D}${sbindir}
    install -m 0755 ${S}/aispeaker-data-setup.sh ${D}${sbindir}/aispeaker-data-setup

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/aispeaker-data-setup.service ${D}${systemd_system_unitdir}/
    install -m 0644 ${S}/var-lib-NetworkManager.mount ${D}${systemd_system_unitdir}/
    install -m 0644 ${S}/var-lib-bluetooth.mount ${D}${systemd_system_unitdir}/
    # NFS mount/automount units are NOT baked here — scripts/provision-data.sh
    # generates them from yocto/local/nfs.env (server, mount point, options are
    # all site-specific) into the /etc overlay on /data.
}

FILES:${PN} = "${sysconfdir} ${sbindir} ${systemd_system_unitdir}"

RDEPENDS:${PN} = "networkmanager"
