SUMMARY = "Extensible music server (MPD-protocol compatible)"
HOMEPAGE = "https://pypi.org/project/mopidy/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy"

SRC_URI[sha256sum] = "772c9f720273d0899f7257d8eff53816db671d51e4cd812885fefb983606456c"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "python3-cyclopts python3-httpx python3-platformdirs python3-pydantic python3-pygobject python3-pykka python3-rich python3-tornado"

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
