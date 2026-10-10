"""decode.py: filename dispatch and SatDump noaa_apt invocation paths
(fake satdump binary; POSIX shell required).

Run: python -u tests/test_decode.py
"""
import os
import sys
import tempfile

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
# DSB-receive satellites (APT off): refused without running satdump
ok, png, err = decode.decode_recording("NOAA_18_20261007_215400.wav")
assert not ok and 'DSB' in err, (ok, err)
ok, png, err = decode.decode_recording("NOAA_19_20261007_215400_00000991.wav")
assert not ok and 'DSB' in err, (ok, err)
print("filename dispatch OK")

# fake satdump: logs its args; on FAKE_OK=1 writes product PNGs into the
# output directory (4th positional arg) so the composite pickup succeeds.
# Works behind 'unshare -rn' too (unshare execs it with the same $@).
FAKE = r'''#!/bin/sh
echo "$@" > "$FAKE_LOG"
outdir="$4"
if [ "$FAKE_OK" = "1" ]; then
  mkdir -p "$outdir"
  comp="$outdir/avhrr_3_rgb_MCIR_(Uncalibrated).png"
  printf 'PNG' > "$comp"
  dd if=/dev/zero bs=1 count=997 seek=3 >> "$comp" 2>/dev/null
  printf 'PNG' > "$outdir/raw_sync.png"
  exit 0
fi
echo "Error: could not find sync frames" >&2
exit 1
'''
d = tempfile.mkdtemp()
# Hermetic: never read the real TLE cache (the decode seeds SatDump's
# TLE file from it) — a missing cache is skipped silently
decode.TLE_CACHE_FILE = os.path.join(d, 'no_such_tle_cache.json')
fakebin = os.path.join(d, 'bin'); os.mkdir(fakebin)
exe = os.path.join(fakebin, 'satdump'); open(exe, 'w').write(FAKE); os.chmod(exe, 0o755)
os.environ['PATH'] = fakebin + ':' + os.environ['PATH']
log = os.path.join(d, 'args'); os.environ['FAKE_LOG'] = log
ORIG_PATH = os.environ['PATH']   # the missing-binary test clobbers it
def touch(n):
    p = os.path.join(d, n); open(p, 'wb').write(b'RIFF'); return p

# unknown satellite: decodes without --satellite_number (no orbit -> no overlay)
os.environ['FAKE_OK'] = '1'
ok, png, err = decode.decode_recording(touch("Mission_X_20261007_215400.wav"))
args = open(log).read()
assert ok and png.endswith('.png') and os.path.exists(png), (ok, err, args)
assert 'audio_wav' in args and '--satellite_number' not in args, (ok, err, args)
# known NOAA: orbit number for the overlay + start timestamp from the name
ok, png, err = decode.decode_recording(touch("NOAA_15_20261007_215400.wav"))
args = open(log).read()
assert ok and '--satellite_number 15' in args and '--start_timestamp' in args, (ok, err, args)
# stale PNG must not count as success when the decoder fails
os.environ['FAKE_OK'] = '0'
wav = touch("NOAA_15_20261007_215400.wav"); stale = wav[:-4] + '.png'; open(stale, 'wb').write(b'old')
# force: the success marker from the run above must not short-circuit this
# deliberately-failing re-run of the same file
ok, png, err = decode.decode_recording(wav, force=True)
assert not ok and not os.path.exists(stale) and 'sync frames' in err, (ok, err, os.path.exists(stale))
# Meteor refused without running satdump (no raw IQ)
os.remove(log)
ok, png, err = decode.decode_recording(touch("Meteor-M_2-4_20261007_215400.wav"))
assert not ok and 'LRPT' in err and not os.path.exists(log)
# ISS without the sstv package: clear error, no crash
ok, png, err = decode.decode_recording(touch("ISS_(Zarya)_20261007_215400.wav"))
# no sstv package -> 'not installed'; package present -> clean decode
# error on the malformed fixture. Both are valid no-crash failures.
assert not ok and 'sstv' in err.lower(), err
# missing binary: clean error
os.environ['PATH'] = '/nonexistent'
ok, png, err = decode.decode_recording(touch("NOAA_15_20261007_215400.wav"), force=True)
assert not ok and 'not installed' in err, err
print("decode_recording paths OK")

# ---- decode-attempt markers: failures persist, short-circuit, and can be forced ----
os.environ['PATH'] = fakebin + ':' + ORIG_PATH   # restore: the fake needs mkdir etc. on PATH
d2 = tempfile.mkdtemp()
wav2 = os.path.join(d2, "NOAA_18_20261007_215400.wav"); open(wav2, 'wb').write(b'RIFF')
ok, png, err = decode.decode_recording(wav2)
assert not ok and 'DSB' in err, (ok, err)
m = decode.read_decode_marker(wav2)
assert m is not None and not m['success'] and 'IQ' in m['message'], m
# second call returns the marker message without touching the filesystem again
ok, png, err = decode.decode_recording(wav2)
assert not ok and 'already attempted' in err and 'IQ' in err, (ok, err)
# success also persists: fake satdump writes the PNG once; later calls
# answer from the marker without re-running the decoder
os.environ['FAKE_OK'] = '1'
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
os._exit(0)
