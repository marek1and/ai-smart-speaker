SUMMARY = "YouTube/stream extractor (Mopidy-YouTube playback)"
HOMEPAGE = "https://pypi.org/project/yt-dlp/"
LICENSE = "Unlicense"
LIC_FILES_CHKSUM = "file://LICENSE;md5=7246f848faa4e9c9fc0ea91122d6e680"

PYPI_PACKAGE = "yt_dlp"

SRC_URI[sha256sum] = "b094813404f87a9dd2186f00815231df32e5fd8a5403be0f807b3bb2d21a4432"

inherit pypi python_hatchling

# yt-dlp ships bash/zsh/fish completions into ${datadir}; useless on a headless
# appliance and otherwise trips the installed-vs-shipped QA check
do_install:append() {
    rm -rf ${D}${datadir}/bash-completion ${D}${datadir}/zsh ${D}${datadir}/fish
}

RDEPENDS:${PN} += "python3-ctypes python3-compression"
