SUMMARY = "MPD-protocol frontend for Mopidy (the app connects here)"
HOMEPAGE = "https://pypi.org/project/mopidy-mpd/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy_mpd"

SRC_URI[sha256sum] = "0f118b1c46665c005575d13ad32906a0e477b698478ef6e950a7720a61e16ac8"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "mopidy python3-pygobject python3-pykka"

# walnascar ships setuptools 76 and the setuptools3 class does not pull
# setuptools-scm; these packages need both at build time. Provide scm natively,
# feed it the version (no git in the sdist), and relax the setuptools>=78 pin —
# they build fine with 76 (pure-Python, declarative PEP 621 metadata).
DEPENDS += "python3-setuptools-scm-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"

do_configure:append() {
    if [ -f ${S}/pyproject.toml ]; then
        sed -i -E \
            -e 's/^license = "([^"]+)"/license = {text = "\1"}/' \
            -e '/^license-files = /d' \
            -e 's/"setuptools[ >=<!,0-9.]*"/"setuptools"/g' \
            ${S}/pyproject.toml
    fi
}
