SUMMARY = "YouTube/stream extractor (Mopidy-YouTube playback)"
HOMEPAGE = "https://pypi.org/project/yt-dlp/"
LICENSE = "Unlicense"
LIC_FILES_CHKSUM = "file://LICENSE;md5=7246f848faa4e9c9fc0ea91122d6e680"

PYPI_PACKAGE = "yt_dlp"

SRC_URI[sha256sum] = "abe5c6b94c3b2f0560500add1c42e0a97641752743a5b65588ac2007a6e23eb6"

inherit pypi python_hatchling

# yt-dlp ships bash/zsh/fish completions into ${datadir}; useless on a headless
# appliance and otherwise trips the installed-vs-shipped QA check
do_install:append() {
    rm -rf ${D}${datadir}/bash-completion ${D}${datadir}/zsh ${D}${datadir}/fish
}

# YouTube needs its JS challenges solved: yt-dlp-ejs ships the solver scripts,
# deno (in the PATH) runs them. Without them every stream answers HTTP 403.
RDEPENDS:${PN} += "python3-ctypes python3-compression python3-yt-dlp-ejs deno-bin"
