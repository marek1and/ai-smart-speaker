#!/bin/bash
# Sync project files to Raspberry Pi
# Usage: ./sync.sh [hostname]
#   hostname defaults to 'raspberrypi' if not provided

set -e

HOST="${1:-raspberrypi}"
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
