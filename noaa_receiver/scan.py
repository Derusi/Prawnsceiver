"""Frequency scanner + recorded-bandwidth fit (dashboard dongle cards).

The scan sweeps one dongle across a frequency range and parks it on the
next strong signal. It never touches rtl_tcp itself: it only writes the
per-dongle frequency override (state.manual_dongle_freq), which the
capture thread picks up within one IQ block and applies as a live
rtl_tcp retune command — the stream keeps flowing, so the waterfall
shows the swept spectrum in real time and no audio gap opens.

Signal detection runs on the raw FFT magnitude rows the capture thread
publishes as entry['last_mag'] (512 bins, one row every ~34 ms), averaged
over each step's dwell window (the same trick as a spectrogram average):

- noise floor = median of the averaged row (mostly noise bins even when
  a strong carrier is in it), peak = strongest bin OUTSIDE the
  +SDR_OFFSET_HZ window — the dongle's center DC spike (~2x floor,
  measured 2026-10-08) lives there and would end every scan instantly;
- the time-average is the impulse filter: a stationary carrier keeps
  its level in every row, while the single-block impulses that saturate
  one random bin per row at this site (measured: wandering full-scale
  peaks in every waterfall row) are clipped at 8x their row's median and
  then divided by the number of averaged rows, so they fall below the
  hit threshold however hot they are;
- a step is a hit when the averaged row's peak/floor exceeds the ratio
  threshold — a ratio (default 3 = +9.5 dB over the floor) needs no
  absolute calibration, so it works at any gain.

On a hit the dongle is re-centered on the measured peak (bin offset)
and the hit confirmed with a second dwell in which the peak must land
within CONFIRM_TOLERANCE_HZ of 0 Hz — a real signal stays put when the
dongle tunes onto it, while tune-relative artifacts (tuner spurs,
band-edge junk) move away and are rejected; then the recorded (demod)
bandwidth is fitted from the signal's measured width, so the WAV/live
audio band is neither too wide (extra noise) nor too narrow (clipping
the deviation). fit_bandwidth() is served on its own endpoint for the
card's Fit button, to re-fit after a manual tune.

Safety: scanning sets the per-dongle override, which suppresses pass
recording for that dongle (same as a manual tune). A scan of the
PRIMARY dongle therefore aborts itself the moment a satellite pass
rises and rejoins the shared (pass) frequency; /scan_dongle also
refuses to start one during an active pass.
"""
import math
import threading
import time

import numpy as np

from . import state
from .config import FFT_SIZE, SDR_OFFSET_HZ, SDR_RATE

SCAN_SETTLE_SECS = 0.25    # discard first samples after a retune (tuner re-lock)
SCAN_DWELL_SECS = 0.7      # measurement window per step
SCAN_SAMPLE_EVERY = 0.05   # magnitude-row sampling inside the dwell
SCAN_RATIO = 3.0           # hit threshold: peak/floor = +9.5 dB over the floor
SPIKE_GUARD_HZ = 15000     # bins masked around +SDR_OFFSET_HZ (the DC spike)
FIT_MIN_RATIO = 1.8         # below this nothing "stands out" — refuse to fit
STEP_MIN_HZ, STEP_MAX_HZ = 20000, 400000
# After re-centering on a hit, the peak must land within this of 0 Hz: a
# real signal stays put when the dongle tunes onto it, while a
# tune-relative artifact (tuner spur, band-edge junk — measured live:
# phantom finds at +40.8 kHz and at the ±120 kHz window edges) moves
# with the tuner and fails the confirm here.
CONFIRM_TOLERANCE_HZ = 5000

def _bin_hz():
    return SDR_RATE / FFT_SIZE

def _measure(mag):
    """One magnitude row -> (peak_bin, peak/floor ratio, floor).

    Bins within SPIKE_GUARD_HZ of +SDR_OFFSET_HZ are excluded from the
    peak search (the dongle's DC spike). The floor is the row median.
    """
    m = np.asarray(mag, dtype=np.float64)
    n = len(m)
    if n < 8:
        return 0, 0.0, 0.0
    floor = float(np.median(m))
    if floor <= 0.0:
        floor = 1e-9
    c = int(round(n / 2 + SDR_OFFSET_HZ / _bin_hz()))
    w = int(round(SPIKE_GUARD_HZ / _bin_hz()))
    m = m.copy()
    m[max(0, c - w):min(n, c + w + 1)] = 0.0
    idx = int(np.argmax(m))
    return idx, float(m[idx]) / floor, floor

def _peak_offset_hz(mag):
    """Frequency offset of the row's (spike-masked) peak from the center."""
    idx, ratio, _ = _measure(mag)
    if ratio <= 0.0:
        return None
    return (idx - len(mag) / 2.0) * _bin_hz()

def _signal_width_hz(mag):
    """Occupied width (Hz) of the signal around the row's peak.

    Contiguous run around the peak above max(1.5x floor, peak/8 ~ -18 dB);
    the masked spike bins (0.0) end the run like a real gap would. Returns
    0 when nothing stands out of the noise.
    """
    m = np.asarray(mag, dtype=np.float64)
    idx, ratio, floor = _measure(m)
    if ratio < FIT_MIN_RATIO:
        return 0.0
    thresh = max(floor * 1.5, m[idx] / 8.0)
    left = idx
    while left > 0 and m[left - 1] > thresh:
        left -= 1
    right = idx
    n = len(m)
    while right < n - 1 and m[right + 1] > thresh:
        right += 1
    return (right - left + 1) * _bin_hz()

def fit_bw_hz(width_hz):
    """Demod (recorded) low-pass cutoff for a signal of this occupied
    width: half the width plus 30% margin, rounded to 100 Hz and clamped
    to the 1-120 kHz range /tune_dongle validates."""
    bw = int(round(width_hz / 2.0 * 1.3 / 100.0) * 100)
    return max(1000, min(120000, bw))

def _sample(entry, sc, settle=SCAN_SETTLE_SECS, dwell=SCAN_DWELL_SECS, min_rows=5):
    """Average the magnitude rows at the current tune ->
    (peak/floor ratio of the AVERAGED row, averaged row).

    The time-average is the impulse filter (see module docstring): a
    stationary carrier keeps its level, single-block impulses are
    clipped at 8x their row's median (bounding them however hot they
    are) and then divided by the number of averaged rows.

    Rows are collected until the dwell window has elapsed AND at least
    min_rows have been seen — a short rtl_tcp delivery stall right
    after a retune command (tuner re-lock, USB hiccup; measured live:
    one step of the FM-band shakedown got <5 rows in 0.7 s) then merely
    lengthens the window instead of failing the step. A hard deadline
    of 4 dwell windows bounds the wait; (0.0, None) is returned only
    when the scan is stopped or no data ever arrived (dead dongle).
    Only rows not yet seen are accumulated (the capture thread
    publishes a new array object per FFT).
    """
    time.sleep(settle)
    rows = []
    soft_deadline = time.time() + dwell
    hard_deadline = soft_deadline + 3 * dwell
    last_seen = None
    while True:
        if sc.get("stop"):
            return 0.0, None
        with entry["lock"]:
            mag = entry.get("last_mag")
        if mag is not None and mag is not last_seen:
            last_seen = mag
            r = np.asarray(mag, dtype=np.float64)
            med = float(np.median(r))
            if med > 0.0:
                np.clip(r, 0.0, 8.0 * med, out=r)
            rows.append(r)
        if rows and time.time() >= soft_deadline and len(rows) >= min_rows:
            break
        if time.time() >= hard_deadline:
            break
        time.sleep(SCAN_SAMPLE_EVERY)
    if len(rows) < min_rows:
        return 0.0, None
    mean_row = np.mean(rows, axis=0)
    _, ratio, _ = _measure(mean_row)
    return ratio, mean_row

def start_scan(serial, start_mhz, end_mhz, step_khz, ratio=SCAN_RATIO):
    """Register the scan state and spawn its thread (handler entry point)."""
    start_hz, end_hz = int(round(start_mhz * 1e6)), int(round(end_mhz * 1e6))
    step_hz = int(round(step_khz * 1000))
    sc = {
        "active": True, "stop": False,
        "from_hz": start_hz, "to_hz": end_hz, "step_hz": step_hz,
        "ratio": float(ratio), "cur_hz": start_hz,
        "found_hz": None, "found_ratio": None, "bw_khz": None,
        "result": None,
    }
    state.scans[serial] = sc
    threading.Thread(target=scan_dongle_thread, args=(serial,),
                     daemon=True, name=f"scan-{serial}").start()
    return sc

def stop_scan(serial):
    """Ask a running scan to stop at the current frequency (idempotent)."""
    sc = state.scans.get(serial)
    if sc is not None and sc.get("active"):
        sc["stop"] = True
        return True
    return False

def scan_snapshot(serial):
    """Scan state for /dongles.json (None when this dongle never scanned)."""
    sc = state.scans.get(serial)
    if not sc:
        return None
    return {
        "active": sc["active"],
        "from_mhz": round(sc["from_hz"] / 1e6, 4),
        "to_mhz": round(sc["to_hz"] / 1e6, 4),
        "step_khz": round(sc["step_hz"] / 1000.0, 1),
        "cur_mhz": round(sc["cur_hz"] / 1e6, 4),
        "found_mhz": round(sc["found_hz"] / 1e6, 4) if sc["found_hz"] else None,
        "found_db": round(20 * math.log10(sc["found_ratio"]), 1) if sc["found_ratio"] else None,
        "bw_khz": sc["bw_khz"],
        "result": sc["result"],
    }

def scan_dongle_thread(serial):
    """Sweep one dongle's override from from_hz to to_hz and park it on
    the first confirmed strong signal (see module docstring)."""
    sc = state.scans.get(serial)
    entry = state.sdrs.get(serial)
    if sc is None or entry is None:
        return
    with state.status_lock:
        saved_override = state.manual_dongle_freq.get(serial)
    step_dir = 1 if sc["to_hz"] >= sc["from_hz"] else -1
    freq = sc["from_hz"]
    center = freq
    state.log_console(f"🔍 Scan started (dongle {serial}): {sc['from_hz']/1e6:g}-{sc['to_hz']/1e6:g} MHz, "
                      f"{sc['step_hz']/1000:g} kHz steps, stopping above +{round(20*math.log10(sc['ratio']))} dB over the floor")
    try:
        while (step_dir > 0 and freq <= sc["to_hz"]) or (step_dir < 0 and freq >= sc["to_hz"]):
            if sc["stop"]:
                sc["result"] = "stopped"
                break
            # A scan of the primary dongle suppresses its pass recording;
            # a rising pass wins — abort and rejoin the shared frequency
            if entry["primary"] and state.is_pass_active:
                sc["result"] = "aborted"
                state.log_console(f"Scan of dongle {serial} aborted — satellite pass started", "warn")
                break
            sc["cur_hz"] = freq
            with state.status_lock:
                state.manual_dongle_freq[serial] = freq
            ratio, avg_row = _sample(entry, sc)
            if avg_row is None:   # stopped, or no IQ/FFT data at all
                sc["result"] = "stopped" if sc["stop"] else "nodata"
                break
            # Someone (card Tune/Sync, another scan) took the override over:
            # the scan no longer controls this dongle — leave it to them
            if state.manual_dongle_freq.get(serial) != freq:
                sc["result"] = "overtaken"
                break
            if ratio >= sc["ratio"]:
                # Re-center on the measured peak and confirm the hit there
                off = _peak_offset_hz(avg_row)
                center = freq + int(round(off)) if off is not None else freq
                center = max(24000000, min(1766000000, center))
                sc["cur_hz"] = center
                with state.status_lock:
                    state.manual_dongle_freq[serial] = center
                confirm_r, confirm_row = _sample(entry, sc)
                confirm_off = _peak_offset_hz(confirm_row) if confirm_row is not None else None
                if (confirm_row is not None and confirm_r >= sc["ratio"]
                        and confirm_off is not None and abs(confirm_off) <= CONFIRM_TOLERANCE_HZ):
                    sc["found_hz"], sc["found_ratio"] = center, confirm_r
                    sc["result"] = "found"
                    break
                # transient, or a tune-relative artifact that moved away
                # from the re-centered peak — keep sweeping
            freq += step_dir * sc["step_hz"]
        if sc["result"] is None:
            sc["result"] = "nothing"
    finally:
        sc["active"] = False
        if sc["result"] == "found":
            # Parked on the signal: fit the recorded (demod) bandwidth to
            # its measured width, so the WAV/live audio band matches it
            width = _signal_width_hz(confirm_row) if confirm_row is not None else 0.0
            db = round(20 * math.log10(sc["found_ratio"]))
            if width > 0:
                bw = fit_bw_hz(width)
                sc["bw_khz"] = round(bw / 1000.0, 1)
                with state.status_lock:
                    state.manual_dongle_bw[serial] = bw
                state.log_console(f"🔍 Scan (dongle {serial}): strong signal at {center/1e6:.4f} MHz "
                                  f"(+{db} dB over the floor) — parked, recorded bandwidth fitted to ±{sc['bw_khz']:g} kHz")
            else:
                state.log_console(f"🔍 Scan (dongle {serial}): strong signal at {center/1e6:.4f} MHz "
                                  f"(+{db} dB over the floor) — parked")
        elif sc["result"] == "stopped":
            state.log_console(f"🔍 Scan stopped (dongle {serial}) — parked at {sc['cur_hz']/1e6:.4f} MHz")
        elif sc["result"] in ("nothing", "aborted", "nodata"):
            # Nothing found (or a pass needs the dongle / it stopped
            # delivering): restore whatever override state it had before
            with state.status_lock:
                if saved_override is None:
                    state.manual_dongle_freq.pop(serial, None)
                else:
                    state.manual_dongle_freq[serial] = saved_override
            if sc["result"] == "nothing":
                state.log_console(f"🔍 Scan done (dongle {serial}): nothing above "
                                  f"+{round(20*math.log10(sc['ratio']))} dB in {sc['from_hz']/1e6:g}-{sc['to_hz']/1e6:g} MHz")
            elif sc["result"] == "nodata":
                state.log_console(f"Scan ended (dongle {serial}): no spectrum data from the capture "
                                  f"thread — dongle stalled or restarting?", "warn")

def fit_bandwidth(serial):
    """Measure the live signal at the dongle's CURRENT tune and fit the
    recorded (demod) bandwidth to it (handler endpoint for the card's
    Fit button). Leaves the bandwidth untouched when no signal stands
    out of the noise."""
    entry = state.sdrs.get(serial)
    if entry is None:
        return {"success": False, "error": "Unknown dongle"}
    _, mean_row = _sample(entry, {"stop": False}, settle=SCAN_SETTLE_SECS, dwell=0.5)
    if mean_row is None:
        return {"success": False, "error": "No spectrum data from this dongle yet"}
    _, ratio, _ = _measure(mean_row)
    width = _signal_width_hz(mean_row)
    if width <= 0.0:
        return {"success": False, "error": "No signal standing out of the noise at the current tune",
                "ratio_db": round(20 * math.log10(ratio), 1) if ratio > 1 else None}
    bw = fit_bw_hz(width)
    with state.status_lock:
        state.manual_dongle_bw[serial] = bw
    off = _peak_offset_hz(mean_row)
    state.log_console(f"🎚 Dongle {serial}: recorded bandwidth fitted — signal "
                      f"{round(width/1000.0, 1)} kHz wide, demod set to ±{round(bw/1000.0, 1)} kHz")
    return {
        "success": True,
        "width_khz": round(width / 1000.0, 1),
        "bw_khz": round(bw / 1000.0, 1),
        "offset_hz": round(off) if off else 0,
        "ratio_db": round(20 * math.log10(ratio), 1),
    }
