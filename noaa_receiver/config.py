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
# Primary dongle: the serial of the RTL-SDR that feeds live audio and WAV
# recording. USB device indices shift when dongles are (re)plugged, so the
# serial pins the physical dongle (rtl_sdr -d <serial>). If this dongle is
# not attached at startup, the first detected dongle becomes primary.
PRIMARY_DONGLE_SN = "77771111153705700"

# Tuning corrections per dongle serial. Every dongle mistsunes with its own
# crystal/PLL error, so the correction that centers one dongle displaces
# another (the R820T's -8.2 kHz FM-band correction left the FC0013's
# carrier +11.7 kHz off). Values are measured from the live waterfall: the
# distance of a known carrier from the FFT center is this correction
# (positive = dongle tunes low = add to the tune command).
#   'freqs': exact-frequency corrections in Hz
#   'fm_band': FM broadcast band (87.5-108 MHz) correction
#   'ppm': fallback for frequencies not listed (scaled); None = no fallback
# Dongles not listed here fall back to the primary dongle's values.
SDR_DONGLE_CORRECTIONS = {
    # Primary R820T: never confirms PLL lock; the error differs per VCO band
    # (VHF3 ~157 ppm low, VHF2 ~80 ppm high) so a single ppm doesn't fit
    "77771111153705700": {
        "freqs": {
            137620000: 21600,   # NOAA 15 (measured: carrier +21.6 kHz in the waterfall)
            137912500: 21650,   # NOAA 18 (scaled by frequency within the band)
            137100000: 21500,   # NOAA 19 (scaled by frequency within the band)
            437550000: -30000,  # ISS (Zarya) — measured from the 15:08 pass waterfall (-26..-28 kHz raw, minus Doppler)
        },
        "fm_band": -8200,  # VHF2: tunes ~8 kHz HIGH (carriers 6-11 kHz below nominal, measured on 89.7/93.0/95.0/99.6/103.0/105.0)
        "ppm": None,
    },
    # FC0013 dongle: ~39 ppm low, measured at 89.7 MHz (carrier landed +11.7
    # kHz off with the R820T correction applied -> own error ~+3.5 kHz there).
    # The NOAA/VHF3 correction follows the ppm fallback until measured on a pass.
    "00000991": {
        "freqs": {},
        "fm_band": 3500,
        "ppm": 39,
    },
}
# FM broadcast band (VHF2) range definition
FM_BAND = (87_500_000, 108_000_000)

def tuning_correction(freq_hz, serial=None):
    """Static tuning correction for one dongle's crystal/PLL error at a
    frequency (see SDR_DONGLE_CORRECTIONS)."""
    d = SDR_DONGLE_CORRECTIONS.get(serial) or SDR_DONGLE_CORRECTIONS.get(PRIMARY_DONGLE_SN, {})
    freqs = d.get("freqs", {})
    if freq_hz in freqs:
        return freqs[freq_hz]
    if FM_BAND[0] <= freq_hz <= FM_BAND[1]:
        return d.get("fm_band", 0)
    if d.get("ppm"):
        return int(round(freq_hz * d["ppm"] / 1e6))
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
