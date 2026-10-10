import glob
"""Pass history log and recording file management."""
import json
import os
from datetime import datetime, timedelta, timezone

from .. import state
from ..config import PASS_HISTORY_FILE, RECORD_DIR, UTC_OFFSET
from ..decoding.decode import read_decode_marker

def log_pass(sat_name, frequency, max_alt, duration_min, rise_time, set_time, signal_peak, decoded, png_file, wav_file, quality=None, satnogs=None):
    """Log a completed pass (with recording metadata) to the history file.

    satnogs: the SatNOGS DB snapshot fetched during the pass (names, launch,
    status, transmitters) — stored with the pass so the history page shows
    what was received even long after the satellite decays."""
    history = _load_history()
    # One entry per physical pass: tracking of a pass can END twice
    # (overlapping same-frequency passes flipping the receiver back and
    # forth, a manual-tune interruption that re-triggers the same window).
    # The second log carries the later decode state — merge, don't append.
    rise_ts = rise_time.timestamp()
    for h in reversed(history):
        if h.get("sat_name") == sat_name and abs((h.get("rise_ts") or 0) - rise_ts) <= 120:
            h["signal_peak"] = round(max(h.get("signal_peak") or 0, signal_peak), 1)
            if decoded and not h.get("decoded"):
                h["decoded"] = True
                h["png"] = png_file or h.get("png")
            if wav_file and not h.get("wav"):
                h["wav"] = wav_file
            if quality is not None and h.get("quality") is None:
                h["quality"] = quality
            if satnogs is not None and h.get("satnogs") is None:
                h["satnogs"] = satnogs
            _save_history(history)
            return
    history.append({
        "sat_name": sat_name,
        "frequency_mhz": round(frequency / 1e6, 4),
        "max_alt": max_alt,
        "duration_min": duration_min,
        "rise_local": (rise_time + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
        "set_local": (set_time + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
        "rise_ts": rise_time.timestamp(),
        "set_ts": set_time.timestamp(),
        "signal_peak": round(signal_peak, 1),
        "decoded": decoded,
        "png": png_file,
        "wav": wav_file,
        "quality": quality,
        "satnogs": satnogs,
        "timestamp": datetime.now().isoformat(),
    })
    # Keep last 50 passes
    history = history[-50:]
    _save_history(history)

def _load_history():
    if not os.path.exists(PASS_HISTORY_FILE):
        return []
    try:
        with open(PASS_HISTORY_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def _save_history(history):
    try:
        with open(PASS_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        state.log_console(f"History write failed: {e}", "error")

def set_recording_quality(wav_name, quality):
    """Persist a reception-quality value in the matching history entry."""
    history = _load_history()
    for h in reversed(history):
        if h.get("wav") == wav_name:
            h["quality"] = quality
            _save_history(history)
            return True
    return False

def quality_map():
    """Map recording filename -> stored reception quality."""
    return {h.get("wav"): h.get("quality")
            for h in _load_history() if h.get("wav")}

def migrate_pass_history():
    """One-time migration for entries written before the metadata change:
    link each pass to its recording file and backfill set_local / timestamps."""
    if not os.path.exists(PASS_HISTORY_FILE):
        return
    try:
        with open(PASS_HISTORY_FILE, 'r') as f:
            history = json.load(f)
    except Exception:
        return
    recordings = []
    if os.path.exists(RECORD_DIR):
        recordings = [f for f in os.listdir(RECORD_DIR) if f.endswith('.wav')]
    changed = 0
    for h in history:
        # Parse rise_local whenever timestamps or the recording link are missing
        rise_local_dt = None
        needs_backfill = "rise_ts" not in h or "set_local" not in h
        needs_link = not h.get("wav")
        if needs_backfill or needs_link:
            for fmt in ("%d.%m %H:%M", "%a %d.%m %H:%M"):
                try:
                    dt = datetime.strptime(h.get("rise_local", ""), fmt)
                    entry_dt = datetime.fromisoformat(h["timestamp"])
                    rise_local_dt = dt.replace(year=entry_dt.year)
                    break
                except (ValueError, KeyError):
                    continue
            if rise_local_dt is None and "rise_ts" not in h:
                continue
        duration = h.get("duration_min") or 0
        if "rise_ts" not in h and rise_local_dt is not None:
            rise_utc = (rise_local_dt - timedelta(hours=UTC_OFFSET)).replace(tzinfo=timezone.utc)
            h["rise_ts"] = rise_utc.timestamp()
            h["set_ts"] = h["rise_ts"] + duration * 60
            changed += 1
        if "set_local" not in h and rise_local_dt is not None:
            h["set_local"] = (rise_local_dt + timedelta(minutes=duration)).strftime("%H:%M")
            changed += 1
        # Link the recording file by satellite name + pass-time proximity
        if h.get("wav"):
            continue
        if rise_local_dt is None:
            continue
        sat_short = h["sat_name"].replace(" ", "_") + "_"
        best, best_delta = None, None
        for f in recordings:
            if not f.startswith(sat_short):
                continue
            try:
                rec_dt = datetime.strptime(f[len(sat_short):-4], "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            delta = abs((rec_dt - rise_local_dt).total_seconds())
            if delta <= duration * 60 + 360 and (best_delta is None or delta < best_delta):
                best, best_delta = f, delta
        if best:
            h["wav"] = best
            changed += 1
            state.log_console(f"📜 History migration: linked {best} to {h['sat_name']} pass ({h['rise_local']})")
    if changed:
        try:
            with open(PASS_HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=2)
            state.log_console(f"📜 History migration: updated {changed} field(s) in {len(history)} entries")
        except Exception as e:
            state.log_console(f"History migration failed: {e}", "error")

def get_recordings():
    """List available recordings (with reception quality where known)."""
    qualities = quality_map()
    with state.status_lock:
        active_wav = state.current_wav_path if state.is_recording else None
    recordings = []
    if os.path.exists(RECORD_DIR):
        for f in sorted(os.listdir(RECORD_DIR), reverse=True):
            if f.endswith('.wav'):
                path = os.path.join(RECORD_DIR, f)
                size = os.path.getsize(path)
                has_png = os.path.exists(path.replace('.wav', '.png'))
                marker = read_decode_marker(path)
                recordings.append({
                    "filename": f,
                    "size_mb": round(size / (1024*1024), 1),
                    "decoded": has_png,
                    "png": f.replace('.wav', '.png') if has_png else None,
                    "quality": qualities.get(f),
                    "decode_attempted": marker is not None,
                    "decode_error": (marker.get('message')
                                     if marker is not None and not marker.get('success')
                                     else None),
                    # A wav that is still being written must not be played or
                    # decoded: its WAV header is stale and the file incomplete
                    "recording_in_progress": bool(active_wav and os.path.abspath(path) == os.path.abspath(active_wav)),
                    # Raw IQ capture next to the WAV (digital modes: LRPT/DSB
                    # are recorded as raw baseband alongside the FM audio),
                    # shown in the UI and deleted WITH the recording.
                    "iq": {"filename": f.replace('.wav', '.iq.u8'),
                           "size_mb": round(os.path.getsize(path.replace('.wav', '.iq.u8'))
                                            / (1024*1024), 1)}
                          if os.path.exists(path.replace('.wav', '.iq.u8')) else None,
                    # Every generated decode product (SatDump product
                    # directories next to the recording, e.g. <base>_apt/):
                    # the history page links them all instead of one image
                    "products": [
                        {"name": pname,
                         "size_kb": round(os.path.getsize(os.path.join(pdir, pname)) / 1024.0, 1),
                         "path": os.path.basename(pdir) + '/' + pname}
                        for pdir in sorted(glob.glob(os.path.join(RECORD_DIR, f[:-4] + '_*')))
                        if os.path.isdir(pdir)
                        for pname in sorted(os.listdir(pdir))
                        if os.path.isfile(os.path.join(pdir, pname))],
                })
    return recordings
