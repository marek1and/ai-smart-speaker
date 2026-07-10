SUMMARY = "YouTube / YouTube Music backend for Mopidy"
HOMEPAGE = "https://pypi.org/project/mopidy-youtube/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

PYPI_PACKAGE = "mopidy_youtube"

SRC_URI[sha256sum] = "2a00fa704d7479f34fc8fd175a4a52977f3a560c3e69dece82aa3de99189b3bc"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "mopidy python3-beautifulsoup4 python3-cachetools python3-pykka python3-requests python3-setuptools python3-ytmusicapi python3-yt-dlp"

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
