SUMMARY = "YouTube JS challenge solver scripts for yt-dlp"
HOMEPAGE = "https://pypi.org/project/yt-dlp-ejs/"
LICENSE = "Unlicense & MIT & ISC"
LIC_FILES_CHKSUM = "file://LICENSE;md5=f4c62131f879a8445e16a7f265aea635"

PYPI_PACKAGE = "yt_dlp_ejs"

# The sdist bundles the JS with pnpm/deno/npm and downloads node modules while
# building, so install the published wheel (pure Python + prebuilt .js) instead.
# The sdist stays in SRC_URI for the license file.
SRC_URI += "https://files.pythonhosted.org/packages/e3/bd/520769863744b669440a924271a6159ddd82ad5ae26b4ac4d4b69e9f8d44/yt_dlp_ejs-${PV}-py3-none-any.whl;name=wheel;subdir=wheel"
SRC_URI[sha256sum] = "d5fa1639f63b5c4af8d932495f60689d5370f1a095782c944f7f62a303eb104e"
SRC_URI[wheel.sha256sum] = "79300e5fca7f937a1eeede11f0456862c1b41107ce1d726871e0207424f4bdb4"

inherit pypi python_pep517

PEP517_WHEEL_PATH = "${UNPACKDIR}/wheel"
do_compile[noexec] = "1"
