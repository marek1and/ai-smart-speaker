SUMMARY = "Unprivileged 'speaker' service user + SSH keys"
DESCRIPTION = "All speaker services (pipewire, mopidy, the app) run as this user \
via User= in their system units — no user session, no linger. Password is \
locked — SSH key login only. The same key is installed for root (appliance \
administration; there is no sudo on the image). authorized_keys comes from \
the git-ignored yocto/local/ directory."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# files/authorized_keys is a symlink to yocto/local/authorized_keys (not in
# git) — create it with: cat ~/.ssh/id_*.pub > yocto/local/authorized_keys
SRC_URI = "file://authorized_keys"

S = "${UNPACKDIR}"

inherit useradd

USERADD_PACKAGES = "${PN}"
# gpio/spi/i2c: named by the RPi udev rules (99-com.rules), which otherwise
# log an error per rule on every boot
GROUPADD_PARAM:${PN} = "-f plugdev; -r -f gpio; -r -f spi; -r -f i2c"
USERADD_PARAM:${PN} = "-m -u 1000 -s /bin/sh -G audio,video,dialout,plugdev speaker"

do_install() {
    install -d -m 0700 ${D}/home/speaker/.ssh
    install -m 0600 ${S}/authorized_keys ${D}/home/speaker/.ssh/authorized_keys
    chown -R speaker:speaker ${D}/home/speaker

    install -d -m 0700 ${D}${ROOT_HOME}/.ssh
    install -m 0600 ${S}/authorized_keys ${D}${ROOT_HOME}/.ssh/authorized_keys
}

FILES:${PN} = "/home/speaker ${ROOT_HOME}"

# files belong to the dynamically created user
INSANE_SKIP:${PN} += "host-user-contaminated"
