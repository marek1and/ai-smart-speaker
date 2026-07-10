SUMMARY = "Parse Python docstrings (cyclopts dependency)"
HOMEPAGE = "https://pypi.org/project/docstring-parser/"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE.md;md5=4014649477385d83f428a6adae447a49"

PYPI_PACKAGE = "docstring_parser"

SRC_URI[sha256sum] = "292510982205c12b1248696f44959db3cdd1740237a968ea1e2e7a900eeb2015"

inherit pypi python_hatchling
