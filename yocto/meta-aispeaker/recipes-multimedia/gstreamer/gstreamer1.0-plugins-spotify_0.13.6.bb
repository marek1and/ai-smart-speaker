SUMMARY = "GStreamer 'spotifyaudiosrc' element (librespot) for Mopidy-Spotify"
DESCRIPTION = "The Rust gst-plugins-rs 'spotify' plugin. Mopidy-Spotify 5.x \
plays audio through this element (librespot-based), so Spotify needs it plus a \
Spotify Premium account. \
\
OPT-IN / NOT WIRED INTO THE IMAGE BY DEFAULT: this recipe is a scaffold. A Rust \
GStreamer plugin pulls a large crate tree whose per-crate SRC_URIs + checksums \
must be GENERATED, not hand-written. Do that once with cargo-bitbake, then add \
gstreamer1.0-plugins-spotify to CORE_IMAGE_EXTRA_INSTALL and set \
[spotify] enabled = true in mopidy.conf. See yocto/README.md (Spotify)."
HOMEPAGE = "https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs"
LICENSE = "MPL-2.0"
LIC_FILES_CHKSUM = "file://LICENSE-MPL-2.0;md5=815ca599c9df247a0c7f619bab123dad"

SRC_URI = " \
    git://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git;protocol=https;branch=main;destsuffix=git \
    ${@bb.utils.contains('AISPEAKER_SPOTIFY_CARGO', '1', 'file://crates.inc', '', d)} \
"
# gst-plugins-rs 0.13.x is the series matching the plugin version above; pin an
# exact SRCREV after picking a release tag.
SRCREV = "${AUTOREV}"
# S deliberately unset — bitbake.conf derives it from the git checkout
# (destsuffix=git above); assigning it is an error in current oe-core.

inherit cargo pkgconfig

# build ONLY the spotify plugin, not the whole gst-plugins-rs workspace
CARGO_BUILD_FLAGS += "--package gst-plugin-spotify"

DEPENDS += "gstreamer1.0 gstreamer1.0-plugins-base"
RDEPENDS:${PN} += "gstreamer1.0"

# --- HOW TO COMPLETE THIS RECIPE (run once, needs the fetched source) ---
#   pip install cargo-bitbake        # or: cargo install cargo-bitbake
#   git clone https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs
#   cd gst-plugins-rs/audio/spotify && cargo bitbake
# Copy the generated SRC_URI crate list into files/crates.inc, set
# AISPEAKER_SPOTIFY_CARGO = "1" and a fixed SRCREV, then this builds offline.
#
# Prebuilt reference (Debian, not usable directly on Yocto but documents the
# element + versions): https://github.com/kingosticks/gst-plugins-rs-build
