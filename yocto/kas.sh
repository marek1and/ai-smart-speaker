#!/bin/sh
# kas-container wrapper that keeps everything out of the repo root.
#
# By default kas-container uses the current directory as KAS_WORK_DIR, so it
# clones the upstream layers (poky, meta-openembedded, meta-raspberrypi) and
# creates the build dir right next to the project. This wrapper pins
# KAS_WORK_DIR to yocto/ instead. The whole repo is still bind-mounted into the
# container as /repo (kas mounts the git top-level), so the recipes' symlinks
# into linux/ still resolve — only the clones + build dir move under yocto/.
#
# Usage (from anywhere in the repo):
#   yocto/kas.sh build  yocto/kas/aispeaker.yml
#   yocto/kas.sh shell  yocto/kas/aispeaker.yml -c 'bitbake -p'
#   yocto/kas.sh build --target aispeaker-installer-initramfs yocto/kas/aispeaker.yml
set -e
REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$REPO_ROOT"
exec env KAS_WORK_DIR="$REPO_ROOT/yocto" kas-container "$@"
