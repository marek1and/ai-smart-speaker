SUMMARY = "reStructuredText renderer for rich (cyclopts dependency)"
HOMEPAGE = "https://pypi.org/project/rich-rst/"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=003ce70eb2eefc25bae92dd863e17dd9"

PYPI_PACKAGE = "rich_rst"

SRC_URI[sha256sum] = "f4d117b49697f338769759fa5cacf5197da4888b347b9fda2e50aef5cd8d93bd"

inherit pypi python_setuptools_build_meta

RDEPENDS:${PN} += "python3-rich python3-pygments python3-docutils"

# No pyproject.toml rewriting needed: wrynose ships setuptools 82, which
# understands PEP 639 (SPDX license strings + license-files) natively. Under
# walnascar/setuptools 76 this recipe had to convert them to the old table form.
