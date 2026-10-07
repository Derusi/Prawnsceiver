"""Static configuration for the NOAA APT / ISS SSTV receiver.

Dongle-specific calibration (primary dongle, per-dongle tuning corrections)
lives in calibration.py, separate from this system config.
"""
import os

PORT = 8085
LOGDIR = "/var/log/noaa"
RECORD_DIR = "/var/log/noaa/recordings"
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")
RTL_LOG = os.path.join(LOGDIR, "rtl_sdr.log")
# noaa-apt (APT image decoder): install directory (its res/ folder must be
# the working directory), per-decode timeout, and the TLE file the
# scheduler writes for its map overlay (3-line format, refreshed with the
# pass-prediction TLEs so the overlay never uses noaa-apt's bundled stale set)
NOAA_APT_DIR = "/opt/noaa-apt"
NOAA_APT_TIMEOUT_SECS = 120
NOAA_APT_TLE_FILE = os.path.join(LOGDIR, "weather.txt")
WEBDIR = "/home/eugene/aprs_website"
FFT_SIZE = 512
WATERFALL_ROWS = 120
# Gain: 0 = auto (RTL-SDR AGC), or fixed dB like 35. Auto adapts to signal
# strength during passes, which is better than a fixed value.
SDR_GAIN = 0  # auto-gain
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

# Tracked satellites: catalog number -> (name, frequency_hz)
# ISS: ARISS SSTV (Robot 36) on 437.550 MHz during active events
# Meteor-M 2-3/2-4: Russian weather satellites, LRPT digital downlink
# (137.100 / 137.9125 MHz). Not decodable by the APT pipeline — tracked for
# the pass list, recordings and reception history.
TRACKED_SATS = {
    25338: ("NOAA 15", 137620000),
    28654: ("NOAA 18", 137912500),
    33591: ("NOAA 19", 137100000),
    25544: ("ISS (Zarya)", 437550000),
    57166: ("Meteor-M 2-3", 137100000),
    59051: ("Meteor-M 2-4", 137912500),
}

# ISS only transmits SSTV during ARISS events; outside events its passes would
# be recorded as empty WAVs (~350 MB/day). Set False to track ISS in the pass
# list without recording it.
RECORD_ISS = True

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 60  # Start recording 60s before rise, stop 60s after set
TLE_REFRESH_HOURS = 3  # Refresh TLE data every 3h
TLE_CACHE_FILE = os.path.join(LOGDIR, "tle_cache.json")  # last good TLEs, used when Celestrak is unreachable
# Celestrak rejects requests with generic bot user-agents (HTTP 403)
TLE_USER_AGENT = "NOAAh-CrabArk/1.0 (amateur NOAA APT ground station; https://github.com/Derusi/Prawnsceiver)"

# Doppler correction: a satellite's carrier drifts by up to ±3 kHz (NOAA,
# 137 MHz) / ±10 kHz (ISS, 437 MHz) over a pass. The scheduler computes the
# live range-rate Doppler during passes; capture threads rotate the baseband
# in software (rtl_sdr cannot be retuned without restarting it, which would
# gap the recording). Applied only when a dongle's target is within
# DOPPLER_APPLY_RANGE_HZ of the tracked satellite frequency.
DOPPLER_APPLY_RANGE_HZ = 500_000
DOPPLER_UPDATE_SECS = 10  # scheduler tick cadence; steps stay < ~1 kHz on ISS
