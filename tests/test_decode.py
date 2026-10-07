"""decode.py: filename dispatch and noaa-apt invocation paths (fake noaa-apt binary).

Run: python3 tests/test_decode.py
"""
import os, stat, sys, tempfile
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from noaa_receiver import decode
from noaa_receiver.radio import sat_short_name as radio_name   # radio must use the same naming

cases = {
    "NOAA_19_20261007_215400.wav": "NOAA 19",
    "NOAA_19_20261007_215400-2.wav": "NOAA 19",
    "NOAA_18_20261007_215400_00000991.wav": "NOAA 18",
    "ISS_(Zarya)_20261007_215400_77771111153705700.wav": "ISS (Zarya)",
    "Meteor-M_2-3_20261007_215400.wav": "Meteor-M 2-3",
    "NOAA_15_idle_20261007_215400.wav": "NOAA 15",
    "Mission_X_20261007_215400.wav": None,
    "swiss_sat_20261007_215400.wav": None,   # contains 'iss' — must NOT route to SSTV
    "random.wav": None,
}
for name, want in cases.items():
    got = decode.satellite_from_filename(name)
    assert got == want, (name, got, want)
assert radio_name is decode.sat_short_name
# DSB-receive satellites (APT off): refused without running noaa-apt
ok, png, err = decode.decode_recording("NOAA_18_20261007_215400.wav")
assert not ok and 'DSB' in err, (ok, err)
ok, png, err = decode.decode_recording("NOAA_19_20261007_215400_00000991.wav")
assert not ok and 'DSB' in err, (ok, err)
print("filename dispatch OK")

FAKE = r'''#!/bin/sh
# fake noaa-apt: logs its args; writes the -o file when FAKE_OK=1
echo "$@" > "$FAKE_LOG"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
if [ "$FAKE_OK" = "1" ]; then printf 'PNG' > "$out"; exit 0; fi
echo "Error: could not find sync frames" >&2; exit 1
'''
d = tempfile.mkdtemp()
# Hermetic: never depend on (or touch) a real /var/log/noaa/weather.txt —
# on the Pi that file exists and -T would legitimately be passed
decode.NOAA_APT_TLE_FILE = os.path.join(d, 'no_such_weather.txt')
decode.NOAA_APT_DIR = d
fakebin = os.path.join(d, 'bin'); os.mkdir(fakebin)
exe = os.path.join(fakebin, 'noaa-apt'); open(exe, 'w').write(FAKE); os.chmod(exe, 0o755)
os.environ['PATH'] = fakebin + ':' + os.environ['PATH']
log = os.path.join(d, 'args'); os.environ['FAKE_LOG'] = log
def touch(n):
    p = os.path.join(d, n); open(p, 'wb').write(b'RIFF'); return p

# unknown satellite: runs without map overlay and without -s
os.environ['FAKE_OK'] = '1'
ok, png, err = decode.decode_recording(touch("Mission_X_20261007_215400.wav"))
args = open(log).read()
assert ok and png.endswith('.png') and '-m no' in args and '-s' not in args, (ok, err, args)
# known NOAA (APT healthy): map overlay + -s; no -T since the TLE file does not exist here
ok, png, err = decode.decode_recording(touch("NOAA_15_20261007_215400.wav"))
args = open(log).read()
assert ok and '-s noaa_15' in args and '-m yes' in args and '-T' not in args, (ok, err, args)
# stale PNG must not count as success when the decoder fails
os.environ['FAKE_OK'] = '0'
wav = touch("NOAA_15_20261007_215400.wav"); stale = wav[:-4] + '.png'; open(stale, 'wb').write(b'old')
# force: the success marker from the run above must not short-circuit this
# deliberately-failing re-run of the same file
ok, png, err = decode.decode_recording(wav, force=True)
assert not ok and not os.path.exists(stale) and 'sync frames' in err, (ok, err, os.path.exists(stale))
# Meteor refused without running noaa-apt
os.remove(log)
ok, png, err = decode.decode_recording(touch("Meteor-M_2-4_20261007_215400.wav"))
assert not ok and 'LRPT' in err and not os.path.exists(log)
# ISS without the sstv package: clear error, no crash
ok, png, err = decode.decode_recording(touch("ISS_(Zarya)_20261007_215400.wav"))
# no sstv package -> 'not installed'; package present -> clean decode
# error on the malformed fixture. Both are valid no-crash failures.
assert not ok and 'sstv' in err.lower(), err
# missing binary: clean error
os.environ['PATH'] = '/nonexistent'; decode.NOAA_APT_DIR = '/nonexistent'
ok, png, err = decode.decode_recording(touch("NOAA_15_20261007_215400.wav"), force=True)
assert not ok and 'could not be run' in err, err
print("decode_recording paths OK")

# ---- decode-attempt markers: failures persist, short-circuit, and can be forced ----
os.environ['PATH'] = fakebin + ':' + os.environ['PATH']; decode.NOAA_APT_DIR = d
d2 = tempfile.mkdtemp()
wav2 = os.path.join(d2, "NOAA_18_20261007_215400.wav"); open(wav2, 'wb').write(b'RIFF')
ok, png, err = decode.decode_recording(wav2)
assert not ok and 'DSB' in err, (ok, err)
m = decode.read_decode_marker(wav2)
assert m is not None and not m['success'] and 'IQ' in m['message'], m
# second call returns the marker message without touching the filesystem again
ok, png, err = decode.decode_recording(wav2)
assert not ok and 'already attempted' in err and 'IQ' in err, (ok, err)
# success also persists: fake noaa-apt writes the PNG once; later calls
# answer from the marker without re-running the decoder
os.environ['FAKE_OK'] = '1'
decode.NOAA_APT_DIR = d
wav3 = os.path.join(d2, "NOAA_15_20261007_215400.wav"); open(wav3, 'wb').write(b'RIFF')
open(log, 'w').close()
ok, png, err = decode.decode_recording(wav3)
assert ok and png and os.path.exists(png), (ok, err)
first_invocations = open(log).read()
ok2, png2, err2 = decode.decode_recording(wav3)
assert ok2 and png2 == png, (ok2, err2)
assert open(log).read() == first_invocations   # marker short-circuit: decoder NOT re-run
# force=True re-runs the decoder and overwrites the marker
os.remove(log)   # the fake overwrites the log with identical args: absence proves re-invocation
ok3, png3, err3 = decode.decode_recording(wav3, force=True)
assert ok3 and os.path.exists(log), (ok3, err3)
print("decode markers OK")
