"""Recording decoding: noaa-apt for NOAA APT, sstv for ISS Robot 36."""
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import state

from .config import (NOAA_APT_DIR, NOAA_APT_TIMEOUT_SECS, NOAA_APT_TLE_FILE,
                     SAT_DSB_FREQ, SDR_OFFSET_HZ, SDR_RATE, TLE_CACHE_FILE,
                     TRACKED_SATS)
from .dsp import frequency_shift, iq_to_complex, new_state

# Recording names are '<sat>_<YYYYMMDD>_<HHMMSS>[-<n>][_<serial>].wav', written by
# radio.sdr_capture_thread via sat_short_name(). The satellite part is
# matched lazily up to the first timestamp, so names with underscores work.
_RECORDING_RE = re.compile(r'^(?P<sat>.+?)_(?P<ts>\d{8}_\d{6})(?:-\d+)?(?:_(?P<serial>.+))?\.wav$', re.I)

# noaa-apt satellite ids (map overlay / false color need the right orbit)
_NOAA_APT_SATS = {"NOAA 15": "noaa_15", "NOAA 18": "noaa_18", "NOAA 19": "noaa_19"}

# Satellites received via DSB (APT transmitter off): their recordings hold
# the DSB instrument-data stream, which noaa-apt cannot decode
_DSB_RECEIVE_SATS = {name for catnr, (name, _f) in TRACKED_SATS.items()
                     if catnr in SAT_DSB_FREQ}


def sat_short_name(sat_name):
    """Filename-safe satellite name used in recording names."""
    return sat_name.replace(" ", "_").replace("(idle)", "idle")


def satellite_from_filename(wav_path):
    """The tracked satellite a recording belongs to (canonical name from
    TRACKED_SATS), or None if the filename does not name one.

    Exact match on the name part — a substring test ('iss' in name) would
    misroute any satellite whose name merely contains it.
    """
    m = _RECORDING_RE.match(os.path.basename(wav_path))
    if not m:
        return None
    short = m.group('sat').lower()
    if short.endswith('_idle'):
        short = short[:-len('_idle')]
    for name, _freq in TRACKED_SATS.values():
        if sat_short_name(name).lower() == short:
            return name
    return None


def _png_path(wav_path):
    base, _ = os.path.splitext(wav_path)
    return base + '.png'

# ---------- decode-attempt markers ----------
# The dashboard auto-decodes every undecoded recording on every history-page
# load, and a decode that can never succeed (dark transmitter, ISS outside
# an ARISS event, missing raw IQ) was re-run each time — loading big WAVs
# into this process until it OOMed (2.4 GB, killed by the kernel once).
# Each attempt's outcome is therefore persisted next to the recording and
# short-circuits every later attempt unless force=True (Retry button).

def _marker_path(wav_path):
    base, _ = os.path.splitext(wav_path)
    return base + '.decode.json'

def read_decode_marker(wav_path):
    """The recorded outcome of a previous decode attempt, or None."""
    try:
        with open(_marker_path(wav_path), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def _write_decode_marker(wav_path, success, message):
    try:
        with open(_marker_path(wav_path), 'w', encoding='utf-8') as f:
            json.dump({'success': bool(success), 'message': str(message),
                       'ts': time.time()}, f)
    except OSError:
        pass

def _satellite_freq(sat_name):
    """The receive frequency used for a satellite's passes (its DSB
    downlink when we receive DSB for it)."""
    for catnr, (name, freq) in TRACKED_SATS.items():
        if name == sat_name:
            return SAT_DSB_FREQ.get(catnr, freq)
    return None


def _serial_from_filename(wav_path):
    """The dongle serial suffix of a recording name, or None (primary)."""
    m = _RECORDING_RE.match(os.path.basename(wav_path))
    return m.group('serial') if m else None


def _centered_cf32(iq_path, shift_hz):
    """Rewrite the raw u8 IQ as cf32 with the satellite rotated to 0 Hz.

    The .iq.u8 files hold the RAW rtl_tcp stream: the dongle is tuned
    SDR_OFFSET_HZ + correction above the target, so the satellite sits at
    -(SDR_OFFSET_HZ + correction) Hz in the file (plus Doppler). SatDump
    baseband pipelines demodulate around 0 Hz — decoding the raw file
    yields no lock at all (verified live on the 04:29 80.8-degree pass:
    clear +8 dB / 72 kHz LRPT plateau in the FFT, zero products from
    the raw decode). The rotation is chunked (phase-continuous via the
    DSP state) so memory stays bounded; residual Doppler (a few kHz,
    slowly varying) is left to the demod's frequency tracking.

    Returns the temp path; the caller removes it."""
    import numpy as np
    tmp = iq_path + '.centered.c32'
    st = new_state()
    chunk = SDR_RATE * 10
    with open(iq_path, 'rb') as fin, open(tmp, 'wb') as fout:
        while True:
            raw = fin.read(chunk * 2)
            if not raw or len(raw) < 4096:
                break
            c = iq_to_complex(raw)
            c = frequency_shift(c, shift_hz, SDR_RATE, st)
            fout.write(c.astype(np.complex64).view(np.float32).tobytes())
    return tmp


def _measure_signal_offset(iq_path, signal_bw_hz=50_000):
    """Where the satellite actually sits in a raw IQ capture (Hz from
    baseband center), measured from the recording itself.

    Static models are not trustworthy here: the dongle's tuning error is
    only approximated by the calibration ppm, transmitters can be off
    their nominal frequency (measured live: Meteor-M 2-4's LRPT sits
    ~13 kHz below the configured 137.9125 MHz), and Doppler adds a few
    kHz. So the decode centering measures the signal's spectral centroid
    instead of trusting any of that.

    The search is restricted to the negative-frequency side: the tuner
    is always SDR_OFFSET_HZ above the satellite (and Doppler plus tuning
    error stay far below that), so the satellite MUST sit below center —
    this excludes the +SDR_OFFSET_HZ DC spike and the R820T spurs that
    otherwise win a naive peak search (a spur at +24 kHz and the band-edge
    noise rise at -110 kHz each fooled an earlier version). A
    signal-width-matched sliding window (72 kHz for LRPT, ~6 kHz for DSB)
    finds where the mean power peaks — wide plateaus win over narrow
    spurs, and spurs cannot drag a centroid through the tuner's
    edge-noise rise.

    Returns None when nothing stands out."""
    import numpy as np
    n_fft = 4096
    bin_hz = SDR_RATE / n_fft
    # window centers to try (satellite side only)
    center_lo, center_hi = -105_000, -25_000
    best = None
    with open(iq_path, 'rb') as f:
        size = os.path.getsize(iq_path)
        span = max(0, size // 2 - SDR_RATE * 8)   # bytes; stay inside
        for t in range(5):
            f.seek(int(span * t / 5) if span else 0)
            raw = f.read(SDR_RATE * 4 * 2)
            if len(raw) < SDR_RATE * 2 * 2:
                break
            c = iq_to_complex(raw)
            segs = c[:len(c) // n_fft * n_fft].reshape(-1, n_fft)
            psd = np.zeros(n_fft)
            for s_ in segs:
                psd += np.abs(np.fft.fftshift(np.fft.fft(s_))) ** 2
            psd /= len(segs)
            freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / SDR_RATE))
            med = float(np.median(psd))
            sm = np.convolve(psd, np.ones(5) / 5, 'same')
            half = max(1, int(signal_bw_hz / 2 / bin_hz))
            acc = np.concatenate(([0.0], np.cumsum(sm)))
            # sliding mean power per window center (integer bins;
            # fftshifted axis: index 0 = -fs/2)
            best_c, best_p = None, -1.0
            lo_bin = n_fft // 2 + int(center_lo / bin_hz)
            hi_bin = n_fft // 2 + int(center_hi / bin_hz)
            for center_bin in range(lo_bin, hi_bin):
                a, b = center_bin - half, center_bin + half
                if a < 0 or b >= len(sm):
                    continue
                mean_p = (acc[b + 1] - acc[a]) / (b - a)
                if mean_p > best_p:
                    best_p, best_c = mean_p, center_bin
            if best_c is None:
                continue
            score = best_p / (med or 1.0)
            if best is None or score > best[0]:
                best = (score, freqs[best_c])
    if best is None:
        return None
    score, offset = best
    if score < 2.0:
        return None   # nothing clearly above the noise floor
    return float(offset)


def _seed_satdump_tles():
    """Keep SatDump's TLE file fed from the receiver's own TLE cache —
    SatDump cannot fetch Celestrak from this network, and with no TLEs it
    cannot geo-reference Meteor images."""
    try:
        import json
        cache = json.load(open(TLE_CACHE_FILE))
        lines = []
        for catnr, trio in cache.items():
            if isinstance(trio, list) and len(trio) == 3:
                lines.extend(l.strip() for l in trio)
        if not lines:
            return
        path = os.path.expanduser('~/.config/satdump/satdump_tles.txt')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass   # best effort — SatDump runs without TLEs, just without maps


def _satdump_decode(pipeline, iq_path, out_dir, label, shift_hz=None):
    """Decode a raw IQ recording with SatDump (runs minutes on the Pi — the
    caller spawns this detached so the scheduler never blocks on it). The
    out_dir doubles as the run marker: existing = already decoded/decoding.

    The IQ is first rotated so the satellite lands at 0 Hz (SatDump's
    baseband pipelines expect a centered signal — see _centered_cf32).
    Note: SatDump also retries fetching TLEs from Celestrak at startup;
    while that host is unreachable each retry costs ~134 s before the
    actual demodulation even starts."""
    exe = shutil.which('satdump')
    if not exe:
        state.log_console(f"SatDump is not installed - cannot decode {label}", "warn")
        return
    os.makedirs(out_dir, exist_ok=True)
    _seed_satdump_tles()
    baseband, fmt = iq_path, 'cu8'
    tmp = None
    try:
        if shift_hz:
            state.log_console(f"Centering baseband for {label}: rotating {shift_hz} Hz")
            tmp = _centered_cf32(iq_path, shift_hz)
            baseband, fmt = tmp, 'cf32'
        # Run in an isolated network namespace when available: SatDump
        # retries its Celestrak TLE fetch, and while that host is
        # unreachable each retry blocks for the full 134 s TCP timeout
        # before the demodulation even starts (observed live). The TLEs
        # come from _seed_satdump_tles instead.
        argv = [exe, pipeline, 'baseband', baseband, out_dir,
                '--samplerate', str(SDR_RATE), '--baseband_format', fmt]
        if shutil.which('unshare'):
            argv = ['unshare', '-rn'] + argv
        cmd = argv
        state.log_console(f"🛰 SatDump decode started ({label}): {pipeline}")
        subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        state.log_console(f"SatDump decode timed out ({label})", "warn")
        return
    except Exception as e:
        state.log_console(f"SatDump decode error ({label}): {e}", "error")
        return
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    products = [f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))]
    if products:
        state.log_console(f"🛰 SatDump decode done ({label}): {len(products)} product file(s) in {os.path.basename(out_dir)}/")
    else:
        state.log_console(f"SatDump produced no products ({label}) - signal too weak or transmitter off", "warn")

def _decode_iq(wav_path, pipeline, suffix):
    """SatDump decode of the raw IQ sibling of a recording. Returns
    (started, message); spawns the actual work detached."""
    base, _ = os.path.splitext(wav_path)
    iq_path, out_dir = base + '.iq.u8', base + suffix
    label = os.path.basename(wav_path)
    if not os.path.exists(iq_path):
        return False, f'No raw IQ capture for {label} - cannot decode'
    if os.path.exists(out_dir):
        return True, 'already decoded'
    # Where the satellite sits in the raw baseband: measured from the IQ
    # itself (the dongle error is modeled, transmitters can be off their
    # nominal frequency, Doppler shifts a few kHz — see
    # _measure_signal_offset). The tuner offset is the fallback.
    # LRPT is a ~72 kHz wide QPSK stream, the DSB telemetry ~6 kHz —
    # the width makes the offset measurement robust against spurs
    measured = _measure_signal_offset(iq_path, 72_000 if 'lrpt' in pipeline else 6_000)
    # _measure_signal_offset returns the signal's POSITION in the baseband
    # (negative Hz, e.g. -71425 for a satellite 71.4 kHz below center);
    # frequency_shift rotates BY its argument (signal at f -> f + shift),
    # so centering the signal at 0 needs the NEGATIVE of the position.
    # The SDR_OFFSET_HZ fallback is already a rotation amount (+60 kHz
    # centers a signal at -60 kHz). Passing the raw position rotated the
    # signal to 2x its offset — every decode whose measurement succeeded
    # (i.e. every strong pass) demodulated empty spectrum: the whole
    # 0-byte-CADU LRPT streak and the empty DSB products.
    shift_hz = -measured if measured is not None else SDR_OFFSET_HZ
    state.log_console(f"Baseband centering for {os.path.basename(wav_path)}: "
                      f"signal {'measured at' if measured is not None else 'NOT FOUND, assuming'} "
                      f"{(measured if measured is not None else -SDR_OFFSET_HZ) / 1000:.1f} kHz, "
                      f"rotating {shift_hz / 1000:.1f} kHz")
    threading.Thread(target=_satdump_decode,
                     args=(pipeline, iq_path, out_dir, label, shift_hz),
                     daemon=True, name='satdump').start()
    return True, 'decode started in the background (SatDump)'


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def decode_recording(wav_path, force=False):
    """Decode a pass recording to a PNG next to the WAV.

    The decoder is chosen by satellite, detected from the recording
    filename. The outcome of every attempt is written to a decode marker
    (<base>.decode.json) and short-circuits later attempts — see the
    marker block above. force=True re-runs and overwrites the marker.

    Returns (success, png_path, error_message).
    """
    if not force:
        marker = read_decode_marker(wav_path)
        if marker is not None:
            if marker.get('success'):
                png = _png_path(wav_path)
                return True, (png if os.path.exists(png) else None), marker.get('message')
            return False, None, f"decode already attempted: {marker.get('message')}"
    output_png = _png_path(wav_path)
    sat = satellite_from_filename(wav_path)
    if sat is None:
        state.log_console(f"Decode: no tracked satellite in the name of {os.path.basename(wav_path)} — trying noaa-apt without map overlay", "warn")
        result = _decode_apt(wav_path, output_png, sat)
    elif sat.startswith('ISS'):
        result = _decode_sstv(wav_path, output_png)
    elif sat.startswith('Meteor'):
        # LRPT is a ~72 kHz wide QPSK digital mode — noaa-apt cannot decode
        # it, but the raw IQ capture can (SatDump meteor_m2-x_lrpt)
        started, msg = _decode_iq(wav_path, 'meteor_m2-x_lrpt', '_lrpt')
        result = (started, None, f'LRPT recording - {msg}')
    elif sat in _DSB_RECEIVE_SATS:
        # APT transmitter off: this satellite is received via its DSB
        # downlink (instrument telemetry) - decodable from the raw IQ
        started, msg = _decode_iq(wav_path, 'noaa_dsb', '_dsb')
        result = (started, None, f'DSB recording - {msg}')
    else:
        result = _decode_apt(wav_path, output_png, sat)
    _write_decode_marker(wav_path, result[0], result[2] or 'decoded')
    return result


def _decode_sstv(wav_path, output_png):
    """Decode ISS Slow-Scan TV (Robot 36 during ARISS events).

    The sstv library auto-detects the mode from the VIS header and returns
    every image in the recording; the ISS repeats images during a pass, so
    extras are saved as <base>_2.png, <base>_3.png, ...
    """
    try:
        import sstv
    except ImportError:
        return False, None, 'sstv decoder not installed (pip install sstv)'
    try:
        images = sstv.decode_from_wav(wav_path)
    except Exception as e:
        return False, None, f'SSTV decode error: {e}'
    if not images:
        return False, None, 'No SSTV transmission found in recording'
    base, _ = os.path.splitext(output_png)
    # Drop leftovers of an earlier decode so the set on disk is this one's
    _remove_quietly(output_png)
    for old in range(2, 100):
        if not os.path.exists(f'{base}_{old}.png'):
            break
        _remove_quietly(f'{base}_{old}.png')
    try:
        images[0].save(output_png)
        for i, image in enumerate(images[1:], start=2):
            image.save(f'{base}_{i}.png')
    except Exception as e:
        return False, None, f'Cannot write SSTV image: {e}'
    return True, output_png, None


def _decode_apt(wav_path, output_png, sat):
    """Decode NOAA APT weather images with noaa-apt.

    Map overlay only when the satellite is known (a wrong orbit draws a
    wrong map) and with our own fresh TLE file when the scheduler has
    written one. A stale PNG from an earlier attempt is removed first —
    "output exists" is the success signal, so it must be this run's.
    """
    sat_arg = _NOAA_APT_SATS.get(sat)
    exe = shutil.which('noaa-apt') or os.path.join(NOAA_APT_DIR, 'noaa-apt')
    cmd = [exe, wav_path, '-o', output_png, '-q', '-R', 'auto']
    if sat_arg:
        cmd += ['-m', 'yes', '-s', sat_arg]
        if os.path.exists(NOAA_APT_TLE_FILE):
            cmd += ['-T', NOAA_APT_TLE_FILE]
    else:
        cmd += ['-m', 'no']
    _remove_quietly(output_png)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=NOAA_APT_TIMEOUT_SECS,
                                cwd=NOAA_APT_DIR if os.path.isdir(NOAA_APT_DIR) else None)
    except subprocess.TimeoutExpired:
        _remove_quietly(output_png)   # a half-written image must not pass as decoded
        return False, None, f'Decode timeout ({NOAA_APT_TIMEOUT_SECS:.0f} s)'
    except Exception as e:
        return False, None, f'noaa-apt could not be run ({exe}): {e}'

    output = (result.stderr or result.stdout or '').strip()
    last_line = output.splitlines()[-1] if output else ''
    if os.path.exists(output_png) and os.path.getsize(output_png) > 0:
        if result.returncode != 0:
            state.log_console(f"noaa-apt exited {result.returncode} but wrote {os.path.basename(output_png)}: {last_line}", "warn")
        return True, output_png, None
    _remove_quietly(output_png)
    return False, None, last_line or f'noaa-apt exited {result.returncode} without an image'
