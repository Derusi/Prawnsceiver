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
# 2026-10-08: the NESDR SMArt v5 (R820T2, 0.5 ppm TCXO) replaces the ancient
# generic R820T as primary; the R820T stays as secondary.
PRIMARY_DONGLE_SN = "48263793"

# AIS dongle: serial of an RTL-SDR dedicated to ship-traffic reception
# (161.975/162.025 MHz, Danube vessels -- see noaa_receiver/ais.py). AIS
# needs a continuously listening receiver, so a dongle pinned here is
# excluded from satellite tracking and never becomes the primary. Plug in
# any spare dongle, read its serial from the /console dongle enumeration
# line (or rtl_sdr -d 99), and set it here to enable AIS. None = off.
# 2026-10-08: the old generic R820T (77771111153705700) is dedicated to
# AIS. Its ~+80 ppm crystal error is covered by the ppm fallback below
# (~+13 kHz correction at 162 MHz); the AIS demod tolerates any residual
# (per-burst DC removal, 14 kHz channel filter). If channels sit visibly
# off-center in the waterfall (+/-25 kHz around center), measure the
# offset and add 162000000 to this dongle's 'freqs' table like the other
# bands.
AIS_DONGLE_SN = "77771111153705700"

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
    # Primary R820T: never confirms PLL lock, but the crystal error is a
    # smooth +72..82 ppm HIGH across all bands (LO tunes high -> correction
    # negative). Measured 2026-10-06, four independent methods agree:
    #   - rtl_test -p: cumulative PPM 82 / 72 (sample clock = PLL reference)
    #   - 103.0 MHz: FM stereo pilot 18998.05 Hz with -8200 applied -> +8198 Hz
    #   - 144.8 MHz: local carriers line up with the 144.6929/144.754/144.8761
    #     survey (that survey reported true RF minus the LO error)
    #   - 437.55 MHz: 15:08 ISS ridges -22.5..-28.7 kHz raw fit +34.8 kHz
    #     with approach Doppler +6..+12 kHz
    # The earlier +21.6 kHz VHF3 value came from an R820T spur at bin 302
    # misread as the satellite: with +21600 applied, neither dongle showed
    # any APT ridge anywhere in the +/-120 kHz window during the 18:00 and
    # 19:38 NOAA 15 passes (58 deg max elevation).
    "77771111153705700": {
        "freqs": {
            137620000: -10955,  # NOAA 15 (79.6 ppm x 137.62 MHz)
            137912500: -10977,  # NOAA 18
            137100000: -10913,  # NOAA 19
            437550000: -34827,  # ISS (Zarya) (79.6 ppm x 437.55 MHz)
        },
        "fm_band": -8200,  # VHF2: tunes ~8 kHz HIGH (carriers 6-11 kHz below nominal, measured on 89.7/93.0/95.0/99.6/103.0/105.0)
        "ppm": 80,
    },
    # FC0013 dongle: rtl_test -p measured cumulative PPM 47 / 44 (crystal
    # high), consistent with the 89.7 MHz carrier measurement (+3.5 kHz
    # = +39 ppm there). NOAA-band corrections use the ppm fallback.
    "00000991": {
        "freqs": {},
        "fm_band": 3500,
        "ppm": 45,
    },
}

# Per-dongle fixed tuner gain in dB. Absent serial -> SDR_GAIN from config
# (0 = the tuner's AGC). Fixed gain gives a stable noise floor: AGC pumps the
# gain down as a satellite rises, which shifts the whole waterfall during a
# pass and makes pass-to-pass comparisons unreliable.
# 2026-10-08 EXPERIMENT VERDICT: 29.7 dB manual on the R820T left the ADC
# ~17-22 dB under-driven vs AGC (IQ rms 1.29 vs 9.4-15.5 u8-units = ~1% of
# ADC range, quantization eats ~3 of the 8 bits) — audio was indistinguish-
# able, IQ/digital decodes destroyed (see EVENTLOG 11:40). AGC wins until a
# fixed value is MEASURED for a specific dongle (sweep gain on a strong FM
# station / NOAA pass, pick the knee before noise floor rise); do not guess
# from a guide's number for a different dongle.
SDR_DONGLE_GAIN = {}

def correction_info(freq_hz, serial=None):
    """(correction_hz, source) describing the tuning correction applied to a
    dongle at a frequency (see SDR_DONGLE_CORRECTIONS). The source labels
    where the value comes from: an exact measured frequency, the FM-band
    rule, a scaled ppm fallback, or nothing.

    An UNKNOWN serial gets a zero correction marked 'unmeasured dongle':
    inheriting the primary's table would mis-tune a modern TCXO dongle
    (e.g. a NESDR SMArt v5 at 0.5 ppm) by ~11 kHz on first use. The dongle
    cards' tuning infobox shows the marker until the dongle gets measured
    and its own entry."""
    d = SDR_DONGLE_CORRECTIONS.get(serial)
    if d is None:
        return 0, "unmeasured dongle"
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
