# PipeWire runs in system mode — make CLI tools (pw-cli, pw-top, pactl, mpc)
# and pip caches work in interactive shells
export PIPEWIRE_RUNTIME_DIR=/run/pipewire
export PULSE_SERVER=unix:/run/pipewire/pulse/native
# keep caches (pip, huggingface) off the read-only rootfs
export XDG_CACHE_HOME=/data/cache
