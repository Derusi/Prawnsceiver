"""Static configuration for the NOAA APT receiver."""
import os

PORT = 8085
LOGDIR = "/var/log/noaa"
RECORD_DIR = "/var/log/noaa/recordings"
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")
RTL_LOG = os.path.join(LOGDIR, "rtl_sdr.log")
WEBDIR = "/home/eugene/aprs_website"
FFT_SIZE = 512
WATERFALL_ROWS = 120
SDR_GAIN = 35
SDR_RATE = 240000
# Offset tuning: rtl_sdr tunes SDR_OFFSET_HZ above the target frequency and the
# signal is shifted back to baseband in software. This moves the dongle's
# inherent center DC spike off the satellite signal (it would otherwise sit
# exactly on the APT carrier).
SDR_OFFSET_HZ = 60000
AUDIO_RATE = 48000
IQ_BLOCK = FFT_SIZE * 2
DECIMATION = SDR_RATE // AUDIO_RATE

# Regensburg coordinates
LAT, LON = 49.013, 12.099
UTC_OFFSET = 2  # Germany UTC+2

# NOAA satellites: catalog number -> (name, frequency_hz)
NOAA_SATS = {
    25338: ("NOAA 15", 137620000),
    28654: ("NOAA 18", 137912500),
    33591: ("NOAA 19", 137100000),
}

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 30  # Start recording 30s before rise, stop 30s after set
TLE_REFRESH_HOURS = 6  # Refresh TLE data every 6h
