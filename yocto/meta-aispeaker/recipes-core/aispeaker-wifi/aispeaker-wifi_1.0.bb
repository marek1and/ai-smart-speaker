SUMMARY = "Bake the home WiFi connection into the image (site-specific)"
DESCRIPTION = "Generates a NetworkManager keyfile connection from the git-ignored \
yocto/local/wifi.env, so a freshly flashed card connects to WiFi on first boot \
without running provision-data.sh. The credentials live only in local/ (never \
in git) but DO end up in the image — keep the .wic private."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# files/wifi.env is a symlink to yocto/local/wifi.env (git-ignored)
SRC_URI = "file://wifi.env"

S = "${UNPACKDIR}"

do_install() {
    # shellcheck disable=SC1091
    . ${S}/wifi.env

    if [ -z "${WIFI_SSID}" ] || [ -z "${WIFI_PSK}" ]; then
        bbfatal "yocto/local/wifi.env must set WIFI_SSID and WIFI_PSK"
    fi
    case "${WIFI_PSK}" in
        *PUT-YOUR-WIFI-PASSWORD*)
            bbfatal "Set the real WIFI_PSK in yocto/local/wifi.env before building" ;;
    esac

    install -d ${D}${sysconfdir}/NetworkManager/system-connections
    conn=${D}${sysconfdir}/NetworkManager/system-connections/home-wifi.nmconnection
    cat > "$conn" <<EOF
[connection]
id=home-wifi
type=wifi
autoconnect=true

[wifi]
ssid=${WIFI_SSID}
mode=infrastructure

[wifi-security]
key-mgmt=wpa-psk
psk=${WIFI_PSK}

[ipv4]
method=auto

[ipv6]
method=auto
EOF
    # NetworkManager refuses keyfiles that are group/world readable
    chmod 0600 "$conn"
}

FILES:${PN} = "${sysconfdir}/NetworkManager/system-connections"

# secret in the package data — do not warn about it, and don't let it be world
# readable in the ipk QA
INSANE_SKIP:${PN} += "host-user-contaminated"

RDEPENDS:${PN} = "networkmanager networkmanager-wifi"
