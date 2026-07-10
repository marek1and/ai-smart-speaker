SUMMARY = "AI Smart Speaker appliance image (RPi5 + reSpeaker XVF3800)"
LICENSE = "MIT"

inherit core-image

# read-only rootfs + writable /etc as an overlay on the data partition
IMAGE_FEATURES += "ssh-server-openssh read-only-rootfs overlayfs-etc"

OVERLAYFS_ETC_MOUNT_POINT = "/data"
OVERLAYFS_ETC_DEVICE = "LABEL=data"
OVERLAYFS_ETC_FSTYPE = "ext4"
OVERLAYFS_ETC_USE_ORIG_INIT_NAME = "1"

IMAGE_FSTYPES = "wic.bz2 wic.bmap"
WKS_FILE = "aispeaker.wks.in"

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
    aispeaker-app-runtime \
    aispeaker-users \
    aispeaker-wifi \
    \
    tzdata ca-certificates curl \
    usbutils htop e2fsprogs-resize2fs parted \
    i2c-tools \
"

# debug tools only when needed:
# EXTRA_IMAGE_FEATURES += "tools-debug"
