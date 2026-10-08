"""Tests for noaa_receiver.scan: peak/floor measurement, width fit, scan sweep.

Run: python3 tests/test_scan.py (needs numpy). Asserts:

- _measure finds the true peak bin and ratio, ignoring the DC-spike
  window at +SDR_OFFSET_HZ (a spike there must never win the peak search)
- _signal_width_hz measures a synthetic hump's occupied width and
  refuses to measure noise
- fit_bw_hz maps width -> demod cutoff (APT-like signal -> ~22 kHz) and
  clamps to the 1-120 kHz range
- the scan thread end-to-end against a fake dongle entry (magnitude rows
  fed by a helper thread): nothing found (override restored), found
  (parked on the re-centered peak, bandwidth fitted), stop request
  (parked at the current frequency)
"""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from noaa_receiver import scan, state
from noaa_receiver.config import FFT_SIZE, SDR_OFFSET_HZ, SDR_RATE

BIN_HZ = SDR_RATE / FFT_SIZE

# --- 1. _measure: peak bin, ratio, DC-spike masking ---
def noise_row(seed):
    rng = np.random.RandomState(seed)
    return rng.uniform(90.0, 110.0, FFT_SIZE).astype(np.float32)

def signal_row(seed, offset_hz, amp=500.0, width_bins=40.0):
    m = noise_row(seed).astype(np.float64)
    idx = np.arange(FFT_SIZE)
    c = FFT_SIZE // 2 + offset_hz / BIN_HZ
    m += amp * np.exp(-0.5 * ((idx - c) / (width_bins / 2.35)) ** 2)
    return m.astype(np.float32)

row = signal_row(1, -50000)
idx, ratio, floor = scan._measure(row)
assert abs((idx - FFT_SIZE // 2) * BIN_HZ - (-50000)) < 4 * BIN_HZ, "peak bin offset wrong"
assert 4.0 < ratio < 6.0, f"peak/floor ratio off: {ratio}"
assert 90 < floor < 110, "floor should track the noise median"

# A 4x-floor spike INSIDE the +SDR_OFFSET_HZ window must not win:
spiked = signal_row(2, -50000)
c = int(round(FFT_SIZE // 2 + SDR_OFFSET_HZ / BIN_HZ))
spiked[c] = 450.0
idx2, ratio2, _ = scan._measure(spiked)
assert abs((idx2 - FFT_SIZE // 2) * BIN_HZ - (-50000)) < 4 * BIN_HZ, \
    "DC spike inside the guard window won the peak search"
assert ratio2 < 6.5, "spike contaminated the ratio"

# Pure noise: ratio stays near 1
_, rnoise, _ = scan._measure(noise_row(3))
assert rnoise < 1.3, f"noise measured as a signal: {rnoise}"
print("1. _measure peak/floor + DC-spike masking: ok")

# --- 2. _signal_width_hz ---
w = scan._signal_width_hz(signal_row(4, -50000))
assert 15000 < w < 40000, f"hump width off: {w} Hz"
assert scan._signal_width_hz(noise_row(5)) == 0.0, "noise must measure zero width"
print(f"2. _signal_width_hz: hump {w/1000:.1f} kHz, noise refused: ok")

# --- 3. fit_bw_hz ---
apt = scan.fit_bw_hz(34000)          # APT-like: ~34 kHz occupied
assert 21000 <= apt <= 24000, apt     # ... maps to the ~22 kHz demod default
assert scan.fit_bw_hz(200) == 1000    # clamp at the bottom
assert scan.fit_bw_hz(400000) == 120000  # clamp at the top (full band)
print(f"3. fit_bw_hz: 34 kHz signal -> {apt/1000:g} kHz cutoff, clamps hold: ok")

# --- 4. scan thread end-to-end (fake dongle entry + row feeder) ---
# Speed the dwell windows up: wrap _sample with near-zero timings
_orig_sample = scan._sample
def _fast_sample(entry, sc, settle=None, dwell=None):
    return _orig_sample(entry, sc, settle=0.0, dwell=0.03)
scan._sample = _fast_sample
scan.SCAN_SAMPLE_EVERY = 0.002

SERIAL = "SCANTEST"
saved = (dict(state.manual_dongle_freq), dict(state.manual_dongle_bw), dict(state.scans))

def make_entry():
    return {"lock": threading.Lock(), "last_mag": None, "primary": False}

def feed(entry, rows, every=0.004):
    """Publish fresh magnitude rows (cycling through `rows`) until stopped."""
    stop = threading.Event()
    def run():
        k = 0
        while not stop.is_set():
            with entry["lock"]:
                entry["last_mag"] = rows[k % len(rows)].copy()
            k += 1
            time.sleep(every)
        return
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return stop

def wait_done(sc, timeout=15):
    deadline = time.time() + timeout
    while sc.get("active") and time.time() < deadline:
        time.sleep(0.02)
    assert not sc.get("active"), "scan thread did not finish"

# 4a. nothing found: noise only -> override cleared, result 'nothing'
state.sdrs[SERIAL] = entry = make_entry()
stop = feed(entry, [noise_row(10), noise_row(11)])
sc = scan.start_scan(SERIAL, 100.0, 100.6, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "nothing", sc["result"]
assert SERIAL not in state.manual_dongle_freq, "override must be cleared when nothing is found"
snap = scan.scan_snapshot(SERIAL)
assert snap["result"] == "nothing" and snap["found_mhz"] is None
print("4a. noise sweep -> 'nothing', override restored: ok")

# 4b. found: a -50 kHz-offset carrier on the FIRST step -> the scan must
# re-center on the measured peak, park there and fit the recorded BW
state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
stop = feed(entry, [signal_row(20, -50000), signal_row(21, -50000)])
sc = scan.start_scan(SERIAL, 100.0, 100.6, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "found", sc["result"]
assert abs(sc["found_hz"] - 99_950_000) <= 2000, sc["found_hz"]   # re-centered on the peak (bin accuracy)
assert abs(state.manual_dongle_freq[SERIAL] - 99_950_000) <= 2000, "dongle not parked on the find"
assert sc["found_ratio"] >= 3.0
bw = state.manual_dongle_bw.get(SERIAL)
assert bw and 14000 <= bw <= 24000, f"fitted bandwidth off: {bw}"
snap = scan.scan_snapshot(SERIAL)
assert abs(snap["found_mhz"] - 99.95) < 0.005 and snap["bw_khz"] == round(bw / 1000, 1)
assert snap["active"] is False
print(f"4b. found + re-centered + parked at 99.95 MHz, BW fitted to {bw/1000:g} kHz: ok")

# 4c. stop request: parks at the current frequency
state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
state.manual_dongle_bw.clear()
stop = feed(entry, [noise_row(30), noise_row(31)])
sc = scan.start_scan(SERIAL, 100.0, 102.0, 100)
time.sleep(0.15)   # let it make a few steps
assert scan.stop_scan(SERIAL) is True
wait_done(sc)
stop.set()
assert sc["result"] == "stopped", sc["result"]
assert state.manual_dongle_freq.get(SERIAL) == sc["cur_hz"], "must stay parked after Stop"
print("4c. stop request -> parked at the current frequency: ok")

# restore global state
state.manual_dongle_freq.clear(); state.manual_dongle_bw.clear(); state.scans.clear()
state.manual_dongle_freq.update(saved[0]); state.manual_dongle_bw.update(saved[1])
state.scans.update(saved[2])
del state.sdrs[SERIAL]
scan._sample = _orig_sample
print("all scan tests passed")
