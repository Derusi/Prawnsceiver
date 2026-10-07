"""Integration test: the real sdr_capture_thread against a fake rtl_tcp server.

Drives the recording state machine (pass start/end, same-frequency pass
switch, band switch mid-pass, per-dongle override, WAV open failure, foreign
stream detection) and checks the rtl_tcp commands and WAV files it produces.
rtl_tcp itself is replaced by a stand-in child process ('sleep').

Run: python3 -u tests/test_radio.py (needs numpy; ~20 s; uses TCP port 1299)
"""
import glob, socket, struct, subprocess, tempfile, threading, time, wave
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import numpy as np
from collections import deque
from noaa_receiver import radio, state
from noaa_receiver.config import SDR_OFFSET_HZ, WATERFALL_ROWS
from noaa_receiver.calibration import tuning_correction

tmp = tempfile.mkdtemp(prefix='prawn_')
radio.LOGDIR = os.path.join(tmp, 'log'); radio.RECORD_DIR = os.path.join(tmp, 'rec')
radio.RTL_LOG = os.path.join(radio.LOGDIR, 'rtl_sdr.log')
radio.WAV_RETRY_SECS = 0.5
radio.PLL_CHECK_AFTER_SECS = 0.5
SERIAL = 'TESTSN'
PORT = 1299

# ---- fake rtl_tcp: header + endless IQ, records control commands ----
commands = []
class FakeRtlTcp(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', PORT)); self.srv.listen(1)
        self.conn = None; self.sessions = 0
    def run(self):
        rng = np.random.default_rng(0)
        while True:
            conn, _ = self.srv.accept()
            self.sessions += 1; self.conn = conn
            try:
                conn.sendall(b'RTL0' + struct.pack('!II', 5, 29))
                conn.settimeout(0.01)
                chunk = rng.integers(100, 156, 65536, dtype=np.uint8).tobytes()
                while True:
                    conn.sendall(chunk)
                    try:
                        cmd = conn.recv(5)
                        if not cmd: break
                        while len(cmd) < 5:
                            more = conn.recv(5 - len(cmd))
                            if not more: break
                            cmd += more
                        commands.append(struct.unpack('!BI', cmd))
                    except socket.timeout:
                        pass
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                conn.close(); self.conn = None
srv = FakeRtlTcp(); srv.start()

import traceback
def _hook(*a):
    traceback.print_exception(*a)
    for c in children: c.kill()
    os._exit(1)
sys.excepthook = _hook
# ---- unit: port probe + command framing ----
assert radio._port_in_use(PORT) is True, "probe must see the fake listener"
assert radio._port_in_use(PORT + 50) is False
assert struct.pack('!BI', 1, 437610000) == b'\x01' + (437610000).to_bytes(4, 'big')
print("port probe + command framing OK")
radio._port_in_use = lambda port: False   # the fake server legitimately owns the port from here on

# ---- stand-in child process + doppler spy ----
children = []
real_popen = subprocess.Popen
def fake_popen(args, **kw):
    p = real_popen(['sleep', '1000']); children.append(p); return p
radio.subprocess.Popen = fake_popen
last_doppler = {'d': None}
real_doppler = radio.doppler_shift
def spy_doppler(c, d, fs, st):
    last_doppler['d'] = d; return real_doppler(c, d, fs, st)
radio.doppler_shift = spy_doppler

state.primary_serial = SERIAL
state.sdrs[SERIAL] = {
    'label': 'fake', 'tuner': 'R820T', 'serial': SERIAL, 'primary': True,
    'waterfall': state.waterfall_buffer, 'lock': state.waterfall_lock,
    'signal': 0.0, 'proc': None, 'last_data': 0.0, 'port': PORT,
    'la': {'data': [], 'base': 0, 'total': 0, 'cond': threading.Condition()},
    'is_recording': False, 'wav': None, 'wav_path': None,
}
entry = state.sdrs[SERIAL]
t = threading.Thread(target=radio.sdr_capture_thread, args=(SERIAL,), daemon=True); t.start()

def wait_for(cond, secs=4.0, what=''):
    end = time.time() + secs
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    raise AssertionError(f'timeout waiting for {what}')

def set_sched(freq, sat, active):
    with state.status_lock:
        state.current_frequency = freq; state.current_sat_name = sat; state.is_pass_active = active

def expect_cmd(freq):
    want = (1, freq + SDR_OFFSET_HZ + tuning_correction(freq, SERIAL))
    wait_for(lambda: want in commands, what=f'retune command {want}')
    commands.remove(want)

def wavs(): return sorted(glob.glob(os.path.join(radio.RECORD_DIR, '*.wav')))

# 1. idle streaming
wait_for(lambda: entry['last_data'] > 0 and len(entry['waterfall']) > 3, what='IQ flowing')
wait_for(lambda: entry['la']['total'] > 10, what='live audio')
assert not entry['is_recording'] and not commands
assert last_doppler['d'] == 0
print("1 idle: streaming, waterfall rows, live audio, no retune OK")

# 2. pass start NOAA 19 @137.1 + doppler
set_sched(137100000, "NOAA 19", True)
expect_cmd(137100000)
wait_for(lambda: entry['is_recording'] and 'NOAA_19_' in os.path.basename(entry['wav_path'] or ''), what='NOAA 19 wav')
assert state.is_recording and state.current_wav_path == entry['wav_path']
with state.status_lock:
    state.doppler_hz, state.doppler_freq_hz = 2000, 137100000
wait_for(lambda: last_doppler['d'] == 2000, what='doppler applied after live retune (was gated on the start frequency)')
print("2 pass start: retune, WAV open, Doppler applied on the retuned band OK")

# 3. same-frequency pass switch
first_wav = entry['wav_path']
set_sched(137100000, "Meteor-M 2-3", True)
wait_for(lambda: entry['wav_path'] and 'Meteor-M_2-3_' in os.path.basename(entry['wav_path']), what='Meteor wav')
time.sleep(0.2); assert not commands, f"no retune expected on same frequency, got {commands}"
with wave.open(first_wav) as w: assert w.getnframes() > 0 and w.getframerate() == 48000
print("3 same-frequency switch: WAV split, no retune, first WAV valid OK")

# 4. band switch mid-pass (overlapping pass) to ISS
set_sched(437550000, "ISS (Zarya)", True)
expect_cmd(437550000)
wait_for(lambda: entry['wav_path'] and 'ISS_(Zarya)_' in os.path.basename(entry['wav_path']), what='ISS wav')
with state.status_lock:
    state.doppler_hz, state.doppler_freq_hz = -9000, 437550000
wait_for(lambda: last_doppler['d'] == -9000, what='ISS doppler')
print("4 band switch mid-pass: WAV closed/reopened, retune, Doppler follows OK")

# 5. manual dongle override set and cleared during the pass
with state.status_lock: state.manual_dongle_freq[SERIAL] = 100000000
expect_cmd(100000000)
wait_for(lambda: not entry['is_recording'], what='override stops recording')
wait_for(lambda: last_doppler['d'] == 0, what='doppler off on the override band')
with state.status_lock: state.manual_dongle_freq.pop(SERIAL)
expect_cmd(437550000)
wait_for(lambda: entry['is_recording'], what='recording resumes after override cleared')
print("5 dongle override: retune out/in, recording paused/resumed OK")

# 6. pass end
set_sched(137620000, "NOAA 15 (idle)", False)
expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'] and not state.is_recording and state.current_wav_path is None, what='pass end closes WAV')
for p in wavs():
    with wave.open(p) as w: assert w.getnframes() > 0, p
print(f"6 pass end: WAV closed, {len(wavs())} valid WAVs: {[os.path.basename(p) for p in wavs()]}")

# 7. WAV open failure must not kill the receiver; retried later
os.chmod(radio.RECORD_DIR, 0o500)
sessions_before = srv.sessions; child = children[-1]
set_sched(137912500, "NOAA 18", True)
expect_cmd(137912500)
time.sleep(1.5)
print("   7 diag: recording=", entry['is_recording'], "child alive=", child.poll() is None, "sessions", sessions_before, "->", srv.sessions, "wav_path", entry['wav_path'])
assert not entry['is_recording'] and child.poll() is None and srv.sessions == sessions_before, "receiver restarted on a WAV open failure"
os.chmod(radio.RECORD_DIR, 0o700)
wait_for(lambda: entry['is_recording'], secs=3, what='WAV open retried')
set_sched(137620000, "NOAA 15 (idle)", False); expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'], what='pass end')
print("7 WAV open failure: no restart, retried after WAV_RETRY_SECS OK")

# 8. foreign stream: our child dies while the port keeps streaming -> restart, not silent use
sessions_before = srv.sessions
children[-1].kill()
wait_for(lambda: srv.sessions > sessions_before, secs=15, what='capture thread abandoned the foreign stream and reconnected')
wait_for(lambda: entry['last_data'] > time.time() - 1, what='streaming again')
print("8 child death with live port: stream abandoned and restarted OK")
# 9. same satellite reopened within one second must not overwrite
set_sched(137100000, "NOAA 19", True); expect_cmd(137100000)
wait_for(lambda: entry['is_recording'], what='rec')
set_sched(137100000, "Meteor-M 2-3", True)
wait_for(lambda: entry['wav_path'] and 'Meteor' in entry['wav_path'], what='split')
set_sched(137100000, "NOAA 19", True)
wait_for(lambda: entry['wav_path'] and 'NOAA_19' in entry['wav_path'], what='split back')
set_sched(137620000, "NOAA 15 (idle)", False); expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'], what='end')
names = [os.path.basename(p) for p in wavs()]
assert len(names) == len(set(names)) and len([n for n in names if n.startswith('NOAA_19_')]) >= 3, names
print("9 unique names on fast re-split:", [n for n in names if 'NOAA_19' in n])
print("ALL RADIO TESTS PASSED")
for c in children: c.kill()
os._exit(0)
