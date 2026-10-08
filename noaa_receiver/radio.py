"""SDR threads: per-dongle rtl_tcp capture, waterfall FFT, FM demod, WAV recording.

All attached dongles run on the same frequency with the same settings (they
re-tune together via state.current_frequency). Dongles are pinned by SERIAL
(USB indices shift when devices are (re)plugged), and the configured primary
dongle feeds the live audio stream and records WAVs during passes. Every
dongle gets its own waterfall buffer and signal strength, so receive quality
can be compared on the dashboard.

Each dongle is served by a persistent rtl_tcp process bound to 127.0.0.1
(one port per dongle): the capture threads read the IQ stream from the
socket and retune by sending rtl_tcp frequency commands — the stream keeps
flowing while the tuner re-locks, so a frequency switch takes milliseconds
and does not restart the process, gap the audio, or split the recording.
"""
import os
import re
import socket
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime

from . import state
from .calibration import (AIS_DONGLE_SN, FM_BAND, PRIMARY_DONGLE_SN,
                          correction_info, tuning_correction)
from .config import (AUDIO_RATE, DOPPLER_APPLY_RANGE_HZ, DECIMATION, FFT_SIZE,
                     IQ_BLOCK, IQ_RECORD_FREQS, LOGDIR, RECORD_DIR, RTL_LOG,
                     SAT_DSB_DEMOD_BW_HZ, SAT_DSB_FREQ, SDR_GAIN,
                     SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS)

from .decode import sat_short_name
from .dsp import doppler_shift, fm_demodulate, frequency_shift, iq_to_complex, new_state

# Waterfall/signal FFT cadence: compute the FFT only every Nth IQ block. The
# dashboard draws ~5 rows/s, so ~59 rows/s (469 blocks/s / 8) is still 10x
# oversampled. At full block rate the FFT + row conversion (~1 ms/block on
# the Pi) starved the demod once two dongles were attached.
FFT_EVERY = 8

# Retry interval for a WAV that could not be opened/written (disk full etc.)
WAV_RETRY_SECS = 10.0

_fft_window = None

def _get_fft_window():
    """FFT_SIZE-point Hamming window, computed once."""
    global _fft_window
    if _fft_window is None:
        import numpy as np
        _fft_window = np.hamming(FFT_SIZE)
    return _fft_window

def _rtl_pll_failed(log_path):
    """True if the tuner logged 'PLL not locked' since the log was truncated.

    Checked once from the capture loop a few seconds after rtl_tcp started
    (not by sleeping before the loop: every second spent waiting is a
    second of IQ backlog in rtl_tcp's buffers, i.e. live-audio lag).
    Note: on some R820T dongles this lock bit is unreliable — the tuner
    receives fine while never confirming lock. The receiver therefore only
    warns and keeps running; the per-frequency mistune that such dongles
    show is handled per dongle by SDR_DONGLE_CORRECTIONS.
    """
    try:
        with open(log_path, 'r', errors='replace') as f:
            return 'PLL not locked' in f.read()
    except OSError:
        return False

PLL_CHECK_AFTER_SECS = 3.0

# ---------- rtl_tcp control protocol ----------
# rtl_tcp exposes a dongle over TCP: a continuous IQ stream plus a tiny
# command channel (1-byte command + 4-byte big-endian value). Frequency
# changes apply while the stream keeps flowing, so a retune is one socket
# write instead of the old kill-restart-sleep cycle (~8 s of dead air).
RTL_TCP_SET_FREQ = 0x01
RTL_TCP_SET_GAIN_MODE = 0x03   # 0 = tuner AGC, 1 = manual
RTL_TCP_SET_GAIN = 0x04        # value = gain in tenths of dB

def _rtl_tcp_set(sock, cmd, value):
    """Send one rtl_tcp control command: 1 byte command + uint32 big-endian
    ('!BI' is network byte order, standard sizes, no padding: 5 bytes)."""
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f'rtl_tcp command value out of range: {value}')
    sock.sendall(struct.pack('!BI', cmd, value))

def _port_in_use(port):
    """True if something already accepts connections on 127.0.0.1:port.

    Checked right before spawning rtl_tcp: a stale rtl_tcp (or anything
    else) on the port would make our child fail to bind and exit while the
    capture loop happily reads the FOREIGN stream — the post-handshake
    liveness check cannot catch that reliably, because connect() succeeds
    before our child has even tried to bind.
    """
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False

def kill_stale_rtl_tcp():
    """Kill rtl_tcp processes left over from a previous server run.

    Killing the server does not kill its rtl_tcp children (they exit only
    with their parent's pipe, and their stdout is DEVNULL) — they survive,
    keep the dongles claimed AND keep their ports bound, so a fresh server's
    own rtl_tcp would silently fail to bind and the capture threads would
    talk to the stale processes. Called once at startup, before any capture
    thread starts.
    """
    try:
        # -9: rtl_tcp traps SIGTERM and only exits its poll loop later —
        # a streaming rtl_tcp reliably ignores plain SIGTERM (verified live).
        # -x: match the process name exactly, not any command line that
        # merely mentions rtl_tcp (an editor, a grep, this server's logs).
        r = subprocess.run(['pkill', '-9', '-x', 'rtl_tcp'], capture_output=True)
        if r.returncode == 0:
            time.sleep(1.0)   # let the kernel release the devices and ports
            state.log_console("Killed stale rtl_tcp processes from a previous run", "warn")
    except FileNotFoundError:
        pass   # pkill not available (non-Linux) — capture threads will surface any conflict

def _recv_exact(sock, n):
    """Read exactly n bytes from the socket; None when the stream ends."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

class _IQReader:
    """Block reader for the rtl_tcp IQ stream.

    One recv() takes whatever has arrived (up to `chunk` bytes — it does
    not wait for the chunk to fill, so no latency is added) and the capture
    loop slices IQ_BLOCK-sized blocks off it: ~16x fewer syscalls than one
    recv() per 1 kB block. read_block() returns None when the stream ends.
    """
    def __init__(self, sock, block, chunk=65536):
        self.sock, self.block, self.chunk = sock, block, chunk
        self.buf, self.pos = b'', 0

    def read_block(self):
        buf, pos, block = self.buf, self.pos, self.block
        if len(buf) - pos < block:
            buf = buf[pos:]
            while len(buf) < block:
                data = self.sock.recv(self.chunk)
                if not data:
                    return None
                buf += data
            pos = 0
            self.buf = buf
        self.pos = pos + block
        return buf[pos:pos + block]

_dup_serial_warned = set()

def enumerate_dongles():
    """Detect attached RTL-SDR dongles and register them in state.sdrs,
    keyed by serial.

    rtl_sdr prints every attached device on stderr before opening one
    ("Found N device(s):" followed by "idx: label, tuner, SN: serial"), so a
    short probe run is enough — probing a nonexistent index (-d 99) fails
    right after the listing without claiming any dongle. The primary
    dongle's entry shares the legacy state.waterfall_buffer globals so the
    rest of the system (status, live audio, recording) keeps working.
    """
    err = ''
    try:
        p = subprocess.run(['rtl_sdr', '-d', '99', '-f', '100000000', '-n', '1', '-'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        err = p.stderr.decode('utf-8', 'replace')
    except Exception as e:
        state.log_console(f"Dongle enumeration failed: {e}", "error")
        return
    found = re.findall(r'^\s*(\d+):\s*([^,\n]+),\s*([^,\n]+),\s*SN:\s*(\S+)\s*$',
                       err, re.M)
    if not found:
        return  # probe failed (e.g. all dongles busy mid-restart) — keep the known set
    serials = [serial.strip() for _, _, _, serial in found]
    # Decide the primary dongle once, at the first successful enumeration
    if state.primary_serial is None:
        if PRIMARY_DONGLE_SN and PRIMARY_DONGLE_SN in serials:
            state.primary_serial = PRIMARY_DONGLE_SN
        else:
            # The dedicated AIS dongle (if configured) never becomes primary
            selectable = [sn for sn in serials if sn != AIS_DONGLE_SN] or serials
            state.primary_serial = selectable[0]
            if PRIMARY_DONGLE_SN:
                state.log_console(f"Configured primary dongle {PRIMARY_DONGLE_SN} not found — using {state.primary_serial}", "warn")
        if AIS_DONGLE_SN and AIS_DONGLE_SN in serials:
            state.log_console(f'Dongle {AIS_DONGLE_SN} dedicated to AIS (ship traffic) — excluded from satellite tracking')
    for idx_s, label, tuner, serial in found:
        serial = serial.strip()
        if not serial:
            state.log_console("Dongle without a serial found — cannot pin it across replugs, skipping", "warn")
            continue
        if serial in state.sdrs:
            if serials.count(serial) > 1 and serial not in _dup_serial_warned:
                _dup_serial_warned.add(serial)
                state.log_console(f"Several dongles report serial {serial} — only one can be driven (rtl_tcp -d picks the first); give each a unique serial with rtl_eeprom -s", "warn")
            continue
        primary = serial == state.primary_serial
        if primary:
            waterfall, lock = state.waterfall_buffer, state.waterfall_lock
        else:
            waterfall = deque(maxlen=WATERFALL_ROWS)
            lock = threading.Lock()
        state.sdrs[serial] = {
            'label': label.strip(), 'tuner': tuner.strip(), 'serial': serial,
            'primary': primary,
            'ais': serial == AIS_DONGLE_SN,
            'waterfall': waterfall, 'lock': lock,
            'signal': 0.0, 'proc': None, 'last_data': 0.0,
            # rtl_tcp port for this dongle (localhost-bound, one per dongle)
            'port': 1235 + len(state.sdrs),
            # Live audio ring for this dongle's demodulated audio
            'la': {'data': [], 'base': 0, 'total': 0, 'cond': threading.Condition()},
            # WAV recording state (every dongle records its own file)
            'is_recording': False, 'wav': None, 'wav_path': None, 'iq': None,
        }
        state.log_console(f"🔌 Dongle {idx_s.strip()}: {label.strip()} ({tuner.strip()}), SN {serial}" + (" — primary" if primary else ""))

def sdr_thread():
    """Enumerate attached dongles and keep one capture thread per dongle.

    Re-enumerates periodically so dongles plugged in later are picked up
    without a restart. A capture thread whose dongle is unplugged simply
    retries until it comes back. Also acts as a stall watchdog: a dongle
    whose rtl_tcp process is alive but stops delivering IQ data (flaky USB
    device) gets its process killed so the capture thread restarts it.
    """
    threads = {}
    while True:
        enumerate_dongles()
        for sn, entry in state.sdrs.items():
            t = threads.get(sn)
            if t is None or not t.is_alive():
                if entry.get('ais'):
                    from .ais import ais_capture_thread
                    t = threading.Thread(target=ais_capture_thread, args=(sn,),
                                         daemon=True, name=f'ais-{sn}')
                else:
                    t = threading.Thread(target=sdr_capture_thread, args=(sn,),
                                         daemon=True, name=f'sdr-{sn}')
                t.start()
                threads[sn] = t
                continue
            proc = entry['proc']
            if (proc is not None and proc.poll() is None
                    and entry['last_data'] > 0
                    and time.time() - entry['last_data'] > 30):
                state.log_console(f"Dongle {sn} stalled (no IQ data for {int(time.time() - entry['last_data'])}s) — restarting rtl_tcp", "warn")
                entry['last_data'] = time.time()
                try:
                    proc.kill()
                except Exception:
                    pass
        time.sleep(10)

def sdr_capture_thread(serial):
    """Capture loop for one dongle (pinned by serial via rtl_tcp -d):
    rtl_tcp IQ stream → FFT waterfall + FM demod + live audio; WAV
    recording during passes (every dongle records its own file)."""
    import numpy as np
    entry = state.sdrs[serial]
    primary = entry['primary']
    rtl_log_path = RTL_LOG if primary else os.path.join(LOGDIR, f'rtl_sdr_{serial}.log')
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    rtl_log_f = open(rtl_log_path, 'w')
    pll_warned = False
    # This dongle's own DSP chain state — demod filters, rotator and NCO
    # phases must not be shared across the concurrently demodulating threads
    dst = new_state()
    restart_backoff = 5.0
    # Recording failures (disk full, permissions) must not take the receiver
    # down: the WAV open is retried at most every WAV_RETRY_SECS
    wav_retry_at = 0.0

    def set_primary_recording(path):
        if primary:
            with state.status_lock:
                state.current_wav = None
                state.current_wav_path = path
                state.is_recording = path is not None

    def close_wav():
        """Finish this dongle's current WAV (pass end, band or pass switch)."""
        wav, path = entry['wav'], entry['wav_path']
        iq = entry['iq']
        entry['wav'] = None
        entry['wav_path'] = None
        entry['iq'] = None
        entry['is_recording'] = False
        set_primary_recording(None)
        if iq is not None:
            try:
                iq.close()
            except Exception:
                pass
        if wav is None:
            return
        try:
            wav.close()
            state.log_console(f"🎬 Recording stopped (dongle {serial}): {path}")
        except Exception as e:
            state.log_console(f"WAV close error (dongle {serial}): {e}", "error")

    def open_wav(sat_name):
        """Start a WAV for sat_name; returns False (and arms the retry
        timer) if the file cannot be opened."""
        nonlocal wav_retry_at
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "" if primary else f"_{serial}"
        path = os.path.join(RECORD_DIR, f"{sat_short_name(sat_name)}_{timestamp}{suffix}.wav")
        # Never overwrite: a WAV split and reopened within the same second
        # (fast A -> B -> A pass switch) gets the same timestamp
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(RECORD_DIR, f"{sat_short_name(sat_name)}_{timestamp}-{n}{suffix}.wav")
        try:
            wav = wave.open(path, 'wb')
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(AUDIO_RATE)
        except Exception as e:
            wav_retry_at = time.time() + WAV_RETRY_SECS
            state.log_console(f"Cannot open recording {path} (dongle {serial}): {e} — retrying in {WAV_RETRY_SECS:.0f}s", "error")
            return False
        entry['wav'] = wav
        entry['wav_path'] = path
        entry['is_recording'] = True
        # Digital modes (DSB, LRPT) on this frequency also capture the raw
        # IQ baseband next to the audio — the demod audio cannot carry them
        entry['iq'] = None
        if tuned_freq in IQ_RECORD_FREQS:
            iq_path = path[:-len('.wav')] + '.iq.u8'
            try:
                entry['iq'] = open(iq_path, 'wb', buffering=1 << 20)
                state.log_console(f"📡 IQ recording started (dongle {serial}): {iq_path}")
            except Exception as e:
                state.log_console(f"IQ recording could not start (dongle {serial}): {e}", "warn")
        set_primary_recording(path)
        state.log_console(f"🎬 Recording started (dongle {serial}): {path}")
        return True

    while True:
        run_started = time.time()
        proc = None
        sock = None
        try:
            with state.status_lock:
                tuned_freq = state.manual_dongle_freq.get(serial, state.current_frequency)
            correction = tuning_correction(tuned_freq, serial)
            # shown in the dongle cards' tuning infobox
            entry['correction'], entry['correction_src'] = correction_info(tuned_freq, serial)
            freq_str = f"{tuned_freq + SDR_OFFSET_HZ + correction}"
            record_sat = None
            if _port_in_use(entry['port']):
                raise RuntimeError(f'port {entry["port"]} is already in use (stale rtl_tcp? another server?) — not starting rtl_tcp for dongle {serial}')
            # Fresh log per rtl_tcp start so the PLL check below only sees
            # messages from the process it is validating
            rtl_log_f.seek(0)
            rtl_log_f.truncate()
            # rtl_tcp serves this dongle on a localhost port; the capture
            # loop below reads the IQ stream from the socket and retunes
            # with in-band commands (no process restart on band changes)
            proc = subprocess.Popen(
                ['rtl_tcp', '-a', '127.0.0.1', '-p', str(entry['port']), '-d', serial,
                 '-f', freq_str, '-s', str(SDR_RATE), '-g', str(SDR_GAIN)],
                stdout=subprocess.DEVNULL, stderr=rtl_log_f
            )
            entry['proc'] = proc
            entry['last_data'] = time.time()
            # Wait for the data port and read the handshake ('RTL0' magic +
            # tuner type + gain count). rtl_tcp opens the dongle before it
            # listens, so an open port means the device is claimed and set.
            deadline = time.time() + 10
            sock = None
            while time.time() < deadline:
                try:
                    sock = socket.create_connection(('127.0.0.1', entry['port']), timeout=2)
                    break
                except OSError:
                    if proc.poll() is not None:
                        break  # rtl_tcp exited (dongle unplugged) — restart path
                    time.sleep(0.2)
            if sock is None:
                raise RuntimeError(f'rtl_tcp did not open port {entry["port"]}')
            # The IQ stream delivers a block every ~2 ms; 30 s of silence is
            # a dead stream (same threshold as the stall watchdog in
            # sdr_thread — whichever fires first, the result is a restart).
            sock.settimeout(30)
            header = _recv_exact(sock, 12)
            if header is None or header[:4] != b'RTL0':
                raise RuntimeError('bad rtl_tcp handshake')
            if proc.poll() is not None:
                # Our child died (e.g. it could not bind its port because a
                # stale rtl_tcp still holds it) and something else is
                # serving that port — do NOT silently use the foreign
                # stream; restart loudly instead.
                raise RuntimeError('rtl_tcp exited immediately (port conflict? device busy?)')
            tuner_type, gain_count = struct.unpack('!II', header[4:12])
            # Fixed tuner gain (per dongle, see calibration.SDR_DONGLE_GAIN):
            # manual gain mode + gain in tenths of dB over the control
            # protocol — the rtl_tcp CLI parses -g as an int, so fractional
            # gain steps like the R820T's 29.7 dB must go through the socket
            gain_db = SDR_DONGLE_GAIN.get(serial, SDR_GAIN)
            if gain_db:
                _rtl_tcp_set(sock, RTL_TCP_SET_GAIN_MODE, 1)
                _rtl_tcp_set(sock, RTL_TCP_SET_GAIN, int(round(gain_db * 10)))
                state.log_console(f"Tuner gain set (dongle {serial}): {gain_db} dB manual")
            if primary:
                state.rtl_sdr_proc = proc
                state.log_console(f"rtl_tcp started (pid {proc.pid}, primary dongle {serial}, tuner type {tuner_type}, {gain_count} gain steps), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={SDR_DONGLE_GAIN.get(serial, SDR_GAIN) or 'auto'}dB")
            else:
                state.log_console(f"rtl_tcp started (pid {proc.pid}, dongle {serial}, tuner type {tuner_type}), tuned {freq_str}Hz")
            pll_check_at = None if pll_warned else time.time() + PLL_CHECK_AFTER_SECS
            last_history_append = 0.0
            last_proc_check = time.time()
            block_count = 0
            reader = _IQReader(sock, IQ_BLOCK)

            while True:
                raw = reader.read_block()
                if entry['iq'] is not None:
                    try:
                        entry['iq'].write(raw)
                    except Exception as e:
                        state.log_console(f"IQ write error (dongle {serial}): {e} — stopping the IQ capture", "error")
                        try: entry['iq'].close()
                        except Exception: pass
                        entry['iq'] = None
                if raw is None:
                    state.log_console(f"rtl_tcp stream ended (dongle {serial}), restarting...", "warn")
                    break
                now_ts = time.time()
                entry['last_data'] = now_ts
                if now_ts - last_proc_check >= 1.0:
                    last_proc_check = now_ts
                    if proc.poll() is not None:
                        # Data keeps flowing although OUR rtl_tcp is gone:
                        # the socket is connected to a foreign listener on
                        # this port (see _port_in_use) — never use it.
                        raise RuntimeError(f'rtl_tcp (pid {proc.pid}) exited but port {entry["port"]} still streams — foreign rtl_tcp on this port')
                    if pll_check_at is not None and now_ts >= pll_check_at:
                        pll_check_at = None
                        if _rtl_pll_failed(rtl_log_path):
                            pll_warned = True
                            state.log_console(f"Dongle {serial}: R820T PLL lock not confirmed — continuing anyway (lock bit unreliable on this dongle, mistune handled per dongle via SDR_DONGLE_CORRECTIONS)", "warn")

                # One consistent snapshot of the scheduler's state per block.
                # Reading the frequency, satellite name and pass flag in
                # separate lock sections let a pass switch land between
                # them — the WAV then got the NEW satellite's name while the
                # dongle stayed on the OLD frequency, and the retune test
                # below never fired again for that pass.
                with state.status_lock:
                    override = serial in state.manual_dongle_freq
                    freq_now = state.manual_dongle_freq.get(serial, state.current_frequency)
                    sat_now = state.current_sat_name
                    pass_active = state.is_pass_active
                    rec_paused = state.recordings_paused
                    dop_hz, dop_freq = state.doppler_hz, state.doppler_freq_hz
                    bw_hz = state.manual_dongle_bw.get(serial)

                # Offset-shift the baseband once: satellite to 0 Hz, DC spike
                # displaced to +SDR_OFFSET_HZ. Shared by waterfall FFT and demod.
                c = iq_to_complex(raw)
                c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE, dst)

                # Live Doppler correction (scheduler-computed during passes):
                # rotate the satellite's Doppler-drifted carrier into the demod
                # center in software. Applied only while THIS dongle's current
                # tune (tuned_freq — not the frequency it started on) is near
                # the tracked satellite's frequency; otherwise the NCO runs at
                # 0 Hz, which keeps its phase instead of dropping it.
                if abs(tuned_freq - dop_freq) >= DOPPLER_APPLY_RANGE_HZ:
                    dop_hz = 0
                c = doppler_shift(c, dop_hz, SDR_RATE, dst)

                # FFT for waterfall + signal strength, every FFT_EVERY-th
                # block (see FFT_EVERY — CPU budget, not display needs)
                block_count += 1
                if block_count % FFT_EVERY == 0:
                    try:
                        if len(c) >= FFT_SIZE:
                            window = _get_fft_window()
                            windowed = c[:FFT_SIZE] * window
                            fft_result = np.fft.fftshift(np.fft.fft(windowed))
                            magnitude = np.abs(fft_result)
                            center = len(magnitude) // 2
                            band = float(magnitude[center-10:center+10].mean())
                            with entry['lock']:
                                entry['signal'] = band
                            if primary:
                                with state.signal_lock:
                                    state.signal_strength = band
                                    if entry['is_recording']:
                                        state.pass_signal_peak = max(state.pass_signal_peak, band)
                                    if now_ts - last_history_append >= 1.0:
                                        last_history_append = now_ts
                                        state.signal_history.append(band)
                            # Display normalization by the row max. The DC
                            # spike at +SDR_OFFSET_HZ is a mild artifact on
                            # both current dongles (~2x the noise floor,
                            # never the row max - measured 2026-10-08 from
                            # raw IQ), so it needs no special-casing; the
                            # blanking that used to live here existed for
                            # the removed FC0013, whose spike saturated at
                            # 7x its strongest signal.
                            peak = magnitude.max()
                            if peak > 0:
                                magnitude *= 255.0 / peak
                            row = magnitude.astype(int).tolist()
                            with entry['lock']:
                                entry['waterfall'].append(row)
                    except Exception:
                        pass

                # Retune when this dongle's target frequency changes (idle
                # park -> pass band, e.g. 137 MHz -> 437 MHz ISS; a manual
                # override set or cleared): one rtl_tcp command on the open
                # stream — the tuner re-locks in milliseconds and the IQ
                # keeps flowing, so there is no restart gap and the live
                # audio does not cut out. A running WAV is closed first so
                # one file never spans two bands. A pass switch between two
                # satellites SHARING a frequency (NOAA 19 / Meteor-M 2-3 at
                # 137.1, NOAA 18 / Meteor-M 2-4 at 137.9125) needs no
                # retune, but the WAV is closed and reopened all the same so
                # one file never spans two passes.
                switch_band = freq_now != tuned_freq
                switch_sat = entry['is_recording'] and sat_now != record_sat
                if switch_band or switch_sat:
                    if entry['is_recording']:
                        close_wav()
                    if switch_band:
                        new_corr = tuning_correction(freq_now, serial)
                        _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, freq_now + SDR_OFFSET_HZ + new_corr)
                        entry['correction'], entry['correction_src'] = correction_info(freq_now, serial)
                        if primary:
                            state.log_console(f"Retuning (live): {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                        tuned_freq = freq_now
                    elif primary:
                        state.log_console(f"Pass switch on the same frequency: {record_sat} -> {sat_now}")
                    record_sat = None

                # Record to WAV during passes — every dongle records its own
                # file (suffixed with its serial); the primary additionally
                # mirrors its state into the legacy globals used by the
                # status/handler/scheduler. A dongle with a manual frequency
                # override is parked on the operator's frequency — recording
                # it would fill a satellite-named WAV with the wrong band.
                # The dashboard's global pause (rec_paused) skips the WAV
                # entirely: mid-pass it closes a running file within one
                # block, and no new one opens until it is cleared.
                should_record = pass_active and not override and not rec_paused
                if should_record and not entry['is_recording']:
                    if now_ts >= wav_retry_at and open_wav(sat_now):
                        record_sat = sat_now
                elif not should_record and entry['is_recording']:
                    close_wav()

                # FM-demodulate every block (every dongle: each feeds its own
                # live audio ring and its own WAV). Broadcast FM (manual radio
                # test tunes) deviates ±75 kHz, so the whole ±120 kHz capture
                # band is demodulated and only the audio is low-passed;
                # satellite APT/SSTV keeps the narrow 22 kHz.
                # Demod channel width = what the WAV and live audio carry:
                # the manual per-dongle override if set (bw_hz), else the
                # mode default (full ±120 kHz on broadcast FM, 22 kHz on
                # satellite modes). fm_demodulate skips the filter for a
                # falsy cutoff (full band).
                try:
                    if FM_BAND[0] <= freq_now <= FM_BAND[1]:
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz, audio_cutoff_hz=18000, st=dst)
                    else:
                        # Auto demod width: DSB receive frequencies carry a
                        # narrowband digital stream — 6 kHz instead of the
                        # 22 kHz APT default tightens the recording SNR
                        auto_bw = SAT_DSB_DEMOD_BW_HZ if freq_now in SAT_DSB_FREQ.values() else 22000
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz or auto_bw, st=dst)
                except Exception:
                    audio = b''
                if audio:
                    state.push_live_audio(entry, audio)
                    if entry['wav'] is not None:
                        try:
                            entry['wav'].writeframes(audio)
                        except Exception as e:
                            # Disk full etc.: stop this file instead of
                            # logging 469 errors/s; the open is retried
                            # after WAV_RETRY_SECS while the pass lasts
                            state.log_console(f"WAV write error (dongle {serial}): {e} — closing {entry['wav_path']}", "error")
                            close_wav()
                            wav_retry_at = now_ts + WAV_RETRY_SECS

        except Exception as e:
            state.log_console(f"SDR thread error (dongle {serial}): {e}", "error")
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)   # reap it, and be sure the dongle/port are released before the restart
            except Exception:
                pass
        if sock is not None:
            try: sock.close()
            except: pass
        close_wav()
        # Backoff when rtl_tcp keeps dying immediately (dongle unplugged,
        # flaky USB): 5 s doubling up to 60 s; reset after a stable run
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 60.0)
        time.sleep(restart_backoff)
