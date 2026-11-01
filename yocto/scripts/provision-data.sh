#!/bin/bash
# After flashing the image (bmaptool/dd) — seeds the 'data' partition with
# site-specific configuration that is deliberately kept out of git:
#  - NetworkManager WiFi profile (keyfile in the /etc overlay)
#  - optional NFS shares: logs/updates and the music collection (generated mount+automount units)
#  - application directory skeleton
#
# Site config lives in the git-ignored yocto/local/ directory:
#   local/wifi.env:  WIFI_SSID="..."  WIFI_PSK="..."
#   local/nfs.env:   NFS_EXPORT + NFS_MOUNTPOINT + NFS_OPTIONS   (optional),
#                    MUSIC_NFS_EXPORT [+ MUSIC_NFS_OPTIONS] -> /mnt/music (optional)
#
# Usage: sudo ./provision-data.sh /dev/sdX
#        sudo ./provision-data.sh /dev/sdX "SSID" "psk"   (overrides wifi.env)
set -euo pipefail

DEV="${1:?usage: $0 /dev/sdX [SSID PSK]}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)/local"

WIFI_SSID="${2:-}"
WIFI_PSK="${3:-}"
if [ -z "$WIFI_SSID" ] && [ -f "$LOCAL_DIR/wifi.env" ]; then
    # shellcheck source=/dev/null
    . "$LOCAL_DIR/wifi.env"
fi
[ -n "${WIFI_SSID:-}" ] && [ -n "${WIFI_PSK:-}" ] || {
    echo "No WiFi credentials: pass SSID/PSK as arguments or create $LOCAL_DIR/wifi.env"
    exit 1
}

DATA_PART="$(lsblk -rno NAME,LABEL "$DEV" | awk '$2=="data"{print "/dev/"$1}')"
[ -n "$DATA_PART" ] || { echo "No partition labelled 'data' found on $DEV"; exit 1; }

MNT="$(mktemp -d)"
mount "$DATA_PART" "$MNT"
trap 'umount "$MNT" && rmdir "$MNT"' EXIT

# overlayfs-etc: the upperdir of the /etc overlay lives on the data partition
UPPER="$MNT/overlay-etc/upper"

NM_DIR="$UPPER/NetworkManager/system-connections"
mkdir -p "$NM_DIR"
cat > "$NM_DIR/home-wifi.nmconnection" <<EOF
[connection]
id=home-wifi
type=wifi
autoconnect=true

[wifi]
ssid=$WIFI_SSID
mode=infrastructure

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PSK

[ipv4]
method=auto

[ipv6]
method=auto
EOF
chmod 600 "$NM_DIR/home-wifi.nmconnection"
echo "WiFi profile '$WIFI_SSID' written"

# optional NFS shares — generate the mount + automount units into the /etc
# overlay. The unit filename must match the mount point, so it is derived via
# systemd-escape. The mount point must exist in the image (read-only rootfs):
# /mnt/nas and /mnt/music do.
nfs_share() {  # export mountpoint options
    local export="$1" mp="$2" opts="$3" base sysd
    base=$(systemd-escape -p "$mp")           # /mnt/nas -> mnt-nas
    sysd="$UPPER/systemd/system"
    mkdir -p "$sysd/multi-user.target.wants"

    cat > "$sysd/${base}.mount" <<EOF
[Unit]
Description=NFS share ${export} (site-specific, from nfs.env)

[Mount]
What=${export}
Where=${mp}
Type=nfs
Options=${opts}

[Install]
WantedBy=multi-user.target
EOF

    cat > "$sysd/${base}.automount" <<EOF
[Unit]
Description=Automount for ${mp}

[Automount]
Where=${mp}
TimeoutIdleSec=300

[Install]
WantedBy=multi-user.target
EOF

    # enable the automount (lazy mount on first access — keeps boot NAS-free)
    ln -sf "/etc/systemd/system/${base}.automount" \
        "$sysd/multi-user.target.wants/${base}.automount"
    echo "NFS share configured: ${export} -> ${mp}  (unit ${base}.mount)"
}

if [ -f "$LOCAL_DIR/nfs.env" ]; then
    # shellcheck source=/dev/null
    . "$LOCAL_DIR/nfs.env"
    DEFAULT_OPTS="vers=4,soft,timeo=50,retrans=2,noatime,_netdev"
    if [ -n "${NFS_EXPORT:-}" ]; then
        nfs_share "$NFS_EXPORT" "${NFS_MOUNTPOINT:-/mnt/nas}" "${NFS_OPTIONS:-$DEFAULT_OPTS}"
    fi
    if [ -n "${MUSIC_NFS_EXPORT:-}" ]; then
        nfs_share "$MUSIC_NFS_EXPORT" /mnt/music "${MUSIC_NFS_OPTIONS:-ro,$DEFAULT_OPTS}"
    fi
fi

# /data skeleton (also created at boot by aispeaker-data-setup; UID 1000 = speaker)
mkdir -p "$MNT/opt/ai-smart-speaker" "$MNT/recordings" "$MNT/music" "$MNT/mpd/playlists"
chown -R 1000:1000 "$MNT/opt" "$MNT/recordings" "$MNT/music" "$MNT/mpd"

echo "OK: data partition $DATA_PART provisioned"
