SUMMARY = "URI parsing, classification and composition (Mopidy-Local dependency)"
HOMEPAGE = "https://pypi.org/project/uritools/"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=079933dfba36eb60b5e3512ca0ab61ae"

PYPI_PACKAGE = "uritools"

SRC_URI[sha256sum] = "3a498e7e85ef3249343d5710618d641a414da0fbae6d23053ada7976ee83ea5f"

inherit pypi python_setuptools_build_meta

# Builds with setuptools-scm; the sdist has no git metadata to derive a version.
DEPENDS += "python3-setuptools-scm-native"
export SETUPTOOLS_SCM_PRETEND_VERSION = "${PV}"
