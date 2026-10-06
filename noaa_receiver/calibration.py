"""Per-dongle calibration data.

Everything dongle-specific lives in this module — the primary dongle
selection, the FM-band range used by the correction rules, and the measured
per-dongle tuning corrections — separate from the general system config, so
recalibrating a dongle (or plugging in a new one) never touches config.py.
"""

# Primary dongle: the serial of the RTL-SDR that feeds live audio and WAV
# recording. USB device indices shift when dongles are (re)plugged, so the
# serial pins the physical dongle (rtl_sdr -d <serial>). If this dongle is
# not attached at startup, the first detected dongle becomes primary.
PRIMARY_DONGLE_SN = "77771111153705700"

# FM broadcast band (VHF2) range definition
FM_BAND = (87_500_000, 108_000_000)

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

def correction_info(freq_hz, serial=None):
    """(correction_hz, source) describing the tuning correction applied to a
    dongle at a frequency (see SDR_DONGLE_CORRECTIONS). The source labels
    where the value comes from: an exact measured frequency, the FM-band
    rule, a scaled ppm fallback, or nothing."""
    d = SDR_DONGLE_CORRECTIONS.get(serial) or SDR_DONGLE_CORRECTIONS.get(PRIMARY_DONGLE_SN, {})
    freqs = d.get("freqs", {})
    if freq_hz in freqs:
        return freqs[freq_hz], "measured"
    if FM_BAND[0] <= freq_hz <= FM_BAND[1]:
        return d.get("fm_band", 0), "FM-band"
    if d.get("ppm"):
        return int(round(freq_hz * d["ppm"] / 1e6)), "%d ppm" % d["ppm"]
    return 0, "none"

def tuning_correction(freq_hz, serial=None):
    """Static tuning correction for one dongle's crystal/PLL error at a
    frequency (see SDR_DONGLE_CORRECTIONS)."""
    return correction_info(freq_hz, serial)[0]
