"""DSP chain: offset rotation, low-pass filtering, FM discrimination."""
from .config import SDR_RATE

def iq_to_complex(iq_bytes):
    """Convert raw 8-bit IQ bytes to a complex64 baseband array."""
    import numpy as np
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32) - 127.5
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)

# State shared by the (single-threaded) DSP chain: rotator sample counter
# and FIR filter tail, both continuous across blocks
demod_state = {"rot": 0, "fir_tail": None}

def frequency_shift(c, offset_hz, fs):
    """Rotate baseband so a signal at -offset_hz moves to 0 Hz.

    The dongle is tuned offset_hz ABOVE the wanted frequency, so the signal
    arrives at -offset_hz and the DC spike at 0; this rotation centers the
    signal and displaces the DC spike to +offset_hz. Phase is continuous
    across calls via demod_state["rot"].
    """
    import numpy as np
    from math import gcd
    if offset_hz == 0:
        return c
    period = int(fs // gcd(int(offset_hz), int(fs)))
    n = len(c)
    idx = (np.arange(n) + demod_state["rot"]) % period
    demod_state["rot"] = int((demod_state["rot"] + n) % period)
    w = 2.0 * np.pi * offset_hz / fs
    return (c * np.exp(1j * w * idx)).astype(np.complex64)

_lowpass_taps = None

def get_lowpass_taps():
    """Windowed-sinc low-pass FIR (~22 kHz) for the APT channel."""
    global _lowpass_taps
    if _lowpass_taps is None:
        import numpy as np
        numtaps = 25
        cutoff_hz = 22000.0
        m = np.arange(numtaps) - (numtaps - 1) / 2.0
        h = np.sinc(2 * cutoff_hz / SDR_RATE * m) * np.hamming(numtaps)
        _lowpass_taps = (h / h.sum()).astype(np.float32)
    return _lowpass_taps

def lowpass(c):
    """Low-pass filter with state carried across blocks (no boundary artifacts)."""
    import numpy as np
    taps = get_lowpass_taps()
    tail = demod_state["fir_tail"]
    if tail is None:
        tail = np.zeros(len(taps) - 1, dtype=np.complex64)
    x = np.concatenate([tail, c])
    out = np.convolve(x, taps.astype(np.complex64), mode='valid')
    demod_state["fir_tail"] = x[-(len(taps) - 1):].copy()
    return out.astype(np.complex64)

def fm_demodulate(c, decimation=5):
    """FM discriminate a baseband-centered complex signal, decimate to audio rate."""
    import numpy as np
    c = lowpass(c)
    phase = np.arctan2(c.imag, c.real)
    audio = np.diff(phase)
    if decimation > 1:
        audio = audio[::decimation]
    audio = (audio * 32767 / (np.pi + 1e-9)).astype(np.int16)
    return audio.tobytes()
