SUMMARY = "RAUC update bundle for the speaker"
DESCRIPTION = "The aispeaker-image rootfs as a signed verity bundle. It carries \
the boot partition files too (/usr/lib/aispeaker/boot); the post-install \
handler puts them on the boot partition of the slot it installs to. Install on \
the speaker with aispeaker-update."
LICENSE = "MIT"

inherit bundle

RAUC_BUNDLE_COMPATIBLE = "aispeaker-rpi5"
RAUC_BUNDLE_FORMAT = "verity"
RAUC_BUNDLE_DESCRIPTION = "AISpeaker Linux"
RAUC_BUNDLE_SLOTS = "rootfs"
RAUC_SLOT_rootfs = "aispeaker-image"
RAUC_SLOT_rootfs[fstype] = "ext4"

# git-ignored, next to the CA whose certificate is in the image (rauc-conf)
RAUC_KEY_FILE = "${FILE_DIRNAME}/../../../local/rauc/dev.key.pem"
RAUC_CERT_FILE = "${FILE_DIRNAME}/../../../local/rauc/dev.cert.pem"
