"""Numerical checks for noaa_receiver.dsp: block continuity, Doppler NCO, filters.

Run: python3 tests/test_dsp.py (needs numpy). Prints measurements; asserts the
invariants the capture chain relies on.
"""
import time
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import numpy as np
from noaa_receiver import dsp
from noaa_receiver.config import SDR_RATE, DECIMATION, SDR_OFFSET_HZ, IQ_BLOCK

fs = SDR_RATE
rng = np.random.default_rng(1)

def blocks(x, n=512):
    for i in range(0, len(x), n):
        yield x[i:i+n]

def run_chain(x, dop, blk, fm_band=False):
    st = dsp.new_state()
    out = []
    for b in blocks(x, blk):
        c = dsp.frequency_shift(b, SDR_OFFSET_HZ, fs, st)
        c = dsp.doppler_shift(c, dop, fs, st)
        if fm_band:
            a = dsp.fm_demodulate(c, DECIMATION, iq_cutoff_hz=None, audio_cutoff_hz=18000, st=st)
        else:
            a = dsp.fm_demodulate(c, DECIMATION, st=st)
        out.append(np.frombuffer(a, dtype=np.int16))
    return np.concatenate(out)

# --- 1. block processing == whole-signal processing ---
N = 512 * 400
t = np.arange(N) / fs
sig = (np.exp(2j*np.pi*(-SDR_OFFSET_HZ + 1234)*t) * np.exp(1j*3.0*np.sin(2*np.pi*2400*t))
       + 0.05*(rng.standard_normal(N) + 1j*rng.standard_normal(N))).astype(np.complex64)
whole = run_chain(sig, 777, N)
for blk in (512, 1024, 100, 7):
    b = run_chain(sig, 777, blk)
    diff = np.abs(b.astype(int) - whole.astype(int)).max()
    print(f"block={blk:5d} max|diff| vs whole = {diff} LSB")
    assert diff <= 1, "block processing must equal whole-signal processing"

# --- 2. Doppler step continuity incl. zero crossing ---
def doppler_run(steps, blk=512):
    """Pure carrier at 0 Hz after offset; doppler_shift applied with a step schedule.
    Expected demod output: per-sample phase advance = -2*pi*d/fs (no spikes)."""
    st = dsp.new_state()
    out = []
    for d, nblocks in steps:
        for _ in range(nblocks):
            k = len(out) * blk
            tt = (np.arange(blk) + k) / fs
            c = np.exp(2j*np.pi*(-SDR_OFFSET_HZ)*tt).astype(np.complex64)
            c = dsp.frequency_shift(c, SDR_OFFSET_HZ, fs, st)
            c = dsp.doppler_shift(c, d, fs, st)
            a = dsp.fm_demodulate(c, 1, iq_cutoff_hz=None, st=st)   # no decimation, no filter
            out.append(np.frombuffer(a, dtype=np.int16).astype(float))
    return np.concatenate(out) * np.pi / 32767

for label, sched in [("50->-50 (no zero)", [(50, 20), (-50, 20)]),
                     ("50->0->-50 (zero crossing)", [(50, 20), (0, 20), (-50, 20)]),
                     ("0->3000 (pass start)", [(0, 20), (3000, 20)]),
                     ("3000->0->3000 (idle gap)", [(3000, 20), (0, 20), (3000, 20)])]:
    y = doppler_run(sched)
    expect = np.concatenate([np.full(n*512, -2*np.pi*d/fs) for d, n in sched])
    err = np.abs(y - expect)[1:]   # first sample has no predecessor
    # At a Doppler step the discriminator legitimately sees the frequency
    # step itself for exactly one sample; anything beyond that is a click.
    step = max(abs(2*np.pi*(sched[i][0]-sched[i-1][0])/fs) for i in range(1, len(sched)))
    print(f"doppler {label:32s} max phase-advance error = {err.max():.4f} rad ({err.max()/np.pi*32767:.0f} LSB), step size {step:.4f} rad")
    assert err.max() <= step + 1e-3, f"phase jump (click) at a Doppler step: {label}"

# --- 3. filter responses ---
def resp(taps, f):
    w = 2*np.pi*f/fs
    n = np.arange(len(taps))
    return 20*np.log10(abs(np.sum(taps*np.exp(-1j*w*n))))
taps = dsp.get_lowpass_taps(22000)
fr = np.arange(0, 120001, 1000)
db = np.array([resp(taps, f) for f in fr])
f3 = fr[np.argmax(db < -3)]
print(f"IQ lowpass(22k) taps={len(taps)}: -3dB at ~{f3/1e3:.0f} kHz; att @24k={resp(taps,24000):.1f} dB, @40k={resp(taps,40000):.1f} dB, @60k={resp(taps,60000):.1f} dB, @100k={resp(taps,100000):.1f} dB")
ataps = dsp.get_real_taps(18000)
print(f"audio lowpass(18k) taps={len(ataps)}: att @19k={resp(ataps,19000):.1f} dB, @24k={resp(ataps,24000):.1f} dB, @38k={resp(ataps,38000):.1f} dB, @57k={resp(ataps,57000):.1f} dB")

# --- 4. per-block cost (relative) ---
st = dsp.new_state()
raw = rng.integers(0, 256, IQ_BLOCK, dtype=np.uint8).tobytes()
t0 = time.perf_counter(); n = 3000
for _ in range(n):
    c = dsp.iq_to_complex(raw)
    c = dsp.frequency_shift(c, SDR_OFFSET_HZ, fs, st)
    c = dsp.doppler_shift(c, 1234, fs, st)
    dsp.fm_demodulate(c, DECIMATION, st=st)
print(f"chain cost: {(time.perf_counter()-t0)/n*1e6:.0f} us/block (this machine)")
