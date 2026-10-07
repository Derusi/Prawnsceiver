"""Recording decoding: noaa-apt for NOAA APT, sstv for ISS Robot 36."""
import os
import re
import shutil
import subprocess

from . import state
from .config import NOAA_APT_DIR, NOAA_APT_TIMEOUT_SECS, NOAA_APT_TLE_FILE, SAT_DSB_FREQ, TRACKED_SATS

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


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def decode_recording(wav_path):
    """Decode a pass recording to a PNG next to the WAV.

    The decoder is chosen by satellite, detected from the recording filename.

    Returns (success, png_path, error_message).
    """
    output_png = _png_path(wav_path)
    sat = satellite_from_filename(wav_path)
    if sat is None:
        state.log_console(f"Decode: no tracked satellite in the name of {os.path.basename(wav_path)} — trying noaa-apt without map overlay", "warn")
    elif sat.startswith('ISS'):
        return _decode_sstv(wav_path, output_png)
    elif sat.startswith('Meteor'):
        # LRPT is a ~72 kHz wide digital mode — noaa-apt would burn CPU for
        # minutes on it and always fail. Recordings are kept for the pass
        # history and waterfall analysis until an LRPT decoder is added.
        return False, None, 'LRPT (Meteor-M) is digital — not decodable by the APT pipeline'
    elif sat in _DSB_RECEIVE_SATS:
        # APT transmitter off: this satellite's passes are recorded on its
        # DSB downlink (instrument telemetry), not on the APT image band
        return False, None, 'DSB (instrument telemetry) recording — not decodable by the APT pipeline'
    return _decode_apt(wav_path, output_png, sat)


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
