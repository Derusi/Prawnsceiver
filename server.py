#!/usr/bin/env python3
"""
APRS Monitor Web Server with Live Waterfall — Optimized
- Uses rtl_sdr (raw IQ) for waterfall FFT and APRS decoding
- Optimized gain (35 dB) and sample rate for R820T tuner
- Software FM demodulation feeds direwolf
- Serves web dashboard, packets, status, and waterfall data
"""
import http.server
import socketserver
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from collections import deque

PORT = 8085
LOGDIR = "/var/log/aprs"
PACKET_FILE = os.path.join(LOGDIR, "aprs_packets.log")
RTL_LOG = os.path.join(LOGDIR, "rtl_fm.log")
DIREWOLF_LOG = os.path.join(LOGDIR, "direwolf.log")
WEBDIR = "/home/eugene/aprs_website"
FFT_SIZE = 512
WATERFALL_ROWS = 120
# R820T optimal: 35 dB gain, 240 kHz sample rate for good FFT resolution
SDR_GAIN = 35
SDR_RATE = 240000
# direwolf expects 48000 Hz audio
AUDIO_RATE = 48000
# Block size: enough IQ samples for FFT + audio decimation
IQ_BLOCK = FFT_SIZE * 2  # 2 bytes per IQ sample (I+Q, each 1 byte)
# Decimation factor: 240000 / 48000 = 5
DECIMATION = SDR_RATE // AUDIO_RATE

waterfall_buffer = deque(maxlen=WATERFALL_ROWS)
waterfall_lock = threading.Lock()
rtl_sdr_proc = None
direwolf_proc = None

def fm_demod(iq_bytes, decimation=5):
    """FM demodulate raw 8-bit IQ and decimate to target audio rate."""
    import numpy as np
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32) - 127.5
    i_samples = raw[0::2]
    q_samples = raw[1::2]
    # Phase = atan2(Q, I)
    phase = np.arctan2(q_samples, i_samples)
    # Differentiate phase for FM demodulated audio
    audio = np.diff(phase)
    # Decimate (downsample) to target audio rate
    if decimation > 1:
        audio = audio[::decimation]
    # Convert to int16
    audio = (audio * 32767 / (np.pi + 1e-9)).astype(np.int16)
    return audio.tobytes()

def sdr_thread():
    """Main SDR thread: rtl_sdr → FFT (waterfall) + FM demod → direwolf."""
    global rtl_sdr_proc, direwolf_proc
    import numpy as np
    os.makedirs(LOGDIR, exist_ok=True)
    rtl_log_f = open(RTL_LOG, 'w')
    dw_log_f = open(DIREWOLF_LOG, 'w')

    while True:
        try:
            direwolf_proc = subprocess.Popen(
                ['direwolf', '-c', '/home/eugene/direwolf.conf', '-r', str(AUDIO_RATE), '-a', '1', '-'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=dw_log_f
            )
            rtl_sdr_proc = subprocess.Popen(
                ['rtl_sdr', '-f', '144.8M', '-s', str(SDR_RATE), '-g', str(SDR_GAIN), '-'],
                stdout=subprocess.PIPE, stderr=rtl_log_f
            )
            print(f"rtl_sdr started (pid {rtl_sdr_proc.pid}), gain={SDR_GAIN}dB, rate={SDR_RATE}Hz")
            print(f"direwolf started (pid {direwolf_proc.pid}), audio rate={AUDIO_RATE}Hz")

            while True:
                raw = rtl_sdr_proc.stdout.read(IQ_BLOCK)
                if not raw or len(raw) < IQ_BLOCK:
                    print("rtl_sdr stdout closed, restarting...")
                    break
                # FFT for waterfall (from raw IQ)
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
                        if magnitude.max() > 0:
                            magnitude = magnitude / magnitude.max() * 255
                        row = magnitude.astype(int).tolist()
                        with waterfall_lock:
                            waterfall_buffer.append(row)
                except Exception:
                    pass
                # FM-demodulate and feed to direwolf
                try:
                    audio = fm_demod(raw, DECIMATION)
                    if audio:
                        direwolf_proc.stdin.write(audio)
                        direwolf_proc.stdin.flush()
                except BrokenPipeError:
                    print("direwolf stdin broken, restarting...")
                    break
                except Exception as e:
                    print(f"direwolf write error: {e}")
                    break
        except Exception as e:
            print(f"SDR thread error: {e}")
        try: rtl_sdr_proc.kill()
        except: pass
        try: direwolf_proc.kill()
        except: pass
        time.sleep(5)

def parse_packets():
    packets = []
    if not os.path.exists(PACKET_FILE):
        return packets
    try:
        with open(PACKET_FILE, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if '>' in line:
                    callsign = line.split('>')[0].strip()
                    message = line
                else:
                    callsign = "Unknown"
                    message = line
                packets.append({"callsign": callsign, "message": message, "timestamp": timestamp})
    except: pass
    return packets

def get_status():
    status = {
        "rtl_fm_running": rtl_sdr_proc is not None and rtl_sdr_proc.poll() is None,
        "direwolf_running": direwolf_proc is not None and direwolf_proc.poll() is None,
        "packet_count": 0, "last_packet": None, "rtl_log": "", "direwolf_log": ""
    }
    packets = parse_packets()
    status["packet_count"] = len(packets)
    if packets: status["last_packet"] = packets[-1].get("timestamp", "—")
    for log_file, key in [(RTL_LOG, "rtl_log"), (DIREWOLF_LOG, "direwolf_log")]:
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r', errors='replace') as f:
                    status[key] = ''.join(f.readlines()[-10:]).strip()
        except: pass
    return status

class APRSHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/packets.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(parse_packets()).encode())
        elif self.path == '/status.json':
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
        else:
            super().do_GET()
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEBDIR, **kwargs)
    def log_message(self, *args): pass

if __name__ == '__main__':
    socketserver.TCPServer.allow_reuse_address = True
    t = threading.Thread(target=sdr_thread, daemon=True)
    t.start()
    print("SDR thread started (optimized: gain=35dB, rate=240kHz, FFT=512)")
    with socketserver.TCPServer(("0.0.0.0", PORT), APRSHandler) as httpd:
        print(f"APRS Monitor server running on port {PORT}")
        httpd.serve_forever()
