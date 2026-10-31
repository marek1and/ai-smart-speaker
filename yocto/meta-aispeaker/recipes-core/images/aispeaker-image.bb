SUMMARY = "AI Smart Speaker appliance image (RPi5 + reSpeaker XVF3800)"
LICENSE = "MIT"

inherit core-image

# read-only rootfs + writable /etc as an overlay on the data partition
IMAGE_FEATURES += "ssh-server-openssh read-only-rootfs overlayfs-etc"

OVERLAYFS_ETC_MOUNT_POINT = "/data"
OVERLAYFS_ETC_DEVICE = "LABEL=data"
OVERLAYFS_ETC_FSTYPE = "ext4"
OVERLAYFS_ETC_MOUNT_OPTIONS = "defaults,noatime"
OVERLAYFS_ETC_USE_ORIG_INIT_NAME = "1"

# ext4: the rootfs image that goes into the RAUC bundle (aispeaker-bundle)
IMAGE_FSTYPES = "wic.bz2 wic.bmap ext4"
# The A/B partitions are 3G but the ext4 is sized to its content (+30%), and
# RAUC writes it as is; keep 512 MiB of headroom so new packages fit (deno
# alone is 82 MB, the rootfs had 97 MB free).
IMAGE_ROOTFS_EXTRA_SPACE = "524288"
WKS_FILE = "aispeaker.wks.in"
do_image_wic[depends] += "aispeaker-bootsel:do_deploy"

CORE_IMAGE_EXTRA_INSTALL = " \
    kernel-modules \
    \
    networkmanager networkmanager-nmcli networkmanager-wifi \
    wpa-supplicant iw \
    avahi-daemon \
    openssh-sftp-server rsync \
    nfs-utils-client \
    \
    pipewire pipewire-pulse pipewire-alsa pipewire-tools \
    wireplumber \
    alsa-utils \
    \
    python3 python3-modules python3-venv python3-pip \
    libstdc++ libgomp \
    portaudio-v19 libsndfile1 \
    \
    gstreamer1.0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    python3-pygobject \
    \
    respeaker-xvf3800 \
    aispeaker-audio-config \
    aispeaker-mopidy \
    aispeaker-system-config \
    aispeaker-log-shipping \
    aispeaker-app-runtime \
    aispeaker-users \
    aispeaker-wifi \
    aispeaker-update \
    \
    tzdata ca-certificates curl \
    usbutils htop e2fsprogs-resize2fs parted \
    i2c-tools \
"

# debug tools only when needed:
# EXTRA_IMAGE_FEATURES += "tools-debug"

# The boot partition files ride along in the rootfs, so an update brings the
# kernel, dtbs and firmware that match it: RAUC's post-install handler
# (aispeaker-update) copies them onto the boot partition paired with the new
# slot. Same selection and naming as the wic bootimg-partition plugin.
ROOTFS_POSTPROCESS_COMMAND += "aispeaker_copy_boot_files"
do_rootfs[depends] += " \
    virtual/kernel:do_deploy \
    rpi-bootfiles:do_deploy \
    rpi-config:do_deploy \
    rpi-cmdline:do_deploy \
"

python aispeaker_copy_boot_files() {
    import glob, os, shutil
    deploy = d.getVar('DEPLOY_DIR_IMAGE')
    dest = os.path.join(d.getVar('IMAGE_ROOTFS'), 'usr/lib/aispeaker/boot')
    bb.utils.mkdirhier(dest)
    for entry in d.getVar('IMAGE_BOOT_FILES').split():
        src, _, dst = entry.partition(';')
        matches = sorted(glob.glob(os.path.join(deploy, src)))
        if not matches:
            bb.fatal("IMAGE_BOOT_FILES entry %s matches nothing in %s" % (entry, deploy))
        for m in matches:
            if not dst:
                target = os.path.join(dest, os.path.basename(m))
            elif dst.endswith('/'):
                target = os.path.join(dest, dst, os.path.basename(m))
            else:
                target = os.path.join(dest, dst)
            bb.utils.mkdirhier(os.path.dirname(target))
            if os.path.isdir(m):
                shutil.copytree(m, target, dirs_exist_ok=True)
            else:
                shutil.copy2(m, target)
}

# replaced by aispeaker-health-check, which confirms a slot only once the
# speaker works, not as soon as it boots
BAD_RECOMMENDATIONS += "rauc-mark-good"
