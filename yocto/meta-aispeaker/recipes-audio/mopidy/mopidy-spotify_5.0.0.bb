SUMMARY = "Spotify backend for Mopidy (needs gst-plugins-spotify + Premium)"
HOMEPAGE = "https://pypi.org/project/mopidy-spotify/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy_spotify"

SRC_URI[sha256sum] = "d8f24bf897f6ac35eb38a87f9884f74a9fcca54fc48c42f6ea45c76239991cc2"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "mopidy python3-pykka python3-requests"

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
