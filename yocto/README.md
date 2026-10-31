# AISpeaker Linux — dedicated Yocto distribution

Replaces Raspberry Pi OS Lite on the speaker. Goal: a minimal, reproducible
system with a **read-only rootfs** (power-loss resilience), all mutable state
on a separate `/data` partition, and exactly the stack the application uses.

## Architecture

```
yocto/
├── kas/aispeaker.yml          # manifest: upstream layers + build configuration
├── meta-aispeaker/            # our layer (distro, image, recipes)
│   ├── conf/distro/aispeaker.conf
│   ├── files/wic/aispeaker.wks.in   # SD card partition layout
│   └── recipes-*/
├── kas.sh                     # kas-container wrapper (keeps clones+build under yocto/)
├── local/                     # SITE-SPECIFIC, git-ignored (keys, WiFi, NFS)
├── scripts/                   # SD card provisioning + in-place installer staging
├── build/                     # git-ignored: bitbake build dir (downloads, sstate, tmp)
└── bitbake/ openembedded-core/ meta-yocto/ meta-openembedded/ meta-raspberrypi/
                               # git-ignored: kas-cloned upstream
```

Upstream layers are **not vendored** — they are fetched by
[kas](https://kas.readthedocs.io) according to the manifest. Pin commits in
`kas/aispeaker.yml` after the first successful build.

Release: **wrynose** (Yocto 6.0, Python 3.14). Mopidy 4 / Mopidy-Spotify 5 need
Python ≥3.13, which rules out the older LTS (scarthgap ships 3.12).

**Upstream is no longer poky.** poky as a combined repository stopped being
published — its last commit on any branch is from 2026-02 and it never received
whinlatter or wrynose. The equivalent is assembled from four repositories, which
is what the manifest tracks:

| repo | branch | role |
|---|---|---|
| `bitbake` | 2.18 | the build tool (wrynose requires BB_MIN_VERSION 2.18.0); not a layer |
| `openembedded-core` | wrynose | layer `meta` |
| `meta-yocto` | wrynose | layer `meta-poky` — provides `conf/distro/poky.conf`, which our distro requires |
| `meta-openembedded` | wrynose | meta-oe, meta-python, meta-networking, meta-multimedia, meta-filesystems |
| `meta-raspberrypi` | wrynose | machine support |

Audio/udev configs are **symlinks into `linux/`** in this repo — `linux/`
stays the single source of truth; the layer only packages it.

## Site-specific configuration (`local/`, git-ignored)

Nothing personal (addresses, credentials, keys) is committed. Before building
/ provisioning, create:

| File | Content | Used by |
|---|---|---|
| `local/authorized_keys` | `cat ~/.ssh/id_*.pub > local/authorized_keys` | baked into the image (user `speaker`, key-only login) |
| `local/wifi.env` | `WIFI_SSID="..."` `WIFI_PSK="..."` | `provision-data.sh` (written to the SD card, not the image) |
| `local/nfs.env` (optional) | `NFS_EXPORT` + `NFS_MOUNTPOINT` + `NFS_OPTIONS` | `provision-data.sh` (generates the NFS mount/automount units) |

## SD card layout (files/wic/aispeaker.wks.in)

GPT, A/B for RAUC updates (since 2026-10-30; the speaker as migrated that day
still has the earlier boot/root/data MBR layout until its next install):

| Partition | Label | FS | Role |
|---|---|---|---|
| p1 32 MB | bootsel | vfat | `autoboot.txt` only — the firmware's A/B switch |
| p2 / p3 256 MB | bootA / bootB | vfat | firmware, kernel, dtbs, `cmdline.txt` of the slot |
| p4 / p5 3 GB | rootA / rootB | ext4, **read-only** | the OS of each slot |
| p6 8 GB | data | ext4 (rw, noatime) | everything mutable, shared by both slots |

The ext4 inside a root slot is only as big as the image (content + 30 % +
`IMAGE_ROOTFS_EXTRA_SPACE`, 512 MiB), and RAUC writes it as is, so free space in
`/` comes from that setting, not from the 3 GB partition.

No partition is in `/etc/fstab`: wic would write mount points into its own
copy of the rootfs only, and the bundle's rootfs would differ from slot A's.
`/data` is mounted by the overlayfs-etc preinit (by label).

The rest of the card is left unallocated by the image. Grow `/data` once after
installing, online and without unmounting. The image's backup GPT header sits
at the end of the image, not of the card, so parted first asks to move it
(`Fix`) and then needs a second run for the resize:

```bash
printf 'Fix\n' | parted ---pretend-input-tty /dev/mmcblk0 print
printf 'Yes\n100%%\n' | parted ---pretend-input-tty /dev/mmcblk0 resizepart 6
resize2fs /dev/mmcblk0p6
```

On `/data`:

- `/data/opt/ai-smart-speaker` — application + `.venv` (symlinked from `/opt/ai-smart-speaker`, so `sync-to-rpi.sh` keeps working unchanged); its `recordings/` holds the wake word clips and session recordings
- `/data/music`, `/data/mopidy` — music, Mopidy state
- `/data/mopidy/mopidy.conf` — site-specific Mopidy overrides (Spotify/YouTube credentials) — never in git
- `/data/cache` — pip / huggingface / Mopidy caches (`XDG_CACHE_HOME`)
- `/data/overlay-etc/` — writable `/etc` overlay (SSH host keys, NM WiFi profiles) — `overlayfs-etc` image feature
- `/data/var/lib/NetworkManager` — bind-mounted onto `/var/lib/...`
- `/data/var/log/journal` — bind-mounted onto `/var/volatile/log/journal`, i.e. `/var/log/journal` (persistent journal)
- `/data/state` — small service state owned by `speaker`: WirePlumber's (`XDG_STATE_HOME`), the log-shipping cursor
- `/data/rauc` — RAUC's status and the slot states of the tryboot backend; root only (`0700`), never under `/data/state`, which `speaker` can rename

System journal: **persistent on `/data`** (`Storage=persistent`, capped at
200 MB), bind-mounted from `/data/var/log/journal` before journald flushes. On
Raspbian this was `volatile`, which cost the entire history on every reboot —
here `/data` is written anyway, so the trade is worth it. See
`recipes-core/aispeaker-log-shipping/`.

## What's in the image

- **Audio**: PipeWire (+ `pipewire-pulse`) + WirePlumber with configs from
  `linux/` (fixed 48 kHz clock, quantum 480, XVF3800 always-on). Everything
  runs as **system services** under the unprivileged `speaker` user — PipeWire
  in system mode with its socket in `/run/pipewire`; RT scheduling comes from
  unit rlimits (`LimitRTPRIO`), so there is no rtkit/polkit on the image
- **Music: Mopidy** (replaces MPD) — see the dedicated section below
- **reSpeaker XVF3800**: it is plain USB Audio Class (`snd-usb-audio`, part of
  the RPi kernel) — no extra kernel module needed. The `respeaker-xvf3800`
  recipe fetches the prebuilt `xvf_host` tool from the Seeed GitHub repo
  (pinned `SRCREV`) and installs it with `xvf_init.sh` + `init_commands.txt`
  for diagnostics and manual (re)configuration only — there is no boot-time
  init, because the XVF3800 persists its configuration in internal flash
  after `SAVE_CONFIGURATION`. The udev rule stays: it grants the `speaker`
  user access to the USB device (for `xvf_host` and pyusb in the app)
- **Network**: NetworkManager, WiFi by default (profile written by
  `scripts/provision-data.sh`), **powersave disabled**, ethernet works when a
  cable is plugged in (DHCP); avahi (mDNS), sshd (key-only), NFS client
- **Python 3.14** (wrynose) + venv + pip, `libstdc++`/`libgomp` (required by
  manylinux wheels: onnxruntime, scipy, ctranslate2), portaudio, libsndfile
- **GStreamer** (base/good/bad/ugly/libav) + pygobject — Mopidy's playback stack
- **Appliance run model** (simpler than today's Raspbian user-session setup):
  one systemd instance starts pipewire → wireplumber → mopidy →
  `ai-smart-speaker.service`, all with `User=speaker` (UID 1000). No linger,
  no `user@` manager, plain `journalctl -u <unit>`. The app and Mopidy units
  are guarded by `ConditionPathExists` on their venvs — a fresh image doesn't
  fail-loop before deployment. See "Privileges" below for the root question.

**The application is deliberately NOT baked into the image** — it iterates
much faster than the OS. Deployment stays: `sync-to-rpi.sh` + `pip install -r
requirements.txt` in the venv on `/data`.

## Build

Host requirements: Docker + the `kas-container` wrapper
(`curl -L .../siemens/kas/<tag>/kas-container`); ~60 GB disk, first build 1–3 h.

Build through **`yocto/kas.sh`** — a thin wrapper that pins `KAS_WORK_DIR` to
`yocto/` so the upstream layer clones (openembedded-core, meta-openembedded,
meta-raspberrypi) and the build dir land under `yocto/` instead of cluttering
the repo root. The whole repo is still mounted in the container, so the
recipes' symlinks into `linux/` resolve.

```bash
cat ~/.ssh/id_*.pub > yocto/local/authorized_keys   # once
yocto/kas.sh build yocto/kas/aispeaker.yml           # → yocto/build/tmp/deploy/images/raspberrypi5/
```

Flash and provision the card:

```bash
cd yocto/build/tmp/deploy/images/raspberrypi5
sudo bmaptool copy aispeaker-image-raspberrypi5.rootfs.wic.bz2 /dev/sdX
sudo "$OLDPWD"/yocto/scripts/provision-data.sh /dev/sdX
```

First boot: `ssh speaker@aispeaker.local`, then from the repo
`application/sync-to-rpi.sh aispeaker` and in `/opt/ai-smart-speaker`:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
(openwakeword: `--no-deps`, as noted in requirements.txt; pip caches land in
`/data/cache` via `XDG_CACHE_HOME`). The openwakeword wheel ships without its
feature models, so fetch them once:
`.venv/bin/python -c 'import openwakeword.utils as u; u.download_models()'`
(the app needs `melspectrogram.onnx`, `embedding_model.onnx` and
`silero_vad.onnx`). Then, as root:
`systemctl start ai-smart-speaker` (a system unit with `User=speaker`, not
`systemctl --user`; it starts by itself on later boots once the venv exists).

SSH: `speaker` for deploys (the app's files must belong to it), `root` for
administration; there is no sudo and no password login. On dev the aliases are
`aispeaker` (speaker@) and `aispeaker-root` in `~/.ssh/config`.
`sync-to-rpi.sh` sends dev's `config.yml`, i.e. the production one with
`mqtt.enabled: true` — on a second, test Pi turn MQTT off after every sync, or
it takes over the speaker's broker session (same client id).

## Boot time

The image is built to boot fast: no initramfs, no apt machinery, no user
session, `NetworkManager-wait-online` masked (nothing blocks
on the network — the app reconnects on its own), `disable_splash=1` +
`boot_delay=0` in config.txt. What remains:

- RPi5 firmware/EEPROM: ~3–4 s (not reducible from the OS)
- kernel + systemd to `multi-user.target`: a few seconds — profile with
  `systemd-analyze blame` / `systemd-analyze critical-chain`
- the dominant chunk is the application itself: Python imports (onnxruntime)
  and model loading (openwakeword, faster-whisper) — that budget lives in the
  app repo (lazy loading, smaller models), not in the image

## Privileges — nothing runs as root

The whole runtime (PipeWire, WirePlumber, Mopidy, the application) runs as the
unprivileged **`speaker`** user via `User=` in the system units. Nothing in the
audio/app path needs root: the reSpeaker is reached over USB through the
`plugdev` group + the udev rule (`MODE=0666`), audio through the `audio` group.

This matters even for a home appliance: the app is **network-facing** (talks to
Gemini/OpenAI over the internet, subscribes to MQTT, parses network audio, runs
a wake-word model). Read-only rootfs already means a compromise can't rewrite
the system; running unprivileged means it can't touch anything outside `/data`
and the audio devices either. Defense in depth is essentially free here.

`root` exists only for administration (`systemctl`, `nmcli`) — there is no
`sudo` on the image. Root SSH is **key-only** (`PermitRootLogin
prohibit-password`, `PasswordAuthentication no`), which is the standard pattern
for a headless LAN appliance. If you'd rather not expose root over SSH at all,
add `sudo` to the image and a wheel entry for `speaker` instead.

## Music: Mopidy instead of MPD

Mopidy replaces MPD to open the door to **Spotify and YouTube Music** while
staying a drop-in for the application. `Mopidy-MPD` exposes the MPD protocol on
`127.0.0.1:6600`, and the app uses `clear/add/play/stop/status/setvol/
currentsong/playlistinfo/outputs/enableoutput/disableoutput/clearerror` — so
`python-mpd2` in the app is unchanged. Internet radio keeps working via
`Mopidy-Stream`.

**Three protocol gaps the app already handles** (the same code runs on both
servers):

- `clearerror` raises `MpdNotImplementedError` in mopidy-mpd. The app calls it
  before every `play` to make MPD's sticky error attributable to the current
  stream; on Mopidy the call is tolerated and ignored. Left unhandled it would
  abort `play_station` and the radio would never start.
- **`status` never contains `error`** in mopidy-mpd — the field is documented
  but never filled. Dead-stream detection therefore has a second signal:
  playback that stops on its own within `stream_start_grace` seconds of the
  start (config, default 15) and without the app asking for it counts as a
  failed stream. A stop after that window is treated as a person pressing stop.
  Whichever detector fires first wins; a failure is reported once per playback
  attempt.
- **Outputs are the mixer's mute switch.** mopidy-mpd reports a single fake
  output named `Mute` whose `outputenabled` is 1 when muted; `enableoutput`
  mutes and `disableoutput` unmutes. The app's "make sure every output is
  enabled" step special-cases that output and unmutes instead — taken
  literally it silenced every playback.
- **The queue does not survive a restart.** Resuming on an empty queue reloads
  the last station from `radio_state.json` instead of calling a bare `play`.

Audio path: Mopidy → GStreamer → `pulsesink` → `pipewire-pulse` → PipeWire →
XVF3800 (the same sink MPD used via PulseAudio today).

Mopidy is **baked into the image as recipes** (not a venv): `mopidy`,
`mopidy-mpd`, `mopidy-youtube` and `mopidy-spotify`, plus the pure-Python deps
that aren't in meta-python yet (`recipes-devtools/python/`), all pulled from
PyPI with pinned sha256. It runs as a system service (`User=speaker`), starts
on boot, and needs no deployment step. GStreamer reaches it through pygobject,
which is why the distro enables `gobject-introspection-data` (the GStreamer
typelibs).

**YouTube / YouTube Music work without a login**, but only with all of these:

- **Search through YouTube Music** (`musicapi_enabled = true`, ytmusicapi):
  ~1.5 s on the Pi. The default scraper took 9-16 s and blocks Mopidy's core
  meanwhile, so MPD `status` times out.
- **yt-dlp with a JS runtime.** YouTube serves audio only to clients that solve
  its JS challenges; without one even yt-dlp gets HTTP 403. The image ships
  `python3-yt-dlp`, `python3-yt-dlp-ejs` (the solver scripts, installed from
  the wheel because the sdist builds them with npm) and `deno-bin` (official
  binary, found in `PATH`). Bump `yt-dlp` and `yt-dlp-ejs` together — yt-dlp
  pins the ejs version — whenever YouTube breaks playback.
- **`allow_cache = true`.** A googlevideo URL handed to GStreamer gets 403 even
  when yt-dlp resolved it, because it is tied to the client that fetched it;
  worse, after one such failure Mopidy answered 403 for the radio too until it
  was restarted. With the cache, yt-dlp downloads the track to
  `/data/cache/mopidy/youtube` and Mopidy plays the file, so GStreamer never
  talks to YouTube. `systemd-tmpfiles` drops files older than 30 days.
- Two patches to mopidy-youtube 4.0.2 for this stack: `pkg_resources` is gone
  in setuptools 82, and Mopidy 4 dropped `ModelJSONEncoder` (cached track
  metadata).

To try a newer yt-dlp before rebuilding: install the wheels with
`pip install --target` under `/data/tmp`, put deno next to them, and point
Mopidy at both with a drop-in in `/run/systemd/system/mopidy.service.d/`
(`PYTHONPATH`, `PATH`); it disappears on reboot. Cookies (YouTube Premium) are
only needed for the account's own library: `musicapi_cookiefile` in
`/data/mopidy/mopidy.conf`.

**Spotify needs two more things** and is therefore installed but `enabled =
false` by default:

1. A **Spotify Premium** account.
2. `gst-plugins-spotify` — the librespot-based GStreamer element Mopidy-Spotify
   5.x plays through. It is a **Rust** plugin, not in any Yocto layer, and its
   crate manifest must be generated (it can't be hand-written). The scaffold
   recipe `recipes-multimedia/gstreamer/gstreamer1.0-plugins-spotify_*.bb`
   documents the one-time `cargo bitbake` step. Once built and added to the
   image, set `[spotify] enabled = true` and credentials in
   `/data/mopidy/mopidy.conf`.

So out of the box you get MPD-protocol + radio + YouTube Music; Spotify is one
`cargo bitbake` + rebuild away.

> Smoke-test after the swap: there is no `mpc` on the image; speak the MPD
> protocol with `printf 'status\nclose\n' | nc 127.0.0.1 6600` (`outputs`,
> `add <url>`, `play`), or tunnel the port and use `mpc` from another machine.

## System updates (RAUC, A/B)

The running system stays untouched while the other slot is written; the new
slot then boots once and only becomes the default after it proved healthy.

- **Switch:** the RPi5 firmware reads `autoboot.txt` on p1 — `[all]` names the
  boot partition of a normal boot, `[tryboot]` the one used once after
  `reboot '0 tryboot'`. RAUC drives it through a custom bootloader backend
  (`recipes-core/aispeaker-update/files/rpi-tryboot-backend`): installing
  points `[tryboot]` at the new slot, `rauc status mark-good` on the running
  slot points both `[all]` and `[tryboot]` at it — so a stray tryboot later
  boots the same slot, never an old or half-written one. Its state lives in
  `/data/rauc` (root only).
- **Boot files ride in the rootfs** (`/usr/lib/aispeaker/boot`); RAUC's
  post-install handler formats the new slot's boot partition, copies them and
  sets `root=` / `rauc.slot=` in its `cmdline.txt`. The bundle holds only the
  rootfs, so kernel and rootfs always come from the same build.
- **Confirmation:** `aispeaker-health-check.service` marks the slot good once
  the network is up, `sshd.socket` listens and `ai-smart-speaker` (when
  deployed) has been active for 60 s; a failed `mark-good` (bootsel not
  writable) is retried. On a trial boot that does not get there within 10 min
  it reboots — back to the previous slot.
- **Rollback without touching the speaker:** a kernel that cannot mount its
  root panics and reboots (`panic=10`); a hang is reset by the watchdog
  (systemd `RuntimeWatchdogSec=30s`); an unhealthy system by the health check.
  Each reboot before the confirmation lands on the previous slot.

Build, publish, install:

```bash
yocto/kas.sh build yocto/kas/aispeaker.yml
yocto/kas.sh build --target aispeaker-bundle yocto/kas/aispeaker.yml
cp yocto/build/tmp/deploy/images/raspberrypi5/aispeaker-bundle-raspberrypi5.raucb /mnt/qnap/aispeaker/updates/
ssh aispeaker-root aispeaker-update /mnt/nas/updates/aispeaker-bundle-raspberrypi5.raucb
ssh aispeaker-root rauc status        # after the reboot: booted and activated slot
```

Verified on the spare Pi (2026-10-30): A→B update (70 s install, ~35 s to the
confirmed new slot), a slot that cannot mount its root (back on the old slot
after 37 s), a slot whose app does not come up (back after the trial window),
and a plain reboot staying on the confirmed slot. The serial console showed
the broken-root case in full: the firmware in `TRYBOOT_A_B_MODE` picked boot
partition 2, the kernel panicked at 0.64 s ("Unable to mount root fs",
`root=/dev/mmcblk0p9`), rebooted after 10 s, and the firmware took partition 3.
`rootwait=30` only waits while the card itself is missing; a partition that
does not exist fails at once.

Signing: `yocto/local/rauc/` (git-ignored) holds the CA and the signing key and
certificate; the image carries the CA certificate (`rauc-conf`). **Without
these files no update can be issued** — keep a copy outside this machine. The
signing certificate must not carry `extendedKeyUsage=codeSigning`: RAUC checks
the S/MIME purpose by default and rejects it ("unsuitable certificate purpose").

`/data` and the `/etc` overlay are shared by both slots: a change in an image's
`/etc` is hidden by a file of the same name left in the overlay.

## In-place install — no card removal, no case opening

How the speaker went from Raspberry Pi OS to Yocto on 2026-10-30, and how a new
image gets onto it today. A small installer boots once via the RPi5 **tryboot**
flag, runs from RAM and rewrites the SD card from a USB stick. Tryboot is
one-shot: any failure before the write, or a power cut before it, brings the
current system back.

**The whole card is rewritten, `/data` included** — afterwards redeploy the app
(Build section). On the A/B layout use RAUC instead (above); staging this
installer there does not work yet — tryboot boots the `[tryboot]` slot of
`autoboot.txt`, not the `os_prefix` directory the script sets up.

1. Build the image and the installer, then bake the site configuration (WiFi,
   NFS) into a copy of the image. The speaker has no screen, no serial console
   and no password, so without a WiFi profile on first boot it is unreachable:

   ```bash
   yocto/kas.sh build yocto/kas/aispeaker.yml
   yocto/kas.sh build --target aispeaker-installer-initramfs yocto/kas/aispeaker.yml
   sudo yocto/scripts/make-provisioned-image.sh   # → …/aispeaker-image-raspberrypi5.provisioned.wic.bz2
   ```

2. Put **only** `*.provisioned.wic.bz2` on a FAT32 (or ext4) stick — the
   installer takes the first `aispeaker-image*.wic.bz2` it finds. The file holds
   the WiFi PSK; wipe the stick afterwards.
3. Stage the installer on the running system (Raspberry Pi OS: boot partition
   `/boot/firmware`, with sudo; Yocto: `/boot`, read-only — `mount -o remount,rw
   /boot` first, as root):

   ```bash
   D=yocto/build/tmp/deploy/images/raspberrypi5
   scp $D/Image $D/bcm2712-rpi-5-b.dtb $D/bcm2712-rpi-cm5*.dtb \
       $D/aispeaker-installer-initramfs-raspberrypi5.cpio.gz \
       yocto/scripts/stage-inplace-install.sh <host>:/tmp/inst/
   ssh <host> 'sudo bash /tmp/inst/stage-inplace-install.sh /tmp/inst'          # Raspberry Pi OS
   ssh aispeaker-root 'bash /tmp/inst/stage-inplace-install.sh /tmp/inst /boot' # Yocto
   ```

   Kernel, initramfs, dtbs, overlays and a copy of `cmdline.txt` go into
   `aispeaker-installer/` on the boot partition, plus a `tryboot.txt` selecting
   it via `os_prefix`. Nothing the current system boots from is changed; a
   plain reboot still boots it.
4. Plug the stick in and `reboot '0 tryboot'` (`systemctl reboot
   --reboot-argument='0 tryboot'`).

What the installer does: waits 10 s, copies the image (~180 MB) into RAM — up
to three attempts — decompresses it once as an integrity test, writes the card
from RAM, then drops the page cache and compares the card with the image
before it reports success and reboots (a writeback error reaches neither
bunzip2 nor busybox `sync`). Without the read-back an install took 10.5–12 min
on the speaker and 15–18 min on the spare Pi's slower card; the read-back adds
about one read of the card. The log goes to
`aispeaker-installer.log` on the boot partition: the old one until the write
starts, the new one afterwards (`/boot/aispeaker-installer.log` on Yocto).

Behaviour that shaped it, found on the spare Pi:

- busybox in the initramfs: `mount` needs `-t`; `dd` has no `conv=`; there is
  no `/tmp` until the init creates it; `od`/`blockdev` are minimal or missing.
- `/dev/console` is the last `console=` of the copied cmdline (on Raspberry Pi
  OS `tty1`, with no framebuffer here) — hence logging through `/dev/kmsg`.
- the cheap test stick dropped off the bus ("device offline") 6–7 s after boot
  whenever it was busy then; hence the wait, the RAM copy and a read-only mount.
- the Pi 5 fan runs flat out without its driver, so it says nothing about the
  installer working.
- the write-progress lines (`/proc/<pid>/io`) never show up in the log, although
  the same code works as a script — not yet understood.

## Logs (`aispeaker-log-shipping`)

Two layers, deliberately separate from the application:

1. **Persistent journal on `/data`** — nothing is lost on reboot, not even the
   last minutes before a crash.
2. **`ship-speaker-logs.timer`** (every 5 min) copies new entries to the NFS
   share, one file per day (`ai-speaker-RRRR-MM-DD.log`), retention 90 days.
   The cursor lives on `/data`, not `/run`: with a persistent journal a cursor
   lost on reboot would re-ship the whole history as duplicates. The cursor
   only advances after a successful write, so a NAS outage costs nothing.

Configure via `/etc/default/aispeaker-log-shipping` (`LOG_DEST`, `LOG_UNITS`,
`LOG_RETENTION_DAYS`). `LOG_DEST` must sit **under the NFS mount point** from
`local/nfs.env` — writing over NFS means the destination has to be inside the
share the speaker mounts (on Raspbian the logs went to a separate QNAP volume
over ssh with a forced command; that machinery is gone).

**Why not log to NFS from inside the application:** a `FileHandler` writing to
the share would block the audio event loop whenever the NAS stalls (up to the
`soft` mount timeout), and it would miss exactly what matters most — tracebacks
on a hard failure, OOM kills and systemd's own restart messages. The app keeps
writing to stdout; where that ends up is an infrastructure decision.

## NFS share (logs; recordings stay local)

`/data` is the writable backbone (/etc overlay, venvs, NM state, caches, Mopidy
state, recordings); the NFS share only receives the log copies. It can't
replace `/data`: the network isn't up in early boot, and a read-only rootfs needs
a local writable partition to come up at all.

1. Export a share on the NAS (NFSv4) **for the speaker's IP** — QNAP grants
   access per host; a missing host shows up as "No such file or directory" from
   the NFSv4 pseudo-root, not as "access denied".
2. Set in `local/nfs.env`: `NFS_EXPORT` (server:export), `NFS_MOUNTPOINT`
   (default `/mnt/nas`; it must exist in the image), `NFS_OPTIONS`.
   `provision-data.sh` then **generates** the `<mountpoint>.mount` + `.automount`
   units into the `/etc` overlay and enables the automount. Nothing NFS is
   baked into the image.
   > QNAP with NFSv4 uses a pseudo-root, so the export path is `/<share>`
   > (e.g. `192.168.1.15:/aispeaker`), not the `/share/...` v3 path.

`soft,timeo=50` + automount = an absent NAS never hangs boot.

Recordings stay on `/data` (decided 2026-10-30): about 1 MB per wake-up, tens of
MB a day, a negligible share of the card's write endurance. Writing them to NFS
directly is not safe — the app writes session recordings on its event loop, and
a soft mount can stall a call for ~15 s when the NAS is down. Copy them to the
NAS by hand when they are needed for training.

## Roadmap

- **Phase 1 — done.** RO system image, app deployed to `/data` via rsync+venv;
  the speaker runs it since 2026-10-30 (boot to wake word 10.5 s, Raspberry Pi
  OS: 19.3 s).
- **Phase 2**: optionally bake the app + its Python deps into the image
  (wheel-based recipes). Log shipping is done; recordings stay local (above).
- **Phase 3**: A/B updates with RAUC (above) — built and verified on the spare
  Pi on 2026-10-30. The speaker gets the A/B layout with its next full install
  (the in-place install above, which wipes `/data` one last time); from then on
  system updates keep `/data`.

## Build status & known iteration points

**Status on wrynose: builds green (2026-08-28).** `bitbake aispeaker-image` →
**181 MB** `aispeaker-image-*.wic.bz2`, 518 MB rootfs, 2641 packages, 8904
tasks. The manifest confirms mopidy 4.0.1 + mopidy-mpd/spotify/youtube, the
Python deps, pipewire, wireplumber, respeaker-xvf3800, networkmanager-wifi and
gstreamer1.0-libav.

For comparison the first wrynose image, before the trimming below, was 202 MB /
592 MB / 2669 packages / 10987 tasks; the last walnascar one was ~177 MB.

What the trimming removed (see `conf/distro/aispeaker.conf`):

- **Mesa userspace and Vulkan** (~30 MB) — nothing renders on a headless box.
  `vc4graphics` stays in MACHINE_FEATURES so an HDMI console remains available.
- **ICU** (32 MB) — arrived through libsoup → libpsl5 → libicudata; a bbappend
  switches libpsl to the libidn2 backend (now 133 KB, souphttpsrc unaffected).
- **SBOM generation** — wrynose moved `create-spdx` into `INHERIT_DISTRO`, so it
  is on by default; it produced a 32 MB spdx.json and 16% of all build tasks.
- **bluetooth** — removed from both DISTRO_FEATURES and MACHINE_FEATURES.
- **Services with nothing to do** (2026-10-30): `3g`/`nfc` from poky's default
  DISTRO_FEATURES (ofono, neard), NetworkManager's `dnsmasq`, systemd's userdb
  (userdbd + worker pool, ~28 MB), busybox syslog/klogd (duplicate the
  journal); `rpcbind` stays installed for nfs-utils-client but disabled (NFSv4).
  The speaker then runs 18 services, as many as on Raspberry Pi OS.
- **Swap:** 512 MB zram (zstd) instead of none — the speaker has 1 GB; swap on
  the SD card would wear it.

Python is the largest remaining item at 171 MB (yt_dlp 25 MB, chardet 24 MB,
pip 12 MB). Another ~40 MB is reachable — chardet comes in via python3-requests,
and the rich/docutils/pygments chain via Mopidy's CLI — but both need verifying
by running Mopidy, not by a build.

Moving walnascar → wrynose needed exactly two changes in this layer:

- **`wic/` → `files/wic/`.** Newer oe-core refuses wks files outside `files/wic`
  ("wic/wks files at … need to be moved to files/wic within the layer to be
  found/used") — caught by the sanity checker, not at image time.
- **setuptools workarounds dropped.** wrynose ships setuptools 82, which
  understands PEP 639 natively, so the `do_configure` sed that rewrote
  `license = "SPDX"` into `{text = …}` and dropped `license-files` is gone from
  mopidy*, rich-rst and ytmusicapi. The `setuptools>=78` pin is now satisfied
  for real instead of being relaxed.

Earlier fixes that still apply (a Mopidy bump may re-surface the
modern-Python-packaging ones):

- **Missing build-system deps:** `python3-setuptools-scm-native` (mopidy*,
  ytmusicapi) + `SETUPTOOLS_SCM_PRETEND_VERSION=${PV}`, `python3-hatch-vcs-native`
  (cyclopts).
- **Package names:** `networkmanager-nmtui` does not exist (removed upstream);
  WiFi needs the split **`networkmanager-wifi`** package — otherwise WiFi
  silently doesn't work. pipewire's pulse server is the `pipewire-pulse`
  package, not a PACKAGECONFIG.
- **Licenses/providers:** `LICENSE_FLAGS_ACCEPTED += "commercial"` (plugins-ugly),
  `PREFERRED_PROVIDER_ffmpeg = "ffmpeg"` (meta-raspberrypi ships a second one).
- **PyPI recipes** use the underscore `PYPI_PACKAGE` form — the pypi class builds
  the archive name from it and ignores `PYPI_ARCHIVE_NAME`.

Verified on the hardware (2026-10-24 … 10-30): boot without failed units,
`/etc` overlay, journal and NetworkManager state on `/data`, NFS, the in-place
installer (Raspberry Pi OS → Yocto and Yocto → Yocto), the app with the
reSpeaker and MQTT, radio play/stop and resume through Mopidy's MPD protocol.
What only showed up on the hardware is in the commit messages of that week:
read-only rootfs ordering, the app unit's groups, the reSpeaker output levels,
the non-recursive chown, the trimmed services, zram.

Still open:

- Spotify: `gstreamer1.0-plugins-spotify` is a scaffold — generate its crate
  manifest with `cargo bitbake` before enabling (see the Mopidy section).
- a reSpeaker plugged in after boot gets only its sink in PipeWire, no source;
  a reboot fixes it.
- no `systemd-analyze` in the image (boot time: `journalctl -b | grep "Startup finished"`).
- for servicing, the rootfs can be remounted rw: `mount -o remount,rw /`

## Why a subdirectory, not a separate repo

The layer packages files from `linux/` (symlinks) and must evolve together
with the application (Python deps ↔ image packages). A separate repo becomes
worthwhile only once CI builds the distro or other projects consume it.
