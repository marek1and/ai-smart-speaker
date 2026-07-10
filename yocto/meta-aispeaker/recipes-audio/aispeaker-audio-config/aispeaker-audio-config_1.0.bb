SUMMARY = "PipeWire/WirePlumber system services for the AI speaker audio graph"
DESCRIPTION = "Appliance model: PipeWire + WirePlumber run as system services \
under the unprivileged 'speaker' user (no user session, no linger, no rtkit — \
RT scheduling comes from unit rlimits). PipeWire runs in system mode with its \
socket in /run/pipewire; clients (WirePlumber, Mopidy, the app) find it via \
PIPEWIRE_RUNTIME_DIR / PULSE_SERVER. Audio configs are packaged from linux/."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# 50-fixed-clock.conf / 51-lowlatency-alsa.conf are symlinks into linux/ —
# single source of truth
SRC_URI = " \
    file://50-fixed-clock.conf \
    file://51-lowlatency-alsa.conf \
    file://pipewire.service \
    file://wireplumber.service \
    file://pipewire-pulse.service \
    file://aispeaker-env.sh \
"

S = "${UNPACKDIR}"

do_install() {
    # PipeWire: fixed 48 kHz clock, quantum 480 (10 ms) — XVF3800 requirement
    install -d ${D}${sysconfdir}/pipewire/pipewire.conf.d
    install -m 0644 ${S}/50-fixed-clock.conf ${D}${sysconfdir}/pipewire/pipewire.conf.d/

    # WirePlumber: XVF3800 always active, period 240x4
    install -d ${D}${sysconfdir}/wireplumber/wireplumber.conf.d
    install -m 0644 ${S}/51-lowlatency-alsa.conf ${D}${sysconfdir}/wireplumber/wireplumber.conf.d/

    # the pipewire stack only ships user units upstream — install system ones
    install -d ${D}${sysconfdir}/systemd/system
    install -m 0644 ${S}/pipewire.service ${D}${sysconfdir}/systemd/system/
    install -m 0644 ${S}/wireplumber.service ${D}${sysconfdir}/systemd/system/
    install -m 0644 ${S}/pipewire-pulse.service ${D}${sysconfdir}/systemd/system/

    install -d ${D}${sysconfdir}/systemd/system/multi-user.target.wants
    for unit in pipewire wireplumber pipewire-pulse; do
        ln -s ${sysconfdir}/systemd/system/${unit}.service \
            ${D}${sysconfdir}/systemd/system/multi-user.target.wants/${unit}.service
    done

    # interactive shells: point pw-cli/pactl/pip at the right places
    install -d ${D}${sysconfdir}/profile.d
    install -m 0644 ${S}/aispeaker-env.sh ${D}${sysconfdir}/profile.d/
}

FILES:${PN} = "${sysconfdir}"

RDEPENDS:${PN} = "pipewire wireplumber"
