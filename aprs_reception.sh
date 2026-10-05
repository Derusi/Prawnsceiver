#!/bin/bash
# APRS Reception Script - starts the unified Python server
# The server manages rtl_sdr (raw IQ) + direwolf + waterfall FFT in one process
# Uses setsid to fully detach from the calling process tree

LOGDIR="/var/log/aprs"
mkdir -p "$LOGDIR"

setsid python3 /home/eugene/aprs_website/server.py > "$LOGDIR/webserver.log" 2>&1 &
echo "APRS Monitor started (pid $!)"
