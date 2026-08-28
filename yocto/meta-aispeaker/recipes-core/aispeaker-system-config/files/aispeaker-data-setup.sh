#!/bin/sh
# Creates the directory skeleton on the writable /data partition.
# Idempotent; runs on every boot before services that use /data.
set -e

mkdir -p /data/opt/ai-smart-speaker
mkdir -p /data/recordings
mkdir -p /data/music
mkdir -p /data/mopidy
mkdir -p /data/state
mkdir -p /data/cache/mopidy
mkdir -p /data/var/lib/NetworkManager
mkdir -p /data/var/log/journal

# Mopidy merges a second config for secrets/extensions; make sure it exists so
# the --config list never points at a missing file
[ -f /data/mopidy/mopidy.conf ] || cat > /data/mopidy/mopidy.conf <<'EOF'
# Site-specific Mopidy overrides (Spotify/YouTube credentials, etc.).
# This file lives on /data and is never in git.
EOF

chown -R speaker:speaker /data/opt /data/opt/ai-smart-speaker \
    /data/recordings /data/music /data/mopidy /data/state /data/cache
