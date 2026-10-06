"""DSP chain: offset rotation, low-pass filtering, FM discrimination."""
from .config import SDR_RATE

def iq_to_complex(iq_bytes):
    """Convert raw 8-bit IQ bytes to a complex64 baseband array."""
    import numpy as np
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32) - 127.5
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)

# State shared by a (single-threaded) DSP chain: rotator sample counter and
# FIR filter tails, continuous across blocks. Each capture thread owns its
# own state dict (dsp.new_state()) — a shared one would corrupt the
# demodulation once several dongles demodulate concurrently.
def new_state():
    return {"rot": 0, "fir_tail": None, "afir_tail": None, "last_c": None, "audio_pos": 0}

demod_state = new_state()  # default for single-stream callers

_phasor_luts = {}

def frequency_shift(c, offset_hz, fs, st=None):
    st = st if st is not None else demod_state
    """Rotate baseband so a signal at -offset_hz moves to 0 Hz.

    The dongle is tuned offset_hz ABOVE the wanted frequency, so the signal
    arrives at -offset_hz and the DC spike at 0; this rotation centers the
    signal and displaces the DC spike to +offset_hz. Phase is continuous
    across calls via st["rot"]. The phasor is periodic in
    fs/gcd(offset_hz, fs) samples (4 for 60 kHz at 240 kHz), so a tiny LUT
    replaces computing exp() for every sample — this runs per IQ block.
    """
    import numpy as np
    if offset_hz == 0:
        return c
    key = (int(offset_hz), int(fs))
    lut = _phasor_luts.get(key)
    if lut is None:
        from math import gcd
        period = int(fs // gcd(int(offset_hz), int(fs)))
        lut = np.exp(2j * np.pi * offset_hz / fs * np.arange(period)).astype(np.complex64)
        _phasor_luts[key] = lut
    period = len(lut)
    n = len(c)
    idx = (np.arange(n) + st["rot"]) % period
    st["rot"] = int((st["rot"] + n) % period)
    return (c * lut[idx]).astype(np.complex64)

_lowpass_taps = {}

def get_lowpass_taps(cutoff_hz=22000.0):
    """Windowed-sinc low-pass FIR taps, cached per cutoff (~22 kHz for the
    APT channel; ~95 kHz for broadcast FM test tunes)."""
    key = int(cutoff_hz)
    if key not in _lowpass_taps:
        import numpy as np
        numtaps = 25
        m = np.arange(numtaps) - (numtaps - 1) / 2.0
        h = np.sinc(2 * cutoff_hz / SDR_RATE * m) * np.hamming(numtaps)
        _lowpass_taps[key] = (h / h.sum()).astype(np.float32)
    return _lowpass_taps[key]

def lowpass(c, cutoff_hz=22000.0, st=None):
    st = st if st is not None else demod_state
    """Low-pass filter with state carried across blocks (no boundary artifacts)."""
    import numpy as np
    taps = get_lowpass_taps(cutoff_hz)
    tail = st["fir_tail"]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.complex64)
    x = np.concatenate([tail, c])
    out = np.convolve(x, taps.astype(np.complex64), mode='valid')
    st["fir_tail"] = x[-(len(taps) - 1):].copy()
    return out.astype(np.complex64)

_real_taps = {}

def get_real_taps(cutoff_hz):
    """Real (audio) low-pass FIR taps, cached per cutoff."""
    key = int(cutoff_hz)
    if key not in _real_taps:
        import numpy as np
        numtaps = 25
        m = np.arange(numtaps) - (numtaps - 1) / 2.0
        h = np.sinc(2 * cutoff_hz / SDR_RATE * m) * np.hamming(numtaps)
        _real_taps[key] = (h / h.sum()).astype(np.float32)
    return _real_taps[key]

def lowpass_audio(a, cutoff_hz, st=None):
    st = st if st is not None else demod_state
    """Low-pass the real discriminator output before decimation (anti-alias)."""
    import numpy as np
    taps = get_real_taps(cutoff_hz)
    tail = st["afir_tail"]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.float32)
    x = np.concatenate([tail, a.astype(np.float32)])
    out = np.convolve(x, taps, mode='valid')
    st["afir_tail"] = x[-(len(taps) - 1):].copy()
    return out

def fm_demodulate(c, decimation=5, iq_cutoff_hz=22000.0, audio_cutoff_hz=None, st=None):
    st = st if st is not None else demod_state
    """FM discriminate a baseband-centered complex signal, decimate to audio rate.

    iq_cutoff_hz: IQ low-pass before discrimination (default 22 kHz APT
    channel). None skips it — broadcast FM tunes use the whole ±120 kHz
    capture band, because a short FIR cannot shape a clean ~95 kHz passband
    and the ±75 kHz deviation must stay inside. audio_cutoff_hz: anti-alias
    low-pass on the discriminator output before decimation to 48 kHz.
    """
    import numpy as np
    if iq_cutoff_hz:
        c = lowpass(c, iq_cutoff_hz, st)
    # Discriminator: phase advance of c[n] relative to c[n-1]. The conjugate
    # product + arctan2 yields the advance in (-pi, pi] directly — unlike
    # diff(arctan2(phase)), which produces 2*pi spikes whenever the wrapped
    # phase crosses the branch cut (any FM whose phase excursion exceeds pi,
    # i.e. also the APT signal). The previous block's last sample is carried
    # in demod_state so block processing matches whole-signal processing.
    if len(c) == 0:
        return b''
    prev = st["last_c"]
    if prev is None:
        prev = c[0]
    dd = c * np.conj(np.concatenate([np.array([prev], dtype=c.dtype), c[:-1]]))
    st["last_c"] = c[-1].copy()
    audio = np.arctan2(dd.imag, dd.real)
    if audio_cutoff_hz:
        audio = lowpass_audio(audio, audio_cutoff_hz, st)
    if decimation > 1:
        # Decimate on a continuous phase across blocks — a per-block [::n]
        # reset would drop samples at every block boundary (0.2% rate error
        # at IQ_BLOCK size) and make block processing differ from a
        # continuous stream.
        keep = (np.arange(len(audio)) + st["audio_pos"]) % decimation == 0
        st["audio_pos"] += len(audio)
        audio = audio[keep]
    audio = (audio * 32767 / (np.pi + 1e-9)).astype(np.int16)
    return audio.tobytes()
