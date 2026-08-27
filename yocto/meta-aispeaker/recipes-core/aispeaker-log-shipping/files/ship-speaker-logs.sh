#!/bin/sh
# Ships new journal entries to the NFS share, one file per day.
#
# The journal itself is persistent on /data, so this is not the only copy —
# it exists so the logs can be read from the NAS and from the dev machine
# without touching the speaker, and so they outlive a reflash of the card.
#
# The cursor lives on /data (NOT in /run): with a persistent journal a cursor
# lost on reboot would make us re-ship the entire history as duplicates.
set -u

CONF=/etc/default/aispeaker-log-shipping
[ -f "$CONF" ] && . "$CONF"

DEST="${LOG_DEST:-/mnt/qnap/aispeaker/logs}"
UNITS="${LOG_UNITS:-ai-smart-speaker.service}"
RETENTION_DAYS="${LOG_RETENTION_DAYS:-90}"
CURSOR=/data/state/speaker-log.cursor
TMPCUR="$CURSOR.tmp"
BATCH=/run/speaker-log.batch

# The share is an automount: touching it triggers the mount, and a missing NAS
# fails fast (soft mount) instead of hanging the timer.
if ! mkdir -p "$DEST" 2>/dev/null; then
    echo "log destination $DEST unavailable — skipping this run" >&2
    exit 0
fi

if [ -f "$CURSOR" ]; then cp -f "$CURSOR" "$TMPCUR"; else rm -f "$TMPCUR"; fi

set -- ""
for u in $UNITS; do set -- "$@" -u "$u"; done
shift

# grep drops journalctl's own chatter ("-- No entries --", boot markers)
journalctl "$@" --cursor-file="$TMPCUR" -o short-iso --no-pager 2>/dev/null \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' > "$BATCH"

if [ -s "$BATCH" ]; then
    # split per day by the date the entry carries, so late arrivals still land
    # in the right file; only advance the cursor once the write succeeded
    if awk -v dir="$DEST" '{
            d = substr($1, 1, 10)
            f = (d ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/) \
                ? dir "/ai-speaker-" d ".log" : dir "/ai-speaker-undated.log"
            print >> f
        }' "$BATCH"; then
        [ -f "$TMPCUR" ] && mv -f "$TMPCUR" "$CURSOR"
    else
        echo "writing to $DEST failed — keeping cursor, will retry" >&2
    fi
fi
rm -f "$BATCH"

find "$DEST" -name 'ai-speaker-*.log' -mtime +"$RETENTION_DAYS" -delete 2>/dev/null
exit 0
