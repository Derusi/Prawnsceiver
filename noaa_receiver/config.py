"""Static configuration for the NOAA APT / ISS SSTV receiver."""
import os

PORT = 8085
LOGDIR = "/var/log/noaa"
RECORD_DIR = "/var/log/noaa/recordings"
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")
RTL_LOG = os.path.join(LOGDIR, "rtl_sdr.log")
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
# Per-frequency static tuning corrections (Hz), added to the rtl_sdr tune
# command. This dongle's R820T never confirms PLL lock and is mistuned by a
# per-band offset (measured: about +21.6 kHz at 137.6 MHz, -8.4 kHz at
# 103 MHz — the error differs per VCO band, so a single ppm value doesn't
# work). Values are measured from the live waterfall during passes: the
# satellite carrier's distance from the FFT center is this correction.
SDR_FREQ_CORRECTION_HZ = {
    137620000: 21600,   # NOAA 15 (measured: carrier +21.6 kHz in the waterfall)
    137912500: 21650,   # NOAA 18 (scaled by frequency within the band)
    137100000: 21500,   # NOAA 19 (scaled by frequency within the band)
    437550000: -30000,  # ISS (Zarya) — measured from the 15:08 pass waterfall (-26..-28 kHz raw, minus Doppler)
}
# FM broadcast band (VHF2): the dongle tunes ~8 kHz HIGH here — broadcast
# carriers show up 6-11 kHz below their nominal frequency (measured on 89.7,
# 93.0, 95.0, 99.6, 103.0, 105.0 MHz). Opposite sign of the VHF3 NOAA error,
# so manual FM radio test tunes need a negative correction.
FM_BAND = (87_500_000, 108_000_000)
FM_BAND_CORRECTION_HZ = -8200

def tuning_correction(freq_hz):
    """Static tuning correction for the mistuned R820T at any frequency."""
    if freq_hz in SDR_FREQ_CORRECTION_HZ:
        return SDR_FREQ_CORRECTION_HZ[freq_hz]
    if FM_BAND[0] <= freq_hz <= FM_BAND[1]:
        return FM_BAND_CORRECTION_HZ
    return 0
AUDIO_RATE = 48000
IQ_BLOCK = FFT_SIZE * 2
DECIMATION = SDR_RATE // AUDIO_RATE

# Regensburg coordinates
LAT, LON = 49.013, 12.099
UTC_OFFSET = 2  # Germany UTC+2

# Tracked satellites: catalog number -> (name, frequency_hz)
# ISS: ARISS SSTV (Robot 36) on 437.550 MHz during active events
TRACKED_SATS = {
    25338: ("NOAA 15", 137620000),
    28654: ("NOAA 18", 137912500),
    33591: ("NOAA 19", 137100000),
    25544: ("ISS (Zarya)", 437550000),
}

# ISS only transmits SSTV during ARISS events; outside events its passes would
# be recorded as empty WAVs (~350 MB/day). Set False to track ISS in the pass
# list without recording it.
RECORD_ISS = True

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 60  # Start recording 60s before rise, stop 60s after set
TLE_REFRESH_HOURS = 6  # Refresh TLE data every 6h
TLE_CACHE_FILE = os.path.join(LOGDIR, "tle_cache.json")  # last good TLEs, used when Celestrak is unreachable
# Celestrak rejects requests with generic bot user-agents (HTTP 403)
TLE_USER_AGENT = "NOAAh-CrabArk/1.0 (amateur NOAA APT ground station; https://github.com/Derusi/Prawnsceiver)"
