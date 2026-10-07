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
from .calibration import (FM_BAND, PRIMARY_DONGLE_SN, correction_info,
                          tuning_correction)
from .config import (AUDIO_RATE, DOPPLER_APPLY_RANGE_HZ, DECIMATION, FFT_SIZE,
                     IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SDR_GAIN,
                     SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS)

from .dsp import doppler_shift, fm_demodulate, frequency_shift, iq_to_complex, new_state

# Waterfall/signal FFT cadence: compute the FFT only every Nth IQ block. The
# dashboard draws ~5 rows/s, so ~59 rows/s (469 blocks/s / 8) is still 10x
# oversampled. At full block rate the FFT + row conversion (~1 ms/block on
# the Pi) starved the demod once two dongles were attached.
FFT_EVERY = 8

_fft_window = None

def _get_fft_window():
    """FFT_SIZE-point Hamming window, computed once."""
    global _fft_window
    if _fft_window is None:
        import numpy as np
        _fft_window = np.hamming(FFT_SIZE)
    return _fft_window

def _rtl_pll_failed(proc):
    """True if the tuner logged 'PLL not locked' since the log was truncated.

    Note: on some R820T dongles this lock bit is unreliable — the tuner
    receives fine while never confirming lock. The receiver therefore only
    warns and keeps running; the per-frequency mistune that such dongles
    show is handled per dongle by SDR_DONGLE_CORRECTIONS.
    """
    deadline = time.time() + 3.0
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
        try:
            with open(RTL_LOG, 'r', errors='replace') as f:
                if 'PLL not locked' in f.read():
                    return True
        except OSError:
            return False
    return False

# ---------- rtl_tcp control protocol ----------
# rtl_tcp exposes a dongle over TCP: a continuous IQ stream plus a tiny
# command channel (1-byte command + 4-byte big-endian value). Frequency
# changes apply while the stream keeps flowing, so a retune is one socket
# write instead of the old kill-restart-sleep cycle (~8 s of dead air).
RTL_TCP_SET_FREQ = 0x01

def _rtl_tcp_set(sock, cmd, value):
    """Send one rtl_tcp control command."""
    sock.sendall(struct.pack('!BI', cmd, value))

def _recv_exact(sock, n):
    """Read exactly n bytes from the IQ stream; None when the stream ends."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

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
            state.primary_serial = serials[0]
            if PRIMARY_DONGLE_SN:
                state.log_console(f"Configured primary dongle {PRIMARY_DONGLE_SN} not found — using {state.primary_serial}", "warn")
    for idx_s, label, tuner, serial in found:
        serial = serial.strip()
        if not serial:
            state.log_console("Dongle without a serial found — cannot pin it across replugs, skipping", "warn")
            continue
        if serial in state.sdrs:
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
            'waterfall': waterfall, 'lock': lock,
            'signal': 0.0, 'proc': None, 'last_data': 0.0,
            # rtl_tcp port for this dongle (localhost-bound, one per dongle)
            'port': 1235 + len(state.sdrs),
            # Live audio ring for this dongle's demodulated audio
            'la': {'data': [], 'base': 0, 'total': 0, 'cond': threading.Condition()},
            # WAV recording state (every dongle records its own file)
            'is_recording': False, 'wav': None, 'wav_path': None,
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
    rtl_tcp IQ stream → FFT waterfall (+ FM demod, WAV recording during
    passes on the primary dongle)."""
    import numpy as np
    entry = state.sdrs[serial]
    primary = entry['primary']
    rtl_log_path = RTL_LOG if primary else os.path.join(LOGDIR, f'rtl_sdr_{serial}.log')
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    rtl_log_f = open(rtl_log_path, 'w')
    pll_warned = False
    # This dongle's own DSP chain state — demod filters must not share state
    # across the concurrently demodulating capture threads
    dst = new_state()
    restart_backoff = 5.0

    def close_wav():
        """Finish this dongle's current WAV (pass end or band switch)."""
        try:
            entry['wav'].close()
            state.log_console(f"🎬 Recording stopped (dongle {serial}): {entry['wav_path']}")
        except Exception as e:
            state.log_console(f"WAV close error (dongle {serial}): {e}", "error")
        entry['wav'] = None
        entry['wav_path'] = None
        entry['is_recording'] = False
        if primary:
            state.current_wav = None
            state.current_wav_path = None
            state.is_recording = False

    while True:
        run_started = time.time()
        proc = None
        sock = None
        try:
            with state.status_lock:
                tune_target = state.manual_dongle_freq.get(serial) or state.current_frequency
            correction = tuning_correction(tune_target, serial)
            # shown in the dongle cards' tuning infobox
            entry['correction'], entry['correction_src'] = correction_info(tune_target, serial)
            freq_str = f"{tune_target + SDR_OFFSET_HZ + correction}"
            tuned_freq = tune_target
            record_sat = None
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
            sock.settimeout(30)
            header = _recv_exact(sock, 12)
            if header is None or header[:4] != b'RTL0':
                raise RuntimeError('bad rtl_tcp handshake')
            if primary:
                state.rtl_sdr_proc = proc
                state.log_console(f"rtl_tcp started (pid {proc.pid}, primary dongle {serial}), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={SDR_GAIN}dB")
                if _rtl_pll_failed(proc) and not pll_warned:
                    pll_warned = True
                    state.log_console("R820T PLL lock not confirmed — continuing anyway (lock bit unreliable on this dongle, mistune handled per dongle via SDR_DONGLE_CORRECTIONS)", "warn")
            else:
                state.log_console(f"rtl_tcp started (pid {proc.pid}, dongle {serial}), tuned {freq_str}Hz")
            last_history_append = 0.0
            block_count = 0

            while True:
                raw = _recv_exact(sock, IQ_BLOCK)
                if raw is None:
                    state.log_console(f"rtl_tcp stream ended (dongle {serial}), restarting...", "warn")
                    break
                entry['last_data'] = time.time()

                # Offset-shift the baseband once: satellite to 0 Hz, DC spike
                # displaced to +SDR_OFFSET_HZ. Shared by waterfall FFT and demod.
                c = iq_to_complex(raw)
                c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE, dst)

                # Live Doppler correction (scheduler-computed during passes):
                # rotate the satellite's Doppler-drifted carrier into the demod
                # center in software — retuning rtl_sdr would need a restart
                # and gap the recording. Applied only when this dongle's
                # target is near the tracked satellite frequency.
                if (state.doppler_hz
                        and abs(tune_target - state.doppler_freq_hz) < DOPPLER_APPLY_RANGE_HZ):
                    c = doppler_shift(c, state.doppler_hz, SDR_RATE, dst)

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
                            band = magnitude[center-10:center+10].mean()
                            with entry['lock']:
                                entry['signal'] = float(band)
                            if primary:
                                with state.signal_lock:
                                    state.signal_strength = float(band)
                                    if state.is_recording:
                                        state.pass_signal_peak = max(state.pass_signal_peak, state.signal_strength)
                                now_ts = time.time()
                                if now_ts - last_history_append >= 1.0:
                                    last_history_append = now_ts
                                    with state.signal_lock:
                                        state.signal_history.append(float(band))
                            # The DC spike at +SDR_OFFSET_HZ is a fixed tuner
                            # artifact (the FC0013's saturates at 7x its
                            # strongest signal). Blank it to the noise level
                            # before display normalization: it carries no
                            # information, would otherwise dominate the row
                            # max on spike-heavy dongles (crushing the real
                            # content), and the feature overlay already
                            # marks its position.
                            bin_hz = SDR_RATE / FFT_SIZE
                            spike = center + int(SDR_OFFSET_HZ / bin_hz)
                            lo, hi = max(spike - 8, 0), min(spike + 9, len(magnitude))
                            magnitude[lo:hi] = np.median(magnitude)
                            if magnitude.max() > 0:
                                magnitude = magnitude / magnitude.max() * 255
                            row = magnitude.astype(int).tolist()
                            with entry['lock']:
                                entry['waterfall'].append(row)
                    except Exception:
                        pass

                # Retune when the scheduler moves to another satellite's
                # frequency (idle park -> pass band, e.g. 137 MHz -> 437 MHz
                # ISS): one rtl_tcp command on the open stream — the tuner
                # re-locks in milliseconds and the IQ keeps flowing, so there
                # is no restart gap and the live audio does not cut out. A
                # direct pass switch closes the running WAV first so one file
                # never spans two bands.
                with state.status_lock:
                    freq_now = state.current_frequency
                    sat_now = state.current_sat_name
                if serial in state.manual_dongle_freq:
                    freq_now = state.manual_dongle_freq[serial]
                if freq_now != tuned_freq and (not entry['is_recording'] or sat_now != record_sat):
                    if entry['is_recording']:
                        close_wav()
                    new_corr = tuning_correction(freq_now, serial)
                    _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, freq_now + SDR_OFFSET_HZ + new_corr)
                    entry['correction'], entry['correction_src'] = correction_info(freq_now, serial)
                    if primary:
                        state.log_console(f"Retuning (live): {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                    tuned_freq = freq_now
                    record_sat = None

                # Record to WAV during passes — every dongle records its own
                # file (suffixed with its serial); the primary additionally
                # mirrors its state into the legacy globals used by the
                # status/handler/scheduler.
                should_record = False
                with state.status_lock:
                    # A dongle with a manual frequency override is parked on
                    # the operator's frequency — recording it would fill a
                    # satellite-named WAV with the wrong band
                    should_record = (state.is_pass_active
                                      and serial not in state.manual_dongle_freq)

                if should_record and not entry['is_recording']:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    sat_short = state.current_sat_name.replace(" ", "_").replace("(idle)", "idle")
                    record_sat = state.current_sat_name
                    suffix = "" if primary else f"_{serial}"
                    entry['wav_path'] = os.path.join(RECORD_DIR, f"{sat_short}_{timestamp}{suffix}.wav")
                    entry['wav'] = wave.open(entry['wav_path'], 'wb')
                    entry['wav'].setnchannels(1)
                    entry['wav'].setsampwidth(2)
                    entry['wav'].setframerate(AUDIO_RATE)
                    entry['is_recording'] = True
                    if primary:
                        state.current_wav_path = entry['wav_path']
                        state.is_recording = True
                    state.log_console(f"🎬 Recording started (dongle {serial}): {entry['wav_path']}")

                elif not should_record and entry['is_recording']:
                    close_wav()

                # FM-demodulate every block (every dongle: each feeds its own
                # live audio ring and its own WAV). Broadcast FM (manual radio
                # test tunes) deviates ±75 kHz, so the whole ±120 kHz capture
                # band is demodulated and only the audio is low-passed;
                # satellite APT/SSTV keeps the narrow 22 kHz.
                try:
                    if FM_BAND[0] <= freq_now <= FM_BAND[1]:
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=None, audio_cutoff_hz=18000, st=dst)
                    else:
                        audio = fm_demodulate(c, DECIMATION, st=dst)
                except Exception:
                    audio = b''
                if audio:
                    state.push_live_audio(entry, audio)

                if entry['is_recording'] and entry['wav']:
                    try:
                        if audio:
                            entry['wav'].writeframes(audio)
                    except Exception as e:
                        state.log_console(f"WAV write error (dongle {serial}): {e}", "error")

        except Exception as e:
            state.log_console(f"SDR thread error (dongle {serial}): {e}", "error")
        try: proc.kill()
        except: pass
        if sock is not None:
            try: sock.close()
            except: pass
        if entry['wav']:
            close_wav()
        if primary:
            state.current_wav = None
            state.current_wav_path = None
            state.is_recording = False
        # Backoff when rtl_sdr keeps dying immediately (dongle unplugged,
        # flaky USB): 5 s doubling up to 60 s; reset after a stable run
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 60.0)
        time.sleep(restart_backoff)
