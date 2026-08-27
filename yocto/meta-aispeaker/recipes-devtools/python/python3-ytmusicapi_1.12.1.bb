SUMMARY = "Unofficial YouTube Music API (Mopidy-YouTube dependency)"
HOMEPAGE = "https://pypi.org/project/ytmusicapi/"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=95addf2156011f40dfac1266911bb6f2"

PYPI_PACKAGE = "ytmusicapi"

SRC_URI[sha256sum] = "4e33f424db311ece1b9e5f29a51ade46b0697f6e2f284f8a40bcbd7219772755"

inherit pypi python_setuptools_build_meta

# build needs setuptools_scm (version from scm); provide it and feed the version
DEPENDS += "python3-setuptools-scm-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"

# No pyproject.toml rewriting needed: wrynose ships setuptools 82, which
# understands PEP 639 (SPDX license strings + license-files) natively. Under
# walnascar/setuptools 76 this recipe had to convert them to the old table form.

RDEPENDS:${PN} += "python3-requests"
