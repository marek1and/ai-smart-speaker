SUMMARY = "reSpeaker XVF3800 host control tool (diagnostics) and udev rule"
DESCRIPTION = "Prebuilt xvf_host (Seeed upstream, rpi_64bit) for inspecting and \
tuning the XVF3800 over USB. No boot-time init: the device keeps its \
configuration in internal flash after SAVE_CONFIGURATION, so xvf_init.sh + \
init_commands.txt are installed only as the documented way to (re)apply the \
expected configuration manually. The udev rule is still required so the \
'speaker' user (xvf_host, pyusb in the app) can access the USB device."
HOMEPAGE = "https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY"
LICENSE = "CLOSED"

SRC_URI = " \
    git://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git;protocol=https;branch=master \
    file://xvf_init.sh \
    file://init_commands.txt \
    file://99-respeaker-xvf3800.rules \
"
# master @ 2026-07 (repo has no release tags)
SRCREV = "9bb8533342b8c00bcdee228e97ad0ae6cd6894fc"

PV = "1.0+git${SRCPV}"

S = "${UNPACKDIR}/git"
XVF_DIR = "${S}/host_control/rpi_64bit"

# prebuilt aarch64 binaries: no stripping, skip ldflags/rdeps QA
INHIBIT_PACKAGE_STRIP = "1"
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INSANE_SKIP:${PN} += "already-stripped ldflags file-rdeps dev-so"

do_install() {
    # only what is actually used: the USB control tool and its libraries
    # (skipped: libdevice_i2c.so, xvf_i2c_dfu, dfu_cmds.yaml — I2C/DFU paths
    # we don't use; convergence dumps are runtime artifacts, not inputs)
    install -d ${D}/opt/reSpeaker
    install -m 0755 ${XVF_DIR}/xvf_host ${D}/opt/reSpeaker/
    install -m 0644 ${XVF_DIR}/libcommand_map.so ${D}/opt/reSpeaker/
    install -m 0644 ${XVF_DIR}/libdevice_usb.so ${D}/opt/reSpeaker/
    install -m 0644 ${XVF_DIR}/transport_config.yaml ${D}/opt/reSpeaker/

    # xvf_init.sh / init_commands.txt are symlinks into linux/ in this repo —
    # init_commands.txt documents the configuration the device is expected
    # to have saved in flash
    install -m 0755 ${UNPACKDIR}/xvf_init.sh ${D}/opt/reSpeaker/
    install -m 0644 ${UNPACKDIR}/init_commands.txt ${D}/opt/reSpeaker/

    install -d ${D}${sysconfdir}/udev/rules.d
    install -m 0644 ${UNPACKDIR}/99-respeaker-xvf3800.rules ${D}${sysconfdir}/udev/rules.d/
}

FILES:${PN} += "/opt/reSpeaker"

RDEPENDS:${PN} = "bash usbutils libusb1"
