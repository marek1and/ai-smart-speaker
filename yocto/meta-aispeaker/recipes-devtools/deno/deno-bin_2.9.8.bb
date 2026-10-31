SUMMARY = "Deno JavaScript runtime (official prebuilt binary)"
DESCRIPTION = "Runs yt-dlp's YouTube JS challenge solver (python3-yt-dlp-ejs). \
Building deno from source needs a Rust + V8 toolchain; the release binary is \
links only against glibc and libgcc."
HOMEPAGE = "https://deno.com"
LICENSE = "MIT"
# the release zip holds only the binary
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "https://github.com/denoland/deno/releases/download/v${PV}/deno-aarch64-unknown-linux-gnu.zip;subdir=${BP}"
SRC_URI[sha256sum] = "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905"

COMPATIBLE_HOST = "aarch64.*-linux"

S = "${UNPACKDIR}/${BP}"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -Dm 0755 ${S}/deno ${D}${bindir}/deno
}

INHIBIT_PACKAGE_STRIP = "1"
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INSANE_SKIP:${PN} += "already-stripped ldflags"
