#!/usr/bin/env python3
"""
NOAA Weather Satellite Receiver with Auto Pass Tracking
- Predicts NOAA 15/18/19 passes using skyfield + Celestrak TLEs
- Automatically switches frequency before each pass
- Records FM-demodulated audio to WAV only during passes
- Live waterfall always active (for monitoring noise floor between passes)
- Serves web dashboard with pass schedule, waterfall, recordings, and APT decode
"""
import glob
import http.server
import socketserver
import json
import os
import struct
import subprocess
import threading
import time
import wave
import urllib.request
from datetime import datetime, timedelta, timezone
from collections import deque

try:
    from skyfield.api import load, wgs84, EarthSatellite
    from skyfield.toposlib import wgs84 as wgs84_alt
    HAS_SKYFIELD = True
except ImportError:
    HAS_SKYFIELD = False

PORT = 8085
LOGDIR = "/var/log/noaa"
RECORD_DIR = "/var/log/noaa/recordings"
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")
RTL_LOG = os.path.join(LOGDIR, "rtl_sdr.log")
WEBDIR = "/home/eugene/aprs_website"
FFT_SIZE = 512
WATERFALL_ROWS = 120
SDR_GAIN = 35
SDR_RATE = 240000
AUDIO_RATE = 48000
IQ_BLOCK = FFT_SIZE * 2
DECIMATION = SDR_RATE // AUDIO_RATE

# Regensburg coordinates
LAT, LON = 49.013, 12.099
UTC_OFFSET = 2  # Germany UTC+2

# NOAA satellites: catalog number -> (name, frequency_hz)
NOAA_SATS = {
    25338: ("NOAA 15", 137620000),
    28654: ("NOAA 18", 137912500),
    33591: ("NOAA 19", 137100000),
}

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 30  # Start recording 30s before rise, stop 30s after set
TLE_REFRESH_HOURS = 6  # Refresh TLE data every 6h

# State
waterfall_buffer = deque(maxlen=WATERFALL_ROWS)
waterfall_lock = threading.Lock()
signal_strength = 0.0
pass_signal_peak = 0.0
signal_lock = threading.Lock()
signal_history = deque(maxlen=300)  # 5 min at 1 Hz

# Live audio stream (FM demodulated, ring buffer of demod blocks)
LIVE_AUDIO_CHUNKS = 2048  # ~4s of live audio retained
live_audio_data = []
live_audio_base = 0      # global index of live_audio_data[0]
live_audio_total = 0     # total demod blocks ever produced
live_audio_cond = threading.Condition()

def push_live_audio(pcm):
    """Append a demodulated PCM block to the live audio ring buffer."""
    global live_audio_total, live_audio_base
    with live_audio_cond:
        live_audio_data.append(pcm)
        live_audio_total += 1
        if len(live_audio_data) > LIVE_AUDIO_CHUNKS:
            drop = len(live_audio_data) - LIVE_AUDIO_CHUNKS
            del live_audio_data[:drop]
            live_audio_base += drop
        live_audio_cond.notify_all()

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
current_frequency = 137620000
current_sat_name = "NOAA 15 (idle)"
is_recording = False
is_pass_active = False
current_wav = None
current_wav_path = None
upcoming_passes = []
current_pass = None
last_tle_refresh = 0
status_lock = threading.Lock()

def fm_demod(iq_bytes, decimation=5):
    """FM demodulate raw 8-bit IQ and decimate to target audio rate."""
    import numpy as np
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32) - 127.5
    i_samples = raw[0::2]
    q_samples = raw[1::2]
    phase = np.arctan2(q_samples, i_samples)
    audio = np.diff(phase)
    if decimation > 1:
        audio = audio[::decimation]
    audio = (audio * 32767 / (np.pi + 1e-9)).astype(np.int16)
    return audio.tobytes()

def fetch_tle(catnr, ts):
    """Fetch a single satellite TLE from Celestrak."""
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"
    text = urllib.request.urlopen(url, timeout=15).read().decode()
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if len(lines) >= 3:
        return EarthSatellite(lines[1], lines[2], lines[0], ts)
    return None

def refresh_tles():
    """Refresh TLE data from Celestrak."""
    global last_tle_refresh
    if not HAS_SKYFIELD:
        log_console("Skyfield not available, cannot predict passes", "warn")
        return {}
    ts = load.timescale()
    sats = {}
    for catnr, (name, freq) in NOAA_SATS.items():
        try:
            sat = fetch_tle(catnr, ts)
            if sat:
                sats[catnr] = (sat, name, freq)
                log_console(f"TLE loaded: {name} (cat #{catnr}), epoch={sat.epoch.utc_datetime()}")
        except Exception as e:
            log_console(f"TLE fetch failed for {name} (cat #{catnr}): {e}", "error")
    last_tle_refresh = time.time()
    return sats

def predict_passes(sats, hours=24):
    """Predict satellite passes over the next `hours` hours."""
    if not sats or not HAS_SKYFIELD:
        return []
    ts = load.timescale()
    site = wgs84.latlon(LAT, LON)
    now = ts.now()
    end = ts.tt_jd(now.tt + hours / 24.0)
    
    passes = []
    for catnr, (sat, name, freq) in sats.items():
        try:
            t, events = sat.find_events(site, now, end, altitude_degrees=PASS_MIN_ALT)
            for i, (ti, event) in enumerate(zip(t, events)):
                if event == 0:  # rise
                    rise_time = ti.utc_datetime()
                    max_alt = 0
                    culm_time = rise_time
                    set_time = rise_time
                    duration_min = 0
                    if i + 1 < len(events) and events[i + 1] == 1:
                        culm_time = t[i + 1].utc_datetime()
                        topocentric = (sat - site).at(t[i + 1])
                        alt, az, dist = topocentric.altaz()
                        max_alt = alt.degrees
                        if i + 2 < len(events) and events[i + 2] == 2:
                            set_time = t[i + 2].utc_datetime()
                            duration_min = (set_time - rise_time).total_seconds() / 60
                    passes.append({
                        "sat": sat,
                        "sat_name": name,
                        "frequency": freq,
                        "rise_utc": rise_time,
                        "culm_utc": culm_time,
                        "set_utc": set_time,
                        "max_alt": round(max_alt, 1),
                        "duration_min": round(duration_min, 1),
                    })
        except Exception as e:
            log_console(f"Pass prediction failed for {name}: {e}", "error")
    
    passes.sort(key=lambda p: p["rise_utc"])
    return passes

def passes_to_json(passes):
    """Convert pass list to JSON-serializable format for the API."""
    result = []
    for p in passes:
        result.append({
            "sat_name": p["sat_name"],
            "frequency_mhz": round(p["frequency"] / 1e6, 4),
            "rise_local": (p["rise_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
            "culm_local": (p["culm_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "set_local": (p["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "max_alt": p["max_alt"],
            "duration_min": p["duration_min"],
            "quality": "high" if p["max_alt"] >= 35 else ("medium" if p["max_alt"] >= 15 else "low"),
            "rise_timestamp": p["rise_utc"].timestamp(),
            "set_timestamp": p["set_utc"].timestamp(),
        })
    return result

def log_pass(sat_name, frequency, max_alt, duration_min, rise_time, set_time, signal_peak, decoded, png_file, wav_file):
    """Log a completed pass (with recording metadata) to the history file."""
    history = []
    if os.path.exists(PASS_HISTORY_FILE):
        try:
            with open(PASS_HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except Exception:
            pass
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
        "timestamp": datetime.now().isoformat(),
    })
    # Keep last 50 passes
    history = history[-50:]
    with open(PASS_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def scheduler_thread():
    """Background thread: refresh TLEs, predict passes, trigger frequency switches."""
    global upcoming_passes, current_pass, current_frequency, current_sat_name, is_pass_active, pass_signal_peak
    while True:
        try:
            # Refresh TLEs if stale
            if time.time() - last_tle_refresh > TLE_REFRESH_HOURS * 3600:
                sats = refresh_tles()
            else:
                sats = refresh_tles()  # first run
            
            # Predict passes
            passes = predict_passes(sats, PASS_PREDICT_HOURS)
            with status_lock:
                upcoming_passes = passes
            
            log_console(f"Predicted {len(passes)} passes in next {PASS_PREDICT_HOURS}h")
            for p in passes[:5]:
                local_rise = p["rise_utc"] + timedelta(hours=UTC_OFFSET)
                log_console(f"  {p['sat_name']} {p['max_alt']:.0f}° at {local_rise.strftime('%H:%M')} ({round(p['frequency']/1e6,4)} MHz)")
            
            # Check every 10 seconds if we need to switch for an upcoming pass
            while True:
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                triggered = None
                for p in passes:
                    # Start pass recording 30s before rise
                    start_time = p["rise_utc"] - timedelta(seconds=PASS_MARGIN_SECS)
                    end_time = p["set_utc"] + timedelta(seconds=PASS_MARGIN_SECS)
                    if start_time <= now <= end_time:
                        triggered = p
                        break
                
                finished_pass = None
                pass_peak = 0.0
                with status_lock:
                    if triggered and (current_pass is None
                                      or current_pass["sat_name"] != triggered["sat_name"]
                                      or current_pass["rise_utc"] != triggered["rise_utc"]):
                        current_pass = triggered
                        current_frequency = triggered["frequency"]
                        current_sat_name = triggered["sat_name"]
                        is_pass_active = True
                        with signal_lock:
                            pass_signal_peak = 0.0
                        local_rise = triggered["rise_utc"] + timedelta(hours=UTC_OFFSET)
                        log_console(f"🔴 PASS START: {triggered['sat_name']} {round(triggered['frequency']/1e6,4)} MHz, max {triggered['max_alt']}° at {local_rise.strftime('%H:%M')}")
                    elif not triggered and current_pass is not None:
                        finished_pass = current_pass
                        local_set = finished_pass["set_utc"] + timedelta(hours=UTC_OFFSET)
                        log_console(f"✅ PASS END: {finished_pass['sat_name']} finished at {local_set.strftime('%H:%M')}")
                        current_pass = None
                        is_pass_active = False
                        # Return to NOAA 15 idle frequency
                        current_frequency = 137620000
                        current_sat_name = "NOAA 15 (idle)"
                        with signal_lock:
                            pass_peak = pass_signal_peak
                            pass_signal_peak = 0.0

                if finished_pass is not None:
                    # Wait for the SDR thread to finalize the WAV, then auto-decode it
                    time.sleep(2)
                    decoded = False
                    png_file = None
                    wav_name = None
                    recordings = sorted(glob.glob(os.path.join(RECORD_DIR, "*.wav")), key=os.path.getmtime, reverse=True)
                    if recordings:
                        latest = recordings[0]
                        wav_name = os.path.basename(latest)
                        latest_png = latest.replace('.wav', '.png')
                        if not os.path.exists(latest_png):
                            log_console(f"Auto-decoding: {wav_name}")
                            try:
                                result = subprocess.run(
                                    ['noaa-apt', latest, '-o', latest_png, '-q'],
                                    capture_output=True, text=True, timeout=120
                                )
                                if os.path.exists(latest_png):
                                    decoded = True
                                    png_file = os.path.basename(latest_png)
                                    log_console(f"Auto-decode successful: {png_file}")
                                else:
                                    log_console(f"Auto-decode failed: {result.stderr}", "error")
                            except Exception as e:
                                log_console(f"Auto-decode error: {e}", "error")
                        else:
                            decoded = True
                            png_file = os.path.basename(latest_png)
                    log_pass(finished_pass["sat_name"], finished_pass["frequency"],
                             finished_pass["max_alt"], finished_pass["duration_min"],
                             finished_pass["rise_utc"], finished_pass["set_utc"],
                             pass_peak, decoded, png_file, wav_name)
                
                # Refresh passes list every 30 min
                if datetime.utcnow().minute % 30 == 0 and datetime.utcnow().second < 10:
                    break
                
                time.sleep(10)
        except Exception as e:
            log_console(f"Scheduler error: {e}", "error")
            time.sleep(60)

def sdr_thread():
    """Main SDR thread: rtl_sdr → FFT waterfall + FM demod → WAV recording during passes."""
    global rtl_sdr_proc, current_wav, current_wav_path, signal_strength, is_recording, pass_signal_peak
    import numpy as np
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    rtl_log_f = open(RTL_LOG, 'w')

    while True:
        try:
            freq_str = f"{current_frequency}"
            rtl_sdr_proc = subprocess.Popen(
                ['rtl_sdr', '-f', freq_str, '-s', str(SDR_RATE), '-g', str(SDR_GAIN), '-'],
                stdout=subprocess.PIPE, stderr=rtl_log_f
            )
            log_console(f"rtl_sdr started (pid {rtl_sdr_proc.pid}), freq={freq_str}Hz, gain={SDR_GAIN}dB")
            last_history_append = 0.0

            while True:
                raw = rtl_sdr_proc.stdout.read(IQ_BLOCK)
                if not raw or len(raw) < IQ_BLOCK:
                    log_console("rtl_sdr stdout closed, restarting...", "warn")
                    break

                # FFT for waterfall
                try:
                    iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
                    i_s = iq[0::2]
                    q_s = iq[1::2]
                    complex_signal = i_s + 1j * q_s
                    if len(complex_signal) >= FFT_SIZE:
                        complex_signal = complex_signal[:FFT_SIZE]
                        window = np.hamming(len(complex_signal))
                        windowed = complex_signal * window
                        fft_result = np.fft.fftshift(np.fft.fft(windowed))
                        magnitude = np.abs(fft_result)
                        center = len(magnitude) // 2
                        band = magnitude[center-10:center+10].mean()
                        with signal_lock:
                            signal_strength = float(band)
                            if is_recording:
                                pass_signal_peak = max(pass_signal_peak, signal_strength)
                        now_ts = time.time()
                        if now_ts - last_history_append >= 1.0:
                            last_history_append = now_ts
                            with signal_lock:
                                signal_history.append(float(band))
                        if magnitude.max() > 0:
                            magnitude = magnitude / magnitude.max() * 255
                        row = magnitude.astype(int).tolist()
                        with waterfall_lock:
                            waterfall_buffer.append(row)
                except Exception:
                    pass

                # Record to WAV only during passes
                should_record = False
                with status_lock:
                    should_record = is_pass_active

                if should_record and not is_recording:
                    # Start new recording
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    sat_short = current_sat_name.replace(" ", "_").replace("(idle)", "idle")
                    current_wav_path = os.path.join(RECORD_DIR, f"{sat_short}_{timestamp}.wav")
                    current_wav = wave.open(current_wav_path, 'wb')
                    current_wav.setnchannels(1)
                    current_wav.setsampwidth(2)
                    current_wav.setframerate(AUDIO_RATE)
                    is_recording = True
                    log_console(f"🎬 Recording started: {current_wav_path}")

                elif not should_record and is_recording:
                    # Stop recording
                    try:
                        current_wav.close()
                        log_console(f"🎬 Recording stopped: {current_wav_path}")
                    except Exception as e:
                        log_console(f"WAV close error: {e}", "error")
                    current_wav = None
                    current_wav_path = None
                    is_recording = False

                # FM-demodulate every block: feeds the live audio stream,
                # and is written to the WAV during passes
                try:
                    audio = fm_demod(raw, DECIMATION)
                except Exception:
                    audio = b''
                if audio:
                    push_live_audio(audio)

                if is_recording and current_wav:
                    try:
                        if audio:
                            current_wav.writeframes(audio)
                    except Exception as e:
                        log_console(f"WAV write error: {e}", "error")

        except Exception as e:
            log_console(f"SDR thread error: {e}", "error")
        try: rtl_sdr_proc.kill()
        except: pass
        try:
            if current_wav:
                current_wav.close()
        except: pass
        is_recording = False
        current_wav = None
        time.sleep(5)

def get_recordings():
    """List available recordings."""
    recordings = []
    if os.path.exists(RECORD_DIR):
        for f in sorted(os.listdir(RECORD_DIR), reverse=True):
            if f.endswith('.wav'):
                path = os.path.join(RECORD_DIR, f)
                size = os.path.getsize(path)
                has_png = os.path.exists(path.replace('.wav', '.png'))
                recordings.append({
                    "filename": f,
                    "size_mb": round(size / (1024*1024), 1),
                    "decoded": has_png,
                    "png": f.replace('.wav', '.png') if has_png else None,
                })
    return recordings

def get_status():
    with status_lock:
        freq = current_frequency
        sat = current_sat_name
        passing = is_pass_active
        passes = upcoming_passes
        cur_pass = current_pass
    
    status = {
        "rtl_sdr_running": rtl_sdr_proc is not None and rtl_sdr_proc.poll() is None,
        "frequency_mhz": round(freq / 1e6, 4),
        "satellite": sat,
        "pass_active": passing,
        "recording": is_recording,
        "recording_count": len(get_recordings()),
        "signal_strength": 0,
        "rtl_log": "",
        "next_pass": None,
    }
    with signal_lock:
        status["signal_strength"] = round(signal_strength, 2)
    
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

CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NOAAh's CrabArk — Debug Console</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: monospace; background: #12121e; color: #e0e0e0; padding: 12px 15px; }
h1 { font-size: 1.1em; color: #53d769; margin-bottom: 4px; }
.sub { font-size: 0.75em; color: #888; margin-bottom: 10px; }
.sub a { color: #53d769; }
#toolbar { display: flex; gap: 15px; font-size: 0.75em; color: #888; margin-bottom: 8px; align-items: center; }
#toolbar label { cursor: pointer; }
#count { color: #f39c12; }
#console { background: #000; border: 1px solid #0f3460; border-radius: 4px; padding: 8px; height: calc(100vh - 110px); overflow-y: auto; font-size: 0.8em; line-height: 1.5; }
.line { white-space: pre-wrap; word-break: break-all; }
.line .t { color: #666; margin-right: 5px; }
.line.error { color: #e74c3c; }
.line.warn { color: #f39c12; }
.line.info { color: #e0e0e0; }
</style>
</head>
<body>
<h1>🛰️ NOAAh's CrabArk — Debug Console</h1>
<div class="sub">Last 100 server messages — <a href="/console.json">raw JSON</a> — <a href="/">dashboard</a></div>
<div id="toolbar">
    <label><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
    <span id="count"></span>
</div>
<div id="console"></div>
<script>
const el = document.getElementById('console');
const countEl = document.getElementById('count');
function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
async function poll() {
    try {
        const res = await fetch('/console.json');
        const lines = await res.json();
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        el.innerHTML = lines.map(l =>
            '<div class="line ' + l.level + '"><span class="t">' + l.time + '</span>' + escapeHtml(l.msg) + '</div>'
        ).join('');
        countEl.textContent = lines.length + ' messages';
        if (document.getElementById('autoscroll').checked && atBottom) el.scrollTop = el.scrollHeight;
    } catch(e) {}
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""

HISTORY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NOAAh's CrabArk — Pass History & Recordings</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 12px 15px; }
h1 { font-size: 1.2em; color: #53d769; margin-bottom: 4px; }
.sub { font-size: 0.8em; color: #888; margin-bottom: 12px; }
.sub a { color: #53d769; }
.sub #crab-count { color: #f39c12; font-weight: bold; }
.section-title { color: #53d769; font-size: 1em; margin: 14px 0 8px 0; }
.pass-card { background: #16213e; border: 1px solid #0f3460; border-left: 3px solid #53d769; margin-bottom: 10px; padding: 10px; border-radius: 4px; }
.pass-card.medium { border-left-color: #f39c12; }
.pass-card.low { border-left-color: #e74c3c; }
.card-head { font-size: 0.85em; color: #a8b8d8; margin-bottom: 6px; }
.card-head .sat { color: #53d769; font-weight: bold; font-size: 1.05em; }
.card-meta { font-size: 0.7em; color: #888; margin-bottom: 6px; }
.filename { font-size: 0.75em; color: #888; word-break: break-all; margin-bottom: 4px; }
button { padding: 4px 10px; background: #53d769; color: #1a1a2e; border: none; border-radius: 3px; cursor: pointer; font-family: monospace; font-size: 0.75em; }
button:hover { background: #3eb852; }
button.danger { background: #e74c3c; color: #fff; margin-left: 6px; }
button.danger:hover { background: #c0392b; }
audio { width: 100%; margin-top: 6px; height: 30px; }
.decoded-img { max-width: 100%; margin-top: 8px; border-radius: 4px; border: 1px solid #0f3460; }
.result { font-size: 0.75em; margin-top: 6px; }
.muted { color: #666; font-size: 0.75em; padding: 10px; }
</style>
</head>
<body>
<h1>📚 Pass History &amp; Recordings</h1>
<div class="sub"><a href="/">&#8592; dashboard</a> &nbsp;|&nbsp; 🦀 Crabs caught: <span id="crab-count">0</span></div>
<div class="section-title">Recorded Passes</div>
<div id="history-list" class="muted">Loading pass history...</div>
<div class="section-title" id="unmatched-title" style="display:none;">Unmatched Recordings</div>
<div id="unmatched-list"></div>
<script>
const decodingInProcess = {};
let historyData = [];
let recordingsData = [];

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function safeId(f) { return f.replace(/[^a-zA-Z0-9]/g, ''); }

function render() {
    const list = document.getElementById('history-list');
    if (historyData.length === 0) {
        list.innerHTML = '<div class="muted">No passes logged yet. Passes appear here after they complete.</div>';
    } else {
        let html = '';
        for (const h of [...historyData].reverse()) {
            const q = h.max_alt >= 35 ? 'high' : (h.max_alt >= 15 ? 'medium' : 'low');
            const qi = q === 'high' ? '🟢' : (q === 'medium' ? '🟡' : '🔴');
            const rec = h.wav ? recordingsData.find(r => r.filename === h.wav) : null;
            const img = (h.png && rec && rec.png) ? h.png : (rec && rec.png ? rec.png : null);
            const rid = h.wav ? 'result-' + safeId(h.wav) : '';
            html += '<div class="pass-card ' + q + '">'
                + '<div class="card-head"><span class="sat">' + qi + ' ' + esc(h.sat_name) + '</span>'
                + ' &nbsp;' + esc(h.rise_local) + ' → ' + esc(h.set_local || '?')
                + ' &nbsp;|&nbsp; ' + esc(h.frequency_mhz) + ' MHz</div>'
                + '<div class="card-meta">max ' + esc(h.max_alt) + '° &nbsp;|&nbsp; '
                + esc(h.duration_min) + ' min &nbsp;|&nbsp; peak: ' + esc(h.signal_peak || '—')
                + ' &nbsp;|&nbsp; ' + (h.decoded ? '🖼️ decoded' : '❌ not decoded') + '</div>';
            if (rec) {
                html += '<div class="filename">' + esc(rec.filename) + ' (' + esc(rec.size_mb) + ' MB)</div>'
                    + (rec.decoded ? '' : '<button onclick="decodeRecording(\'' + esc(rec.filename) + '\')">🔄 Decode APT Image</button>')
                    + '<button class="danger" onclick="deleteRecording(\'' + esc(rec.filename) + '\')">🗑 Delete</button>'
                    + '<audio controls preload="none"><source src="/audio/' + esc(rec.filename) + '" type="audio/wav"></audio>'
                    + '<div class="result" id="' + rid + '"></div>'
                    + (img ? '<img class="decoded-img" src="/images/' + esc(img) + '" alt="APT image" onclick="window.open(\'/images/' + esc(img) + '\')" style="cursor:pointer;">' : '');
            } else {
                html += '<div class="muted" style="padding:4px 0;">' + (h.wav ? 'recording file deleted' : 'no recording on disk') + '</div>';
            }
            html += '</div>';
        }
        list.innerHTML = html;
    }
    const unmatched = recordingsData.filter(r => !historyData.some(h => h.wav === r.filename));
    const utable = document.getElementById('unmatched-list');
    document.getElementById('unmatched-title').style.display = unmatched.length ? '' : 'none';
    let uhtml = '';
    for (const r of unmatched) {
        const rid = 'result-' + safeId(r.filename);
        uhtml += '<div class="pass-card">'
            + '<div class="card-head"><span class="sat">🎧 ' + esc(r.filename) + '</span> &nbsp;|&nbsp; ' + esc(r.size_mb) + ' MB</div>'
            + '<button onclick="decodeRecording(\'' + esc(r.filename) + '\')">🔄 Decode APT Image</button>'
            + '<button class="danger" onclick="deleteRecording(\'' + esc(r.filename) + '\')">🗑 Delete</button>'
            + '<audio controls preload="none"><source src="/audio/' + esc(r.filename) + '" type="audio/wav"></audio>'
            + '<div class="result" id="' + rid + '"></div>'
            + (r.png ? '<img class="decoded-img" src="/images/' + esc(r.png) + '" alt="APT image">' : '')
            + '</div>';
    }
    utable.innerHTML = uhtml;
    document.getElementById('crab-count').textContent = recordingsData.filter(r => r.decoded).length;
    // Auto-decode recordings that are not decoded yet
    for (const r of recordingsData) {
        if (!r.decoded && !decodingInProcess[r.filename]) {
            decodingInProcess[r.filename] = true;
            setTimeout(() => decodeRecording(r.filename), 3000);
        }
    }
}

async function decodeRecording(filename) {
    const resultDiv = document.getElementById('result-' + safeId(filename));
    if (resultDiv) resultDiv.innerHTML = '<span style="color:#f39c12;">⏳ Decoding...</span>';
    try {
        const res = await fetch('/decode/' + filename);
        const data = await res.json();
        if (data.success && resultDiv) {
            resultDiv.innerHTML = '<span style="color:#53d769;">✅ Decoded!</span><img class="decoded-img" src="/images/' + esc(data.png) + '?t=' + Date.now() + '" alt="Decoded APT image">';
        } else if (resultDiv) {
            resultDiv.innerHTML = '<span style="color:#e74c3c;">❌ ' + esc(data.error || 'Decode failed') + '</span>';
        }
    } catch(e) {
        if (resultDiv) resultDiv.innerHTML = '<span style="color:#e74c3c;">❌ Error: ' + esc(e) + '</span>';
    }
}

async function deleteRecording(filename) {
    if (!confirm('Delete ' + filename + ' and its decoded image? This cannot be undone.')) return;
    try {
        const res = await fetch('/delete/' + filename);
        const data = await res.json();
        if (data.success) {
            delete decodingInProcess[filename];
            refresh();
        } else {
            alert('Delete failed: ' + (data.error || 'unknown error'));
        }
    } catch(e) {
        alert('Delete failed: ' + e);
    }
}

async function refresh() {
    try {
        const [h, r] = await Promise.all([fetch('/pass_history.json'), fetch('/recordings.json')]);
        historyData = await h.json();
        recordingsData = await r.json();
        render();
    } catch(e) { console.error('History refresh failed:', e); }
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""

class NOAAHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/status.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(get_status()).encode())
        elif self.path == '/waterfall.json':
            with waterfall_lock:
                data = list(waterfall_buffer)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path == '/passes.json':
            with status_lock:
                passes = passes_to_json(upcoming_passes)
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
            with signal_lock:
                history = list(signal_history)
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
            with status_lock:
                cur = current_pass
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
                        log_console(f"Track computation failed: {e}", "error")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        elif self.path == '/console.json':
            with console_lock:
                lines = list(console_buffer)
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
            log_console(f"🔊 Live audio listener connected ({self.client_address[0]})")
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
                with live_audio_cond:
                    pos = live_audio_total  # start at the live edge for low latency
                while True:
                    data = None
                    with live_audio_cond:
                        if pos >= live_audio_total:
                            live_audio_cond.wait(timeout=5)
                        if pos >= live_audio_total:
                            # No new audio (SDR idle/restarting): send 100 ms of
                            # silence to keep the connection alive and notice a
                            # disconnected client on write
                            data = b'\x00\x00' * (AUDIO_RATE // 10)
                        else:
                            if pos < live_audio_base:
                                pos = live_audio_base  # fell behind, skip dropped chunks
                            data = b''.join(live_audio_data[pos - live_audio_base:])
                            pos = live_audio_total
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # listener disconnected
            log_console(f"🔊 Live audio listener disconnected ({self.client_address[0]})")
            return
        elif self.path.startswith('/decode/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename:
                log_console(f"Decode rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(wav_path):
                log_console(f"Decode requested missing recording: {filename}", "warn")
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Recording not found')
                return
            output_png = wav_path.replace('.wav', '.png')
            try:
                result = subprocess.run(
                    ['noaa-apt', wav_path, '-o', output_png, '-q'],
                    capture_output=True, text=True, timeout=120
                )
                if os.path.exists(output_png):
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True,
                        "png": filename.replace('.wav', '.png'),
                    }).encode())
                else:
                    log_console(f"Decode failed for {filename}: {result.stderr or 'No output image'}", "error")
                    self.send_response(500)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": result.stderr or "No output image"
                    }).encode())
            except subprocess.TimeoutExpired:
                log_console(f"Decode timeout for {filename}", "error")
                self.send_response(504)
                self.end_headers()
                self.wfile.write(b'Decode timeout')
            except Exception as e:
                log_console(f"Decode error for {filename}: {e}", "error")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'Error: {e}'.encode())
        elif self.path.startswith('/delete/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename or not filename.endswith('.wav'):
                log_console(f"Delete rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = os.path.join(RECORD_DIR, filename)
            if not os.path.exists(wav_path):
                log_console(f"Delete requested missing recording: {filename}", "warn")
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Recording not found"}).encode())
                return
            # Never delete the file that is currently being written
            with status_lock:
                recording_now = is_recording and current_wav_path == wav_path
            if recording_now:
                log_console(f"Delete refused, recording in progress: {filename}", "warn")
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
                log_console(f"🗑 Deleted: {', '.join(deleted)}")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "deleted": deleted}).encode())
            except Exception as e:
                log_console(f"Delete error for {filename}: {e}", "error")
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

if __name__ == '__main__':
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    
    # Start scheduler thread (TLE refresh + pass prediction + frequency switching)
    sched_t = threading.Thread(target=scheduler_thread, daemon=True)
    sched_t.start()
    
    # Start SDR thread
    sdr_t = threading.Thread(target=sdr_thread, daemon=True)
    sdr_t.start()
    
    log_console(f"NOAA Receiver started (Regensburg {LAT}N {LON}E)")
    log_console(f"Auto pass tracking enabled, recording only during passes (>{PASS_MIN_ALT}°)")
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), NOAAHandler) as httpd:
        log_console(f"Server running on port {PORT}")
        httpd.serve_forever()
