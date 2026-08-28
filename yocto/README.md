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

| Partition | Label | FS | Mount | Role |
|---|---|---|---|---|
| p1 256 MB | boot | vfat | `/boot` (ro) | RPi firmware + kernel |
| p2 3 GB | root | ext4 | `/` **read-only** | entire OS |
| p3 8 GB | data | ext4 | `/data` (rw, noatime) | everything mutable |

On `/data`:

- `/data/opt/ai-smart-speaker` — application + `.venv` (symlinked from `/opt/ai-smart-speaker`, so `sync-to-rpi.sh` keeps working unchanged)
- `/data/recordings`, `/data/music`, `/data/mopidy` — recordings, music, Mopidy state
- `/data/mopidy/mopidy.conf` — site-specific Mopidy overrides (Spotify/YouTube credentials) — never in git
- `/data/cache` — pip / huggingface / Mopidy caches (`XDG_CACHE_HOME`)
- `/data/overlay-etc/` — writable `/etc` overlay (SSH host keys, NM WiFi profiles) — `overlayfs-etc` image feature
- `/data/var/lib/{NetworkManager,bluetooth}` — bind-mounted onto `/var/lib/...`

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
`/data/cache` via `XDG_CACHE_HOME`). Then, as root:
`systemctl start ai-smart-speaker`.

## Boot time

The image is built to boot fast: no initramfs, no apt machinery, no user
session, journald in RAM, `NetworkManager-wait-online` masked (nothing blocks
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
`127.0.0.1:6600`, and the app uses `clear/add/play/status/setvol/stop/
outputs/enableoutput/currentsong` — all covered — so `python-mpd2` in the app
is unchanged. Internet radio keeps working via `Mopidy-Stream`.

**Two protocol gaps the app already handles** (fixed before the migration, so
the same code runs on both servers):

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

Audio path: Mopidy → GStreamer → `pulsesink` → `pipewire-pulse` → PipeWire →
XVF3800 (the same sink MPD used via PulseAudio today).

Mopidy is **baked into the image as recipes** (not a venv): `mopidy`,
`mopidy-mpd`, `mopidy-youtube` and `mopidy-spotify`, plus the pure-Python deps
that aren't in meta-python yet (`recipes-devtools/python/`), all pulled from
PyPI with pinned sha256. It runs as a system service (`User=speaker`), starts
on boot, and needs no deployment step. GStreamer reaches it through pygobject,
which is why the distro enables `gobject-introspection-data` (the GStreamer
typelibs).

**YouTube / YouTube Music work out of the box** (via `yt-dlp` + `ytmusicapi`,
both baked in). For the authenticated YouTube Music API add headers/keys in
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

So out of the box you get MPD-protocol + radio + YouTube (Music); Spotify is one
`cargo bitbake` + rebuild away.

> Smoke-test after the swap: confirm `mpc -h 127.0.0.1 status`, adding a radio
> stream URL, and `enableoutput` all behave — Mopidy-MPD covers the protocol
> but a couple of commands map onto Mopidy's single output.

## In-place install — no card removal, no case opening

You will test on a spare Pi first (flash normally, as above). To then move the
**speaker** to Yocto without opening it, use the RPi5-native **tryboot** flow: a
tiny installer runs from RAM and rewrites the internal SD card from a USB stick.

Because the installer runs entirely from RAM, it can overwrite the whole card;
because the image source is the USB stick (not the card being written), there's
no self-overwrite; and because tryboot is **one-shot**, a power-cut mid-flash
just boots the old system again.

Build both the image and the installer initramfs:

```bash
yocto/kas.sh build yocto/kas/aispeaker.yml
yocto/kas.sh build --target aispeaker-installer-initramfs yocto/kas/aispeaker.yml
```

1. Copy `aispeaker-image-*.wic.bz2` onto a FAT/ext4 **USB stick**.
2. Copy the installer artifacts to the running target and stage them:

   ```bash
   scp yocto/build/tmp/deploy/images/raspberrypi5/{Image,*.dtb,\
       aispeaker-installer-initramfs*.cpio.gz} pi@aispeaker:/tmp/inst/
   ssh pi@aispeaker 'sudo /path/to/stage-inplace-install.sh /tmp/inst'
   ```

3. Plug the USB stick into the speaker, then `ssh pi@aispeaker 'sudo reboot
   "0 tryboot"'`. The installer flashes the card and reboots into Yocto.
4. Provision `/data` on the now-running speaker (WiFi profile, dirs) with
   `provision-data.sh` targeting the live card, or seed it over SSH.

**Validate this whole flow on the spare Pi first** (put Raspbian on it, then run
the in-place upgrade to Yocto): the installer initramfs and tryboot staging are
prepared here but unverified until a real build exists. A serial console
(`enable_uart=1`) makes the one-shot boot observable.

Once on Yocto with the A/B layout of Phase 3, updates no longer need this dance
— you flash the spare slot and switch. This tryboot installer is specifically
the **one-time Raspbian → Yocto** migration tool.

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

## Recordings/logs on NFS (optional)

`/data` stays mandatory regardless — it is the writable backbone (/etc overlay,
venvs, NM/BT state, caches, Mopidy state). NFS only **offloads** bulky/shareable
data (recordings) to the NAS; it can't replace `/data` (network isn't up in
early boot, and a RO rootfs needs a local writable partition to come up at all).

1. Export a share on the NAS (NFSv4) for the speaker's IP.
2. Set in `local/nfs.env`: `NFS_EXPORT` (server:export), `NFS_MOUNTPOINT`
   (default `/mnt/qnap/aispeaker`), `NFS_OPTIONS`. `provision-data.sh` then
   **generates** the `<mountpoint>.mount` + `.automount` units (name derived via
   `systemd-escape`) into the `/etc` overlay and enables the automount. Nothing
   NFS is baked into the image.
   > QNAP with NFSv4 uses a pseudo-root, so the export path is `/<share>`
   > (e.g. `192.168.1.15:/aispeaker`), not the `/share/...` v3 path.
3. Point the recordings dir at the share, e.g.
   `ln -sfn /mnt/qnap/aispeaker /data/opt/ai-smart-speaker/recordings`.

`soft,timeo=50` + automount = an absent NAS never hangs boot or the app (the
mount is attempted lazily on first access and fails fast).

**Fallback pattern (recommended):** rather than writing recordings straight to
NFS, have the app write locally to `/data/recordings` (always works, survives a
NAS/power outage) and a systemd timer `rsync` them to the NFS mount every few
minutes (optionally `--remove-source-files` after a successful copy). The hot
path never depends on the NAS; the NAS gets a copy when it's reachable.

## Roadmap

- **Phase 1 (this)**: RO system image, app deployed to `/data` via rsync+venv.
- **Phase 2**: recordings on NFS (log shipping is done — see below); optionally
  bake the app + its Python deps into the image (wheel-based recipes).
- **Phase 3**: A/B updates — second rootfs partition + RAUC/swupdate, image
  pulled from the NAS over the network. This gives "swap image on the NAS,
  reboot" without making boot depend on the network. Full network boot
  (TFTP/NFS root) is possible on RPi5, but couples the speaker's availability
  to the NAS and network — an offline-capable A/B scheme is the safer variant
  of the same workflow.

## Build status & known iteration points

**Status on wrynose (2026-08-27):** `bitbake -n aispeaker-image` resolves the
full graph — **11122 tasks, no errors, no warnings**. A real build has not been
run on this release yet; the last green *build* was on walnascar (7350 tasks →
`aispeaker-image-*.wic.bz2`, ~177 MB) with mopidy 4.0.1 + mopidy-mpd/spotify/
youtube, the Python deps, pipewire, wireplumber, respeaker-xvf3800,
networkmanager-wifi and gstreamer1.0-libav in the image.

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

Still to verify on real hardware / not covered by the build:

- Mopidy over the MPD protocol: smoke-test the app's commands (see the Mopidy
  section) — `outputs`/`enableoutput` map onto Mopidy's single output.
- Spotify: `gstreamer1.0-plugins-spotify` is a scaffold — generate its crate
  manifest with `cargo bitbake` before enabling (see the Mopidy section).
- `overlayfs-etc` preinit hook on the kernel cmdline — verify the generated
  cmdline / that `/etc` is writable after first boot.
- the in-place installer initramfs (USB module names, tryboot.txt dtb handling
  on RPi5) — validate on the spare Pi before using it on the speaker.
- audio actually reaching the XVF3800, and Python runtime module deps of the
  baked-in packages (yt-dlp etc.) — a boot + `mpc status` smoke test.
- system update = reflash / in-place installer / dd until Phase 3 lands.
- for servicing, the rootfs can be remounted rw: `mount -o remount,rw /`

## Why a subdirectory, not a separate repo

The layer packages files from `linux/` (symlinks) and must evolve together
with the application (Python deps ↔ image packages). A separate repo becomes
worthwhile only once CI builds the distro or other projects consume it.
