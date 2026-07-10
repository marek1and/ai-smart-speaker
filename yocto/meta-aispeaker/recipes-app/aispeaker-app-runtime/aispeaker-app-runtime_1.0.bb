SUMMARY = "Runtime scaffolding for the ai-smart-speaker application"
DESCRIPTION = "The application is NOT baked into the image — it lives on /data \
(deployed via application/sync-to-rpi.sh + pip install in a venv, as today). \
This recipe provides the /opt/ai-smart-speaker -> /data/opt/ai-smart-speaker \
symlink and a system unit (User=speaker) that starts the app once the venv \
exists."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://ai-smart-speaker.service"

S = "${UNPACKDIR}"

inherit systemd

SYSTEMD_SERVICE:${PN} = "ai-smart-speaker.service"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    # paths from the Raspbian install stay valid: /opt/ai-smart-speaker
    # points into /data
    install -d ${D}/opt
    ln -s /data/opt/ai-smart-speaker ${D}/opt/ai-smart-speaker

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/ai-smart-speaker.service ${D}${systemd_system_unitdir}/
}

FILES:${PN} = "/opt ${systemd_system_unitdir}"

RDEPENDS:${PN} = "python3 python3-venv python3-pip aispeaker-audio-config"
