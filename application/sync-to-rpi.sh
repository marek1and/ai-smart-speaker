#!/bin/bash
# Sync project files to the speaker
# Usage: ./sync-to-rpi.sh [host]
#   host defaults to 'aispeaker' (ssh alias for speaker@ the speaker; on Yocto
#   the app's files must belong to speaker, so never sync as root)

set -e

HOST="${1:-aispeaker}"
REMOTE_DIR="/opt/ai-smart-speaker"

echo "Syncing to ${HOST}:${REMOTE_DIR}..."

rsync -avz --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude '.gitignore' \
    --exclude '.idea' \
    --exclude '.vscode' \
    --exclude '*.egg-info' \
    --exclude '.mypy_cache' \
    --exclude '.pytest_cache' \
    --exclude 'recordings' \
    --exclude '.env' \
    --exclude '*.log' \
    --exclude 'radio_state.json' \
    ./ "${HOST}:${REMOTE_DIR}/"

echo "Done! Files synced to ${HOST}:${REMOTE_DIR}"
