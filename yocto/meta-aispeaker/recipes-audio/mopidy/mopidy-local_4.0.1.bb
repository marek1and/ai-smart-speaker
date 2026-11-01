SUMMARY = "Local music library for Mopidy (indexes and searches files by their tags)"
HOMEPAGE = "https://pypi.org/project/mopidy-local/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy_local"

SRC_URI[sha256sum] = "0becc68bcd637f39a8e89f62cda489feb76354279065c3673d1fbf6a0beb5708"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "mopidy python3-pykka python3-uritools"

# Same setuptools-scm handling as mopidy-mpd.
DEPENDS += "python3-setuptools-scm-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"
