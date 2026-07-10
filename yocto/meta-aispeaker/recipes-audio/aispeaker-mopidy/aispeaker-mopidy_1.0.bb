SUMMARY = "Mopidy music server configuration and system service"
DESCRIPTION = "Wires up Mopidy (baked into the image as recipes, not a venv) as \
a system service under the unprivileged 'speaker' user. Mopidy-MPD exposes the \
MPD protocol on 127.0.0.1:6600, so the application (python-mpd2) is unchanged. \
YouTube / YouTube Music work out of the box; Spotify is installed but disabled \
until the librespot GStreamer plugin and Premium credentials are provided \
(see the Spotify note in yocto/README.md)."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://mopidy.conf \
    file://mopidy.service \
"

S = "${UNPACKDIR}"

do_install() {
    # base (non-secret) config; secret/extension overrides live in
    # /data/mopidy/mopidy.conf, merged on top at runtime (never in git)
    install -d ${D}${sysconfdir}/mopidy
    install -m 0644 ${S}/mopidy.conf ${D}${sysconfdir}/mopidy/mopidy.conf

    install -d ${D}${sysconfdir}/systemd/system
    install -m 0644 ${S}/mopidy.service ${D}${sysconfdir}/systemd/system/
    install -d ${D}${sysconfdir}/systemd/system/multi-user.target.wants
    ln -s ${sysconfdir}/systemd/system/mopidy.service \
        ${D}${sysconfdir}/systemd/system/multi-user.target.wants/mopidy.service
}

FILES:${PN} = "${sysconfdir}"

# the server + the extensions we bake in (all system recipes now)
RDEPENDS:${PN} = "mopidy mopidy-mpd mopidy-youtube mopidy-spotify"
