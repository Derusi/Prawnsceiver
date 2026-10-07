"""HTTP API and dashboard handler."""
import http.server
import json
import os
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from . import state
from .config import (AUDIO_RATE, LAT, LON, MANUAL_TUNE_LOCKOUT_MINS, SAT_DSB_FREQ,
                   PASS_HISTORY_FILE, RECORD_DIR, RTL_LOG, UTC_OFFSET, WEBDIR)

from .decode import decode_recording
from .history import get_recordings, quality_map, set_recording_quality
from .pages import CONSOLE_HTML, HISTORY_HTML
from .thumbs import THUMB_SUFFIX, ensure_thumb
from .passes import HAS_SKYFIELD, load, passes_to_json, wgs84
from .satnogs import satellite_info
from .quality import estimate_quality

def _dongle_list():
    """Dongle descriptors for status/dongles.json: primary first, then stable
    display indices (USB indices are not stable, so dongles are keyed by
    serial everywhere else)."""
    keys = list(state.sdrs)
    keys.sort(key=lambda s: (s != state.primary_serial, s))
    dongles = []
    for i, sn in enumerate(keys):
        e = state.sdrs[sn]
        with e['lock']:
            dongles.append({
                "index": i,
                "id": sn,
                "label": e["label"],
                "tuner": e["tuner"],
                "serial": sn,
                "primary": e["primary"],
                "signal": round(e["signal"], 2),
                "running": e["proc"] is not None and e["proc"].poll() is None,
                "recording": e["is_recording"],
                "wav": os.path.basename(e["wav_path"]) if e["wav_path"] else None,
                "correction_hz": e.get("correction", 0),
                "correction_src": e.get("correction_src", "none"),
                "manual_frequency_mhz": round(state.manual_dongle_freq[sn] / 1e6, 4) if sn in state.manual_dongle_freq else None,
                "manual_bw_khz": round(state.manual_dongle_bw[sn] / 1000.0, 3) if sn in state.manual_dongle_bw else None,
            })
    return dongles

def get_status():
    with state.status_lock:
        freq = state.current_frequency
        sat = state.current_sat_name
        passing = state.is_pass_active
        manual = state.manual_frequency
        passes = state.upcoming_passes
        cur_pass = state.current_pass
        tle = dict(state.tle_progress)
    
    dongles = _dongle_list()
    status = {
        "rtl_sdr_running": state.rtl_sdr_proc is not None and state.rtl_sdr_proc.poll() is None,
        "frequency_mhz": round(freq / 1e6, 4),
        "doppler_hz": state.doppler_hz,
        "manual_frequency_mhz": round(manual / 1e6, 4) if manual else None,
        "dongles": dongles,
        "satellite": sat,
        "pass_active": passing,
        "recording": state.is_recording,
        "recording_count": len(get_recordings()),
        "signal_strength": 0,
        "rtl_log": "",
        "next_pass": None,
        "tle": tle,
        "tle_age_min": round((time.time() - state.last_tle_refresh) / 60, 1) if state.last_tle_refresh else None,
    }
    with state.signal_lock:
        status["signal_strength"] = round(state.signal_strength, 2)
    
    if cur_pass:
        status["current_pass"] = {
            "sat_name": cur_pass["sat_name"],
            "catnr": cur_pass["catnr"],
            "dsb": cur_pass.get("catnr") in SAT_DSB_FREQ,
            "frequency_mhz": round(cur_pass["frequency"] / 1e6, 4),
            "max_alt": cur_pass["max_alt"],
            "set_local": (cur_pass["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
        }
    else:
        status["current_pass"] = None
    
    # Find next upcoming pass
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    for p in passes:
        if p["rise_utc"] > now:
            status["next_pass"] = {
                "sat_name": p["sat_name"],
                "dsb": p.get("catnr") in SAT_DSB_FREQ,
                "frequency_mhz": round(SAT_DSB_FREQ.get(p.get("catnr"), p["frequency"]) / 1e6, 4),
                "rise_local": (p["rise_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
                "max_alt": p["max_alt"],
                "duration_min": p["duration_min"],
                "countdown_min": round((p["rise_utc"] - now).total_seconds() / 60),
            }
            break
    
    for log_file, key in [(RTL_LOG, "rtl_log")]:
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r', errors='replace') as f:
                    status[key] = ''.join(f.readlines()[-8:]).strip()
        except: pass
    return status


class NOAAHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/status.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(get_status()).encode())
        elif self.path.startswith('/waterfall.json'):
            # Optional ?d=<dongle serial> (default: primary) and last=1 for
            # the newest row only (the dashboard scrolls client-side)
            query = parse_qs(urlparse(self.path).query)
            dev = (query.get('d') or [''])[0] or (state.primary_serial or '')
            entry = state.sdrs.get(dev)
            if entry is None:
                data = []
            elif (query.get('last') or [''])[0] == '1':
                with entry['lock']:
                    # deque supports indexing but not slicing
                    data = [entry['waterfall'][-1]] if entry['waterfall'] else []
            else:
                with entry['lock']:
                    data = list(entry['waterfall'])
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path == '/dongles.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(_dongle_list()).encode())
        elif self.path == '/passes.json':
            with state.status_lock:
                passes = passes_to_json(state.upcoming_passes)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(passes).encode())
        elif self.path == '/recordings.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(get_recordings()).encode())
        elif self.path == '/signal_history.json':
            with state.signal_lock:
                history = list(state.signal_history)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(history).encode())
        elif self.path == '/pass_history.json':
            history = []
            if os.path.exists(PASS_HISTORY_FILE):
                try:
                    with open(PASS_HISTORY_FILE, 'r') as f:
                        history = json.load(f)
                except Exception:
                    pass
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(history).encode())
        elif self.path == '/track.json':
            # Polar (az/el) track of the currently active pass
            with state.status_lock:
                cur = state.current_pass
            result = {"active": False, "sat_name": None, "track": [], "now": None}
            if cur is not None:
                result["active"] = True
                result["sat_name"] = cur["sat_name"]
                if HAS_SKYFIELD and cur.get("sat") is not None:
                    try:
                        ts = load.timescale()
                        site = wgs84.latlon(LAT, LON)
                        now = datetime.utcnow().replace(tzinfo=timezone.utc)
                        start, end = cur["rise_utc"], cur["set_utc"]
                        span = (end - start).total_seconds()
                        if span > 0:
                            steps = max(int(span // 30), 1)
                            times = [start + timedelta(seconds=span * k / steps) for k in range(steps + 1)]
                            t = ts.from_datetimes(times)
                            alt, az, _ = (cur["sat"] - site).at(t).altaz()
                            result["track"] = [
                                {"t": times[i].timestamp(),
                                 "alt": round(alt.degrees[i], 1),
                                 "az": round(az.degrees[i], 1)}
                                for i in range(len(times))
                            ]
                        tn = ts.from_datetime(now)
                        altn, azn, _ = (cur["sat"] - site).at(tn).altaz()
                        result["now"] = {"t": now.timestamp(), "alt": round(altn.degrees, 1), "az": round(azn.degrees, 1)}
                    except Exception as e:
                        state.log_console(f"Track computation failed: {e}", "error")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        elif self.path == '/console.json':
            with state.console_lock:
                lines = list(state.console_buffer)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(lines).encode())
        elif self.path == '/console' or self.path == '/console.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(CONSOLE_HTML.encode())
        elif self.path == '/history' or self.path == '/history.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HISTORY_HTML.encode())
        elif self.path.startswith('/audio/'):
            filename = self.path[7:]
            if '..' in filename or '/' in filename:
                self.send_response(400)
                self.end_headers()
                return
            wav_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(wav_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Audio not found')
                return
            # Never serve a recording that is still being written: its WAV
            # header is stale the moment we read it, which makes browsers
            # play a few seconds and show a full progress bar.
            with state.status_lock:
                recording_now = state.is_recording and state.current_wav_path == wav_path
            if recording_now:
                self.send_response(409)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Recording in progress"}).encode())
                return
            size = os.path.getsize(wav_path)
            # Audio elements need Content-Length + range support to seek
            range_header = self.headers.get('Range')
            start, end = 0, size - 1
            partial = False
            if range_header and range_header.startswith('bytes='):
                try:
                    r = range_header[6:].split('-')
                    start = int(r[0]) if r[0] else 0
                    end = int(r[1]) if r[1] else size - 1
                    if start >= size or start > end:
                        raise ValueError
                    end = min(end, size - 1)
                    partial = True
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self.send_header('Content-type', 'audio/wav')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            with open(wav_path, 'rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        elif self.path.split('?')[0] == '/tune':
            # Manual tune (dongle reception test, e.g. FM broadcast radio):
            # /tune?f=89.7 parks the dongle on a frequency and pauses the
            # satellite scheduler; /tune?f=auto hands control back to it.
            query = parse_qs(urlparse(self.path).query)
            f = (query.get('f') or [''])[0].strip().lower()
            with state.status_lock:
                pass_active = state.is_pass_active
            if f in ('', 'auto'):
                with state.status_lock:
                    state.manual_frequency = None
                    # During a pass the scheduler owns the frequency: parking
                    # here would record the idle band under the pass's name
                    if not state.is_pass_active:
                        state.current_frequency = 137620000
                        state.current_sat_name = "NOAA 15 (idle)"
                state.log_console("🛰 Manual tune off — satellite tracking resumed")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "mode": "auto"}).encode())
                return
            try:
                mhz = float(f)
            except ValueError:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Invalid frequency"}).encode())
                return
            if not 24.0 <= mhz <= 1766.0:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Frequency out of R820T range (24-1766 MHz)"}).encode())
                return
            if pass_active:
                state.log_console("Tune rejected: satellite pass in progress", "warn")
                self.send_response(409)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Satellite pass in progress — try again after it ends"}).encode())
                return
            # Auto-reject near a predicted pass too: a manual tune pauses the
            # scheduler, so a tune that sticks right before a rise silently
            # skips that pass (f=auto above is never blocked)
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            lockout = MANUAL_TUNE_LOCKOUT_MINS * 60
            for p in state.upcoming_passes:
                rise = p.get("rise_utc")
                if rise is None or rise <= now:
                    continue
                if (rise - now).total_seconds() <= lockout:
                    mins = max(1, round((rise - now).total_seconds() / 60))
                    state.log_console(f"Tune rejected: next pass {p.get('sat_name')} rises in {mins} min", "warn")
                    self.send_response(409)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": f"Next pass {p.get('sat_name')} rises in {mins} min — tune again after it ends (Auto is still available)"}).encode())
                    return
                break  # passes are sorted: the first future one decides
            freq_hz = int(round(mhz * 1e6))
            with state.status_lock:
                state.manual_frequency = freq_hz
                state.current_frequency = freq_hz
                state.current_sat_name = f"Manual {mhz:.4f} MHz"
            state.log_console(f"📻 Manual tune: {mhz:.4f} MHz — satellite tracking paused")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "mode": "manual", "frequency_mhz": mhz}).encode())
        elif self.path.split('?')[0] == '/sync_tle':
            # Manual TLE refresh: sets a flag; the scheduler picks it up
            # within its 10 s tick and refetches + re-predicts. The network
            # fetch can take minutes, so it must never block this handler.
            if state.tle_sync_requested or (state.tle_progress or {}).get("active"):
                self.send_response(409)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "A TLE sync is already running"}).encode())
                return
            state.tle_sync_requested = True
            state.log_console("🔄 Manual TLE sync requested — fetching fresh elements")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "mode": "queued"}).encode())
        elif self.path.startswith('/tune_dongle'):
            # Per-dongle manual frequency override:
            # /tune_dongle?d=<serial>&f=<mhz> tunes just that dongle (e.g.
            # to compare receive quality around a signal); f=auto clears
            # the override and the dongle rejoins the shared frequency.
            query = parse_qs(urlparse(self.path).query)
            dev = (query.get('d') or [''])[0]
            f = (query.get('f') or [''])[0].strip().lower()
            bw = (query.get('bw') or [''])[0].strip().lower()
            entry = state.sdrs.get(dev)
            if entry is None:
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Unknown dongle"}).encode())
                return
            # Optional demod (recorded) bandwidth override in kHz — the IQ
            # low-pass cutoff ahead of the FM discriminator. bw=auto clears.
            # A bw-only request (no f) leaves the frequency untouched.
            if bw:
                bw_hz = None
                if bw not in ('auto', 'sync'):
                    try:
                        khz = float(bw)
                    except ValueError:
                        self.send_response(400)
                        self.send_header('Content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": False, "error": "Invalid bandwidth"}).encode())
                        return
                    if not 1.0 <= khz <= 120.0:
                        self.send_response(400)
                        self.send_header('Content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": False, "error": "Bandwidth out of range (1-120 kHz)"}).encode())
                        return
                    bw_hz = int(khz * 1000)
                with state.status_lock:
                    if bw_hz is None:
                        state.manual_dongle_bw.pop(dev, None)
                    else:
                        state.manual_dongle_bw[dev] = bw_hz
                state.log_console(f"🎚 Dongle {dev} demod bandwidth: {bw_hz / 1000:g} kHz" if bw_hz else f"🎚 Dongle {dev} demod bandwidth back to auto")
                if not f:
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "mode": "bw", "bandwidth_khz": bw_hz / 1000 if bw_hz else None}).encode())
                    return
            if f in ('', 'auto', 'sync'):
                with state.status_lock:
                    state.manual_dongle_freq.pop(dev, None)
                state.log_console(f"🎛 Dongle {dev} frequency override cleared — back to the shared frequency")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "mode": "sync"}).encode())
                return
            try:
                mhz = float(f)
            except ValueError:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Invalid frequency"}).encode())
                return
            if not 24.0 <= mhz <= 1766.0:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Frequency out of R820T range (24-1766 MHz)"}).encode())
                return
            with state.status_lock:
                state.manual_dongle_freq[dev] = int(round(mhz * 1e6))
            state.log_console(f"🎛 Dongle {dev} frequency override: {mhz:.4f} MHz")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "mode": "manual", "frequency_mhz": mhz}).encode())
        elif self.path.split('?')[0] == '/live.wav':
            # Endless WAV stream of the live FM-demodulated audio of one
            # dongle (default: the primary). WAV header with a maxed-out
            # size; browsers play it progressively.
            query = parse_qs(urlparse(self.path).query)
            dev = (query.get('d') or [''])[0] or (state.primary_serial or '')
            entry = state.sdrs.get(dev)
            if entry is None:
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Unknown dongle"}).encode())
                return
            state.log_console(f"🔊 Live audio listener connected ({self.client_address[0]}, dongle {dev})")
            self.send_response(200)
            self.send_header('Content-type', 'audio/wav')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            data_size = 0xFFFFFFFF
            header = (b'RIFF' + struct.pack('<I', data_size) + b'WAVE'
                      + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, AUDIO_RATE, AUDIO_RATE * 2, 2, 16)
                      + b'data' + struct.pack('<I', data_size))
            ring = entry['la']
            try:
                self.wfile.write(header)
                with ring['cond']:
                    pos = ring['total']  # start at the live edge for low latency
                while True:
                    data = None
                    with ring['cond']:
                        if pos >= ring['total']:
                            ring['cond'].wait(timeout=5)
                        if pos >= ring['total']:
                            # No new audio (SDR idle/restarting): send 100 ms of
                            # silence to keep the connection alive and notice a
                            # disconnected client on write
                            data = b'\x00\x00' * (AUDIO_RATE // 10)
                        else:
                            if pos < ring['base']:
                                pos = ring['base']  # fell behind, skip dropped chunks
                            data = b''.join(ring['data'][pos - ring['base']:])
                            pos = ring['total']
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # listener disconnected
            state.log_console(f"🔊 Live audio listener disconnected ({self.client_address[0]}, dongle {dev})")
            return
        elif self.path.startswith('/decode/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename:
                state.log_console(f"Decode rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(wav_path):
                state.log_console(f"Decode requested missing recording: {filename}", "warn")
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Recording not found')
                return
            output_png = wav_path.replace('.wav', '.png')
            decoded, png_path, err = decode_recording(wav_path)
            # Reception quality: reuse the pass-end value if present, otherwise
            # analyze the recording now (takes a few seconds on a Pi)
            quality = quality_map().get(filename)
            if quality is None:
                quality = estimate_quality(wav_path)
            if quality is not None:
                set_recording_quality(filename, quality)
            if decoded:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "png": filename.replace('.wav', '.png'),
                    "quality": quality
                }).encode())
            else:
                state.log_console(f"Decode failed for {filename}: {err}", "error")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": err,
                    "quality": quality
                }).encode())
        elif self.path.startswith('/delete/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename or not filename.endswith('.wav'):
                state.log_console(f"Delete rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(wav_path):
                state.log_console(f"Delete requested missing recording: {filename}", "warn")
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Recording not found"}).encode())
                return
            # Never delete the file that is currently being written
            with state.status_lock:
                recording_now = state.is_recording and state.current_wav_path == wav_path
            if recording_now:
                state.log_console(f"Delete refused, recording in progress: {filename}", "warn")
                self.send_response(409)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Recording in progress"}).encode())
                return
            deleted = []
            try:
                os.remove(wav_path)
                deleted.append(filename)
                png_path = wav_path[:-4] + '.png'
                if os.path.exists(png_path):
                    os.remove(png_path)
                    deleted.append(os.path.basename(png_path))
                thumb = png_path[:-4] + THUMB_SUFFIX
                if os.path.exists(thumb):
                    os.remove(thumb)
                    deleted.append(os.path.basename(thumb))
                state.log_console(f"🗑 Deleted: {', '.join(deleted)}")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "deleted": deleted}).encode())
            except Exception as e:
                state.log_console(f"Delete error for {filename}: {e}", "error")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
        elif self.path.startswith('/satnogs.json'):
            # SatNOGS DB metadata for the tracked satellite (proxied and
            # cached server-side — the DB has no CORS headers and the
            # dashboard would otherwise hammer it on every poll)
            query = parse_qs(urlparse(self.path).query)
            try:
                catnr = int((query.get('catnr') or ['0'])[0])
            except ValueError:
                catnr = 0
            info, err = (satellite_info(catnr) if catnr else (None, 'missing catnr'))
            self.send_response(200 if info else 404)
            self.send_header('Content-type', 'application/json')
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            self.wfile.write(json.dumps({"info": info, "error": err}).encode())
        elif self.path.startswith('/images/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            img_path = os.path.join(RECORD_DIR, filename)
            if os.path.exists(img_path):
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.end_headers()
                with open(img_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
        elif self.path.startswith('/thumbs/'):
            # Small JPEG preview of a decoded image, generated on first
            # request and cached next to the PNG (see thumbs.py). The pass
            # history page loads these instead of the ~10 MB originals.
            filename = self.path[8:]
            if '..' in filename or '/' in filename:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            if not filename.lower().endswith('.png'):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
                return
            img_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(img_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
                return
            tpath, err = ensure_thumb(img_path)
            if tpath is None:
                # Pillow missing or PNG unreadable — keep the page working
                # by falling back to the full image
                state.log_console(f"Thumbnail fallback for {filename}: {err}", "warn")
                self.send_response(302)
                self.send_header('Location', '/images/' + filename)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-type', 'image/jpeg')
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            with open(tpath, 'rb') as f:
                self.wfile.write(f.read())
        else:
            super().do_GET()
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEBDIR, **kwargs)
    def log_message(self, *args): pass
