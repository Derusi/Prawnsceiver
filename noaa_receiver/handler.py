"""HTTP API and dashboard handler."""
import http.server
import json
import os
import struct
from datetime import datetime, timedelta, timezone

from . import state
from .config import AUDIO_RATE, LAT, LON, PASS_HISTORY_FILE, RECORD_DIR, RTL_LOG, UTC_OFFSET, WEBDIR

from .decode import decode_recording
from .history import get_recordings
from .pages import CONSOLE_HTML, HISTORY_HTML
from .passes import HAS_SKYFIELD, load, passes_to_json, wgs84

def get_status():
    with state.status_lock:
        freq = state.current_frequency
        sat = state.current_sat_name
        passing = state.is_pass_active
        passes = state.upcoming_passes
        cur_pass = state.current_pass
        tle = dict(state.tle_progress)
    
    status = {
        "rtl_sdr_running": state.rtl_sdr_proc is not None and state.rtl_sdr_proc.poll() is None,
        "frequency_mhz": round(freq / 1e6, 4),
        "satellite": sat,
        "pass_active": passing,
        "recording": state.is_recording,
        "recording_count": len(get_recordings()),
        "signal_strength": 0,
        "rtl_log": "",
        "next_pass": None,
        "tle": tle,
    }
    with state.signal_lock:
        status["signal_strength"] = round(state.signal_strength, 2)
    
    if cur_pass:
        status["current_pass"] = {
            "sat_name": cur_pass["sat_name"],
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
                "frequency_mhz": round(p["frequency"] / 1e6, 4),
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
        elif self.path == '/waterfall.json':
            with state.waterfall_lock:
                data = list(state.waterfall_buffer)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
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
            if os.path.exists(wav_path):
                self.send_response(200)
                self.send_header('Content-type', 'audio/wav')
                self.end_headers()
                with open(wav_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Audio not found')
        elif self.path == '/live.wav':
            state.log_console(f"🔊 Live audio listener connected ({self.client_address[0]})")
            # Endless WAV stream of the live FM-demodulated audio.
            # WAV header with a maxed-out size; browsers play it progressively.
            self.send_response(200)
            self.send_header('Content-type', 'audio/wav')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            data_size = 0xFFFFFFFF
            header = (b'RIFF' + struct.pack('<I', data_size) + b'WAVE'
                      + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, AUDIO_RATE, AUDIO_RATE * 2, 2, 16)
                      + b'data' + struct.pack('<I', data_size))
            try:
                self.wfile.write(header)
                with state.live_audio_cond:
                    pos = state.live_audio_total  # start at the live edge for low latency
                while True:
                    data = None
                    with state.live_audio_cond:
                        if pos >= state.live_audio_total:
                            state.live_audio_cond.wait(timeout=5)
                        if pos >= state.live_audio_total:
                            # No new audio (SDR idle/restarting): send 100 ms of
                            # silence to keep the connection alive and notice a
                            # disconnected client on write
                            data = b'\x00\x00' * (AUDIO_RATE // 10)
                        else:
                            if pos < state.live_audio_base:
                                pos = state.live_audio_base  # fell behind, skip dropped chunks
                            data = b''.join(state.live_audio_data[pos - state.live_audio_base:])
                            pos = state.live_audio_total
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # listener disconnected
            state.log_console(f"🔊 Live audio listener disconnected ({self.client_address[0]})")
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
            if decoded:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "png": filename.replace('.wav', '.png'),
                }).encode())
            else:
                state.log_console(f"Decode failed for {filename}: {err}", "error")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": err
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
        else:
            super().do_GET()
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEBDIR, **kwargs)
    def log_message(self, *args): pass
