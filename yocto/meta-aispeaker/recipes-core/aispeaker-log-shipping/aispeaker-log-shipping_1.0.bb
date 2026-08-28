SUMMARY = "Persistent journal on /data + periodic log shipping to the NAS"
DESCRIPTION = "The journal is kept on the writable data partition (a reboot no \
longer wipes the speaker's history) and a timer copies new entries to the NFS \
share, split per day with a retention window. Logging stays out of the \
application: a handler writing to NFS from inside the process would block the \
audio event loop when the NAS stalls, and would miss exactly what matters most \
— tracebacks, OOM kills and systemd's own restart messages."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://ship-speaker-logs.sh \
    file://ship-speaker-logs.service \
    file://ship-speaker-logs.timer \
    file://aispeaker-log-shipping.default \
    file://var-log-journal.mount \
"

S = "${UNPACKDIR}"

inherit systemd

# Only the timer and the mount are enabled; the service is what the timer
# triggers, so it must not be enabled on its own (it has no [Install] section).
SYSTEMD_SERVICE:${PN} = "ship-speaker-logs.timer var-log-journal.mount"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    install -d ${D}${sbindir}
    install -m 0755 ${S}/ship-speaker-logs.sh ${D}${sbindir}/ship-speaker-logs

    install -d ${D}${sysconfdir}/default
    install -m 0644 ${S}/aispeaker-log-shipping.default \
        ${D}${sysconfdir}/default/aispeaker-log-shipping

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/ship-speaker-logs.service ${D}${systemd_system_unitdir}/
    install -m 0644 ${S}/ship-speaker-logs.timer ${D}${systemd_system_unitdir}/
    install -m 0644 ${S}/var-log-journal.mount ${D}${systemd_system_unitdir}/
}

# The systemd class only adds the units listed in SYSTEMD_SERVICE to FILES, so
# ship-speaker-logs.service would otherwise be installed but unpackaged.
FILES:${PN} += " \
    ${sysconfdir}/default/aispeaker-log-shipping \
    ${systemd_system_unitdir}/ship-speaker-logs.service \
"

# nfs-utils-client provides the mount helper for the share the timer writes to
RDEPENDS:${PN} = "aispeaker-system-config nfs-utils-client"
