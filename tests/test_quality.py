"""quality.py: reception scoring against synthetic recordings.

Generates a good APT recording (2400 Hz AM subcarrier carrying the
channel-A sync frame every row), a good Robot 36 SSTV recording, and pure
noise — the scorer must separate them. This is the regression test for the
"estimate_quality returns 0% for everything" bug.

Run: python3 tests/test_quality.py   (needs numpy)
"""
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from noaa_receiver.decoding import quality   # noqa: E402

RATE = 48000


def write_wav(path, x):
    data = (np.clip(x, -1.0, 1.0) * 32767).astype('<i2').tobytes()
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)


def synth_apt(secs=30.0, row_px=quality.APT_ROW_PX, noise=0.01, drop_rows=0):
    """Good NOAA APT: AM 2400 Hz subcarrier, sync frame at each row start."""
    px = quality.APT_RATE
    n = int(secs * RATE)
    t = np.arange(n, dtype='float64') / RATE
    p = (t * px).astype('int64') % row_px          # pixel index within row
    tpl = np.array(quality._apt_sync_template(), dtype='float64')
    env = np.full(row_px, 0.35)                     # picture-ish content
    env[:len(tpl)] = tpl                            # channel-A sync frame
    e = env[p]
    if drop_rows:                                   # corrupt N rows' sync
        row_idx = (t * px // row_px).astype('int64')
        bad = row_idx % 25 < drop_rows
        e = np.where(bad, np.random.default_rng(0).uniform(-0.4, 0.4, n), e)
    x = (0.5 + 0.45 * e) * np.cos(2 * np.pi * quality.APT_CARRIER * t)
    x += np.random.default_rng(1).normal(0, noise, n)
    return x


def synth_robot36(images=1, noise=0.008):
    """Good Robot 36: calibration header + VIS, then images with per-line
    1200 Hz sync pulses (simplified picture content: 1500 Hz)."""
    rng = np.random.default_rng(2)
    segs = []
    for img in range(images):
        # 1900 Hz leader (300 ms), 1200 Hz break (10 ms), 1900 (300 ms),
        # 1200 Hz VIS (30 ms) — the calibration header the scorer looks for
        for f, ms in ((1900.0, 300), (1200.0, 10), (1900.0, 300), (1200.0, 30)):
            n = int(RATE * ms / 1000)
            segs.append(np.full(n, f, dtype='float64'))
        for _ in range(quality.R36_LINES):
            n_sync = int(RATE * 0.009)               # 9 ms sync pulse
            n_scan = int(RATE * quality.R36_LINE_S) - n_sync
            segs.append(np.concatenate([
                np.full(n_sync, 1200.0),
                rng.uniform(1450, 1550, n_scan)]))   # picture tones ~1500 Hz
    freqs = np.concatenate(segs)
    n = len(freqs)
    t = np.arange(n, dtype='float64') / RATE
    x = 0.8 * np.sin(2 * np.pi * freqs * t)
    x += rng.normal(0, noise, n)
    return x


def synth_noise(secs=20.0):
    return np.random.default_rng(3).normal(0, 0.1, int(secs * RATE))


d = tempfile.mkdtemp()

# --- APT: good recording must score high ---
apt = os.path.join(d, 'NOAA_15_20261008_072300.wav')
write_wav(apt, synth_apt())
q = quality.estimate_quality(apt)
print('APT good:', q)
assert q is not None and q >= 70, q

# Partially corrupted sync must score in between
apt_bad = os.path.join(d, 'NOAA_18_20261008_072300.wav')
write_wav(apt_bad, synth_apt(drop_rows=8))
q_bad = quality.estimate_quality(apt_bad)
print('APT 2/5 rows desynced:', q_bad)
assert q_bad is not None and 10 <= q_bad < q, q_bad

# Pure noise must not score
noise_wav = os.path.join(d, 'NOAA_19_20261008_072300.wav')
write_wav(noise_wav, synth_noise())
q_noise = quality.estimate_quality(noise_wav)
print('APT noise:', q_noise)
assert not q_noise or q_noise <= 5, q_noise

# --- SSTV: good Robot 36 must score high ---
sstv = os.path.join(d, 'ISS_(Zarya)_20261008_084500.wav')
write_wav(sstv, synth_robot36())
q = quality.estimate_quality(sstv)
print('SSTV good:', q)
assert q is not None and q >= 70, q

# Routing must be by exact satellite name, not a substring: a sentinel
# SSTV scorer must NOT be consulted for a 'swiss_sat' recording, but must
# be for an ISS one
sentinel = object()
orig = quality._sstv_quality
quality._sstv_quality = lambda np, x, rate: sentinel
try:
    other = os.path.join(d, 'swiss_sat_20261008_084500.wav')
    write_wav(other, synth_robot36())
    q_other = quality.estimate_quality(other)
    assert q_other is not sentinel, 'iss substring routing bug'
    q_iss = quality.estimate_quality(sstv)
    assert q_iss is sentinel, 'ISS recording not routed to the SSTV scorer'
finally:
    quality._sstv_quality = orig

print('quality scoring OK')
