"""Recording decoding: noaa-apt for NOAA APT, sstv for ISS Robot 36."""
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import state
from .config import NOAA_APT_DIR, NOAA_APT_TIMEOUT_SECS, NOAA_APT_TLE_FILE, SAT_DSB_FREQ, SDR_RATE, TRACKED_SATS

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

def _satdump_decode(pipeline, iq_path, out_dir, label):
    """Decode a raw IQ recording with SatDump (runs minutes on the Pi — the
    caller spawns this detached so the scheduler never blocks on it). The
    out_dir doubles as the run marker: existing = already decoded/decoding."""
    exe = shutil.which('satdump')
    if not exe:
        state.log_console(f"SatDump is not installed - cannot decode {label}", "warn")
        return
    os.makedirs(out_dir, exist_ok=True)
    cmd = [exe, pipeline, 'baseband', iq_path, out_dir,
           '--samplerate', str(SDR_RATE), '--baseband_format', 'cu8']
    state.log_console(f"🛰 SatDump decode started ({label}): {pipeline}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        state.log_console(f"SatDump decode timed out ({label})", "warn")
        return
    except Exception as e:
        state.log_console(f"SatDump decode error ({label}): {e}", "error")
        return
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
    threading.Thread(target=_satdump_decode, args=(pipeline, iq_path, out_dir, label),
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
