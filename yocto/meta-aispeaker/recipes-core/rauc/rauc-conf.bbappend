# The speaker's slot layout and the update CA. ca.cert.pem is a symlink into
# the git-ignored yocto/local/rauc/, next to the signing key.
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
