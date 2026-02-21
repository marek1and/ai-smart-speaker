#!/bin/bash

BASE_DIR="/opt/reSpeaker"
XVF_BIN="$BASE_DIR/xvf_host"
CMD_FILE="$BASE_DIR/init_commands.txt"
LOG_FILE="/tmp/respeaker_init.log"

export LD_LIBRARY_PATH="$BASE_DIR:$LD_LIBRARY_PATH"

echo "--- XVF3800 initialization script started: $(date) ---" > $LOG_FILE

# 1. Wait loop for the device
DEVICE_READY=0
for i in {1..15}; do
    if lsusb -d 2886:001a > /dev/null; then
        echo "Device found at attempt: $i" >> $LOG_FILE
        DEVICE_READY=1
        break
    fi
    sleep 1
done

if [ $DEVICE_READY -eq 0 ]; then
    echo "ERROR: Device not found after waiting." >> $LOG_FILE
    exit 1
fi

# Short pause after USB detection before sending commands
sleep 1

# 2. Parse and send commands with throttling
if [ -f "$CMD_FILE" ]; then
    echo "Starting command execution from $CMD_FILE..." >> $LOG_FILE
    
    # Read the file line by line. 
    # 'cmd' will take the first word, 'args' will take the rest of the line (handles multiple parameters, e.g., 7 3)
    while read -r cmd args; do
        # Skip empty lines and comments (#)
        [[ -z "$cmd" || "$cmd" =~ ^# ]] && continue
        
        echo "Sending: $cmd $args" >> $LOG_FILE
        
        # Binary call for a SINGLE command
        # $args without quotes, so bash splits "7 3" into two arguments
        $XVF_BIN "$cmd" $args >> $LOG_FILE 2>&1
        
        RET_CODE=$?
        
        if [ $RET_CODE -ne 0 ]; then
             echo "!!! WARN: Command $cmd returned error $RET_CODE. Retrying once..." >> $LOG_FILE
             sleep 0.5
             $XVF_BIN -cmd "$cmd" $args >> $LOG_FILE 2>&1
        fi

        # KEY: Delay between commands. 0.1s is usually enough, 0.2s for safety.
        sleep 0.1
        
    done < "$CMD_FILE"

    echo "Initialization sequence finished." >> $LOG_FILE
else
    echo "ERROR: Missing file $CMD_FILE" >> $LOG_FILE
    exit 1
fi
