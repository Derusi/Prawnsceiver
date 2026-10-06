"""Shared mutable state, locks, and ring buffers.

Other modules access this state as `state.foo` (attribute access on this
module object), which avoids `global` declarations entirely.
"""
import threading
from collections import deque
from datetime import datetime

from .config import WATERFALL_ROWS

# State
waterfall_buffer = deque(maxlen=WATERFALL_ROWS)
waterfall_lock = threading.Lock()
# Per-dongle state, keyed by dongle SERIAL (USB indices are not stable across
# replugs). Filled by radio.enumerate_dongles. The primary dongle's entry
# aliases the legacy globals above; the others get their own buffers.
# All dongles tune the same frequency.
sdrs = {}
# Serial of the primary dongle (live audio, WAV recording), decided once at
# startup from config.PRIMARY_DONGLE_SN.
primary_serial = None
signal_strength = 0.0
pass_signal_peak = 0.0
signal_lock = threading.Lock()
signal_history = deque(maxlen=300)  # 5 min at 1 Hz

# Live audio (FM demodulated): every dongle entry owns a ring
# {'data': [...], 'base': int, 'total': int, 'cond': Condition} created by
# radio.enumerate_dongles, fed by its capture thread and served by
# /live.wav?d=<serial>.
LIVE_AUDIO_CHUNKS = 2048  # ~4s of live audio retained

def push_live_audio(entry, pcm):
    """Append a demodulated PCM block to a dongle entry's live audio ring."""
    ring = entry['la']
    with ring['cond']:
        ring['data'].append(pcm)
        ring['total'] += 1
        if len(ring['data']) > LIVE_AUDIO_CHUNKS:
            drop = len(ring['data']) - LIVE_AUDIO_CHUNKS
            del ring['data'][:drop]
            ring['base'] += drop
        ring['cond'].notify_all()

# Debug console ring buffer (served at /console)
console_buffer = deque(maxlen=100)
console_lock = threading.Lock()

def log_console(msg, level="info"):
    """Log a message to the /console debug page (and stdout)."""
    line = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "msg": str(msg),
    }
    with console_lock:
        console_buffer.append(line)
    line_str = f"[{line['time']}] [{level}] {msg}"
    try:
        print(line_str)
    except UnicodeEncodeError:
        # Console can't render the message (e.g. emoji on a non-UTF-8 terminal)
        print(line_str.encode('ascii', 'backslashreplace').decode('ascii'))
rtl_sdr_proc = None
# Manual tune (FM radio test): while set, the scheduler must not retune or
# record — the operator controls the frequency from the dashboard.
manual_frequency = None
# Per-dongle manual frequency override (serial -> Hz): a dongle with an
# entry tunes there instead of the shared current_frequency (e.g. to compare
# receive quality at a slightly different center). Cleared per dongle.
manual_dongle_freq = {}
current_frequency = 137620000
current_sat_name = "NOAA 15 (idle)"
is_recording = False
is_pass_active = False
current_wav = None
current_wav_path = None
upcoming_passes = []
current_pass = None
last_tle_refresh = 0
# TLE fetch progress, exposed via status.json for the dashboard progress bar
tle_progress = {"active": False, "done": 0, "total": 0, "current": None}
status_lock = threading.Lock()
