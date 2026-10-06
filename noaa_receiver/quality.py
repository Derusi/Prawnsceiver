"""Reception quality estimation for pass recordings (0-100%).

The quality score answers "how good was the RF reception during this pass",
independent of whether the offline decoder produced a usable image:

- APT (NOAA): fraction of received lines with a valid channel-A sync frame
  (weighted by subcarrier SNR)
- SSTV (ISS Robot 36): fraction of image lines with a valid 1200 Hz sync
  pulse after a decoded calibration header
"""
import os
import wave

# APT timing (source: sigidwiki APT structure, same as noaa-apt)
APT_RATE = 4160          # pixels per second
APT_CARRIER = 2400.0     # AM subcarrier
APT_ROW_PX = 2080        # one image row (channel A + channel B)
APT_SYNC_PX = 38         # sync frame length in pixels (7 pulses @ 1040 Hz)

# Robot 36 timing (same values the sstv decoder uses)
R36_LINE_S = 0.150
R36_LINES = 240


def estimate_quality(wav_path):
    """Score a pass recording 0-100. Returns None if it can't be judged."""
    try:
        import numpy as np
        with wave.open(wav_path, 'rb') as w:
            rate = w.getframerate()
            frames = w.getnframes()
            raw = w.readframes(frames)
    except Exception:
        return None
    if frames < rate * 10 or frames > rate * 60 * 30:
        # Under 10 s there is nothing to judge; over 30 min is not a pass
        # recording (and would need too much memory to analyze)
        return None
    x = np.frombuffer(raw, dtype='<i2').astype('float32') / 32768.0
    if 'iss' in os.path.basename(wav_path).lower():
        return _sstv_quality(np, x, rate)
    return _apt_quality(np, x, rate)


def _apt_quality(np, x, rate):
    """APT score: line sync integrity gated by subcarrier SNR."""
    snr_db = _subcarrier_snr(np, x, rate)
    snr_score = min(max((snr_db - 6.0) / 12.0, 0.0), 1.0) if snr_db is not None else 0.0
    sync_ratio = _apt_sync_ratio(np, x, rate)
    if sync_ratio is None:
        return None
    return int(round(100 * sync_ratio * snr_score))


def _subcarrier_snr(np, x, rate):
    """Mean SNR of the 2400 Hz subcarrier vs the 5-8 kHz noise floor (dB)."""
    win = int(rate * 0.25)
    if win < 64 or len(x) < win * 2:
        return None
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    carrier_band = (freqs > 2340) & (freqs < 2460)
    noise_band = (freqs > 5200) & (freqs < 8200)
    if not carrier_band.any() or not noise_band.any():
        return None
    window = np.hanning(win)
    snrs = []
    # Sample up to 40 windows spread over the recording
    step = max(win, (len(x) - win) // 40)
    for start in range(0, len(x) - win, step):
        p = np.abs(np.fft.rfft(x[start:start + win] * window)) ** 2
        carrier = p[carrier_band].mean()
        noise = p[noise_band].mean()
        if noise > 0:
            snrs.append(10 * np.log10(carrier / noise))
        if len(snrs) >= 40:
            break
    if not snrs:
        return None
    return float(np.mean(snrs))


def _apt_envelope(np, x, rate):
    """AM-demodulate the 2400 Hz subcarrier to a 4160 Hz line-pixel stream."""
    # Mix to baseband and boxcar low-pass, in chunks to bound memory
    width = max(4, int(round(rate * 0.00025)))
    kernel = np.ones(width, dtype='float32') / width
    chunk = rate * 60
    mag = []
    for start in range(0, len(x), chunk):
        seg = x[start:start + chunk]
        n = np.arange(len(seg), dtype='float32')
        ph = 2 * np.pi * APT_CARRIER * (start + n) / rate
        lo = np.convolve(seg * np.cos(ph), kernel, 'same')
        hi = np.convolve(seg * np.sin(ph), kernel, 'same')
        mag.append(np.sqrt(lo * lo + hi * hi))
    env = np.concatenate(mag)
    # Resample the envelope to the APT pixel rate
    px_count = int(len(env) * APT_RATE / rate)
    px_idx = np.arange(px_count) * (rate / APT_RATE)
    return np.interp(px_idx, np.arange(len(env), dtype='float32'), env).astype('float32')


def _apt_sync_template():
    """Channel-A sync frame: 1040 Hz square wave, 7 pulses, 38 px total."""
    tpl = [-1.0] * 2
    for _ in range(7):
        tpl += [1.0] * 2 + [-1.0] * 2
    tpl += [-1.0] * 8
    return tpl


def _apt_sync_ratio(np, x, rate):
    """Fraction of signal-bearing rows that contain a valid sync frame."""
    sig = _apt_envelope(np, x, rate)
    if len(sig) < APT_ROW_PX * 5:
        return None

    # Cross-correlate the pixel stream against the sync template (FFT-based)
    tpl = np.array(_apt_sync_template(), dtype='float32')
    n = 1
    while n < len(sig) + len(tpl):
        n *= 2
    corr = np.fft.irfft(
        np.fft.rfft(sig, n) * np.conj(np.fft.rfft(tpl, n)), n
    )[:len(sig)]

    # Per-row correlation peak and signal power
    n_rows = len(corr) // APT_ROW_PX
    row_peak = np.zeros(n_rows)
    row_power = np.zeros(n_rows)
    for r in range(n_rows):
        s = r * APT_ROW_PX
        row_peak[r] = corr[s:s + APT_ROW_PX + len(tpl)].max()
        row_power[r] = (sig[s:s + APT_ROW_PX] ** 2).mean()

    # Only score rows that actually carry signal (pass, not pre/post-roll noise)
    power_thr = 0.15 * row_power.max()
    active = row_power > power_thr
    if active.sum() < 5:
        return None
    # Rows are synced when their correlation peak is comparable to the best
    # one: near-uniform peaks = solid sync, scattered peaks = noise.
    peaks = row_peak[active]
    sync_thr = 0.6 * peaks.max()
    synced = peaks > sync_thr
    return float(synced.sum()) / float(active.sum())


def _sstv_quality(np, x, rate):
    """Robot 36 score: sync-pulse integrity of the best image in the pass."""
    p1900, p1200, p1500 = _tone_powers(np, x, rate, (1900.0, 1200.0, 1500.0))

    # Scale-free tone classification: windows dominated by one of the
    # header/sync tones (ratios, not absolute thresholds, so picture tones
    # near 1900 Hz don't fake a leader).
    lead_hi = (p1900 > 4.0 * p1200) & (p1900 > 4.0 * p1500)
    vis_hi = (p1200 > 4.0 * p1900) & (p1200 > 4.0 * p1500)

    # Calibration header: 1900 Hz leader, 1200 Hz break, 1900 Hz leader
    # Windows are 10 ms (index = 10 ms steps)
    headers = []
    k = 0
    while k < len(p1900) - 70:
        if lead_hi[k] and lead_hi[k:k + 28].sum() >= 25:
            # leader 1 (300 ms) -> break (10 ms) -> leader 2 (300 ms) -> VIS (30 ms)
            if vis_hi[k + 30:k + 34].any() and lead_hi[k + 31:k + 59].sum() >= 25 \
                    and vis_hi[k + 60:k + 66].any():
                headers.append(k)
                k += 66
                continue
        k += 1

    if not headers:
        return 0

    # Count lines with a valid 1200 Hz sync pulse after each header
    line_ms = int(round(R36_LINE_S * 1000))
    best = 0.0
    for h in headers:
        vis_end = h + 91  # 640 ms header + 270 ms VIS, in 10 ms windows
        expected = min(R36_LINES, max(0, (len(p1200) - vis_end) // line_ms))
        if expected == 0:
            continue
        found = 0
        for line in range(expected):
            t = vis_end + line * line_ms
            if vis_hi[max(0, t - 3):t + 4].any():
                found += 1
        best = max(best, found / expected)
    return int(round(100 * best))


def _tone_powers(np, x, rate, freqs, win_ms=10.0):
    """Sliding-window tone powers (Goertzel-style via rfft bins).

    Returns one array per requested frequency, sampled every 10 ms.
    """
    from numpy.lib.stride_tricks import sliding_window_view
    win = int(round(rate * win_ms / 1000.0))
    hop = win
    if len(x) < win:
        return [np.zeros(0) for _ in freqs]
    bin_hz = rate / win
    window = np.hanning(win)
    out = [[] for _ in freqs]
    chunk = int(rate * 60)
    for start in range(0, len(x) - win, chunk):
        seg = x[start:start + chunk + win]
        sw = sliding_window_view(seg, win)[::hop] * window
        spec = np.abs(np.fft.rfft(sw, axis=1)) ** 2
        for i, f in enumerate(freqs):
            b = int(round(f / bin_hz))
            lo_b, hi_b = max(b - 1, 0), min(b + 2, spec.shape[1])
            out[i].append(spec[:, lo_b:hi_b].sum(axis=1))
    return tuple(np.concatenate(o) for o in out)
