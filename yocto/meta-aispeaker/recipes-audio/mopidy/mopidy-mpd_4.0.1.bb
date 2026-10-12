SUMMARY = "MPD-protocol frontend for Mopidy (the app connects here)"
HOMEPAGE = "https://pypi.org/project/mopidy-mpd/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy_mpd"

SRC_URI[sha256sum] = "89630424725139e537614fa89655a278019058e218488f9115641bdc0184dcdc"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "mopidy python3-pygobject python3-pykka"

# The setuptools3 class does not pull setuptools-scm, which these packages need
# at build time. Provide it natively and feed it the version — the sdist has no
# git metadata to derive one from.
#
# wrynose ships setuptools 82, which satisfies the setuptools>=78 pin and
# understands PEP 639 (SPDX license strings, license-files) natively, so the
# pyproject.toml rewriting these recipes carried under walnascar/setuptools 76
# is gone.
DEPENDS += "python3-setuptools-scm-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"
