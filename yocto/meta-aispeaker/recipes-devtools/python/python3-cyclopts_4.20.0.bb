SUMMARY = "CLI framework (Mopidy dependency)"
HOMEPAGE = "https://pypi.org/project/cyclopts/"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=4db124a215079420358276ea07be5a3e"

PYPI_PACKAGE = "cyclopts"

SRC_URI[sha256sum] = "1d819de2b12dc6b1c9f17ce0f4937d82922c0b83ac846eb4b3289c9c9f321c9f"

inherit pypi python_hatchling

# build-system requires hatch-vcs (version from scm); provide it natively and
# feed the version so it doesn't look for a .git in the sdist
DEPENDS += "python3-hatch-vcs-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"

RDEPENDS:${PN} += "python3-attrs python3-docstring-parser python3-rich python3-rich-rst"
