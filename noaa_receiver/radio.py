"""SDR threads: per-dongle rtl_sdr capture, waterfall FFT, FM demod, WAV recording.

All attached dongles run on the same frequency with the same settings (they
re-tune together via state.current_frequency). Dongles are pinned by SERIAL
(USB indices shift when devices are (re)plugged), and the configured primary
dongle feeds the live audio stream and records WAVs during passes. Every
dongle gets its own waterfall buffer and signal strength, so receive quality
can be compared on the dashboard.
"""
import os
import re
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime

from . import state
from .config import (AUDIO_RATE, DECIMATION, FFT_SIZE, FM_BAND, IQ_BLOCK, LOGDIR,
                     PRIMARY_DONGLE_SN, RECORD_DIR, RTL_LOG, SDR_GAIN, SDR_OFFSET_HZ,
                     SDR_RATE, WATERFALL_ROWS, tuning_correction)

from .dsp import fm_demodulate, frequency_shift, iq_to_complex

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
    """True if rtl_sdr logged 'PLL not locked' since the log was truncated.

    Note: on some R820T dongles this lock bit is unreliable — the tuner
    receives fine while never confirming lock. The receiver therefore only
    warns and keeps running; the per-frequency mistune that such dongles
    show is handled by SDR_FREQ_CORRECTION_HZ.
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
        }
        state.log_console(f"🔌 Dongle {idx_s.strip()}: {label.strip()} ({tuner.strip()}), SN {serial}" + (" — primary" if primary else ""))

def sdr_thread():
    """Enumerate attached dongles and keep one capture thread per dongle.

    Re-enumerates periodically so dongles plugged in later are picked up
    without a restart. A capture thread whose dongle is unplugged simply
    retries until it comes back. Also acts as a stall watchdog: a dongle
    whose rtl_sdr process is alive but stops delivering IQ data (flaky USB
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
                state.log_console(f"Dongle {sn} stalled (no IQ data for {int(time.time() - entry['last_data'])}s) — restarting rtl_sdr", "warn")
                entry['last_data'] = time.time()
                try:
                    proc.kill()
                except Exception:
                    pass
        time.sleep(10)

def sdr_capture_thread(serial):
    """Capture loop for one dongle (pinned by serial via rtl_sdr -d):
    rtl_sdr → FFT waterfall (+ FM demod, WAV recording during passes on the
    primary dongle)."""
    import numpy as np
    entry = state.sdrs[serial]
    primary = entry['primary']
    rtl_log_path = RTL_LOG if primary else os.path.join(LOGDIR, f'rtl_sdr_{serial}.log')
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    rtl_log_f = open(rtl_log_path, 'w')
    pll_warned = False

    while True:
        proc = None
        try:
            with state.status_lock:
                tune_target = state.current_frequency
            correction = tuning_correction(tune_target)
            freq_str = f"{tune_target + SDR_OFFSET_HZ + correction}"
            tuned_freq = tune_target
            record_sat = None
            # Fresh log per rtl_sdr start so the PLL check below only sees
            # messages from the process it is validating
            rtl_log_f.seek(0)
            rtl_log_f.truncate()
            proc = subprocess.Popen(
                ['rtl_sdr', '-d', serial, '-f', freq_str, '-s', str(SDR_RATE), '-g', str(SDR_GAIN), '-'],
                stdout=subprocess.PIPE, stderr=rtl_log_f
            )
            entry['proc'] = proc
            entry['last_data'] = time.time()
            if primary:
                state.rtl_sdr_proc = proc
                state.log_console(f"rtl_sdr started (pid {proc.pid}, primary dongle {serial}), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={SDR_GAIN}dB")
                if _rtl_pll_failed(proc) and not pll_warned:
                    pll_warned = True
                    state.log_console("R820T PLL lock not confirmed — continuing anyway (lock bit unreliable on this dongle, mistune handled via SDR_FREQ_CORRECTION_HZ)", "warn")
            else:
                state.log_console(f"rtl_sdr started (pid {proc.pid}, dongle {serial}), tuned {freq_str}Hz")
            last_history_append = 0.0
            block_count = 0

            while True:
                raw = proc.stdout.read(IQ_BLOCK)
                if not raw or len(raw) < IQ_BLOCK:
                    state.log_console(f"rtl_sdr (dongle {serial}) stdout closed, restarting...", "warn")
                    break
                entry['last_data'] = time.time()

                # Offset-shift the baseband once: satellite to 0 Hz, DC spike
                # displaced to +SDR_OFFSET_HZ. Shared by waterfall FFT and demod.
                c = iq_to_complex(raw)
                c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE)

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
                            if magnitude.max() > 0:
                                magnitude = magnitude / magnitude.max() * 255
                            row = magnitude.astype(int).tolist()
                            with entry['lock']:
                                entry['waterfall'].append(row)
                    except Exception:
                        pass

                # Retune when the scheduler moves to another satellite's
                # frequency: restart rtl_sdr on the new frequency. Between
                # passes (idle) this parks the dongle on NOAA 15; at a pass
                # boundary it switches bands (e.g. 137 MHz -> 437 MHz ISS).
                # All dongles re-tune together; the primary additionally
                # keeps one satellite's audio per WAV.
                with state.status_lock:
                    freq_now = state.current_frequency
                    sat_now = state.current_sat_name
                if freq_now != tuned_freq and (not state.is_recording or sat_now != record_sat):
                    if primary:
                        state.log_console(f"Retuning: {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                    break

                # Record to WAV only during passes (primary dongle only —
                # comparison dongles feed waterfalls, not recordings)
                if primary:
                    should_record = False
                    with state.status_lock:
                        should_record = state.is_pass_active

                    if should_record and not state.is_recording:
                        # Start new recording
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        sat_short = state.current_sat_name.replace(" ", "_").replace("(idle)", "idle")
                        record_sat = state.current_sat_name
                        state.current_wav_path = os.path.join(RECORD_DIR, f"{sat_short}_{timestamp}.wav")
                        state.current_wav = wave.open(state.current_wav_path, 'wb')
                        state.current_wav.setnchannels(1)
                        state.current_wav.setsampwidth(2)
                        state.current_wav.setframerate(AUDIO_RATE)
                        state.is_recording = True
                        state.log_console(f"🎬 Recording started: {state.current_wav_path}")

                    elif not should_record and state.is_recording:
                        # Stop recording
                        try:
                            state.current_wav.close()
                            state.log_console(f"🎬 Recording stopped: {state.current_wav_path}")
                        except Exception as e:
                            state.log_console(f"WAV close error: {e}", "error")
                        state.current_wav = None
                        state.current_wav_path = None
                        state.is_recording = False

                    # FM-demodulate every block: feeds the live audio stream,
                    # and is written to the WAV during passes. Broadcast FM
                    # (manual radio test tunes) deviates ±75 kHz, so the whole
                    # ±120 kHz capture band is demodulated and only the audio
                    # is low-passed; satellite APT/SSTV keeps the narrow 22 kHz.
                    try:
                        if FM_BAND[0] <= freq_now <= FM_BAND[1]:
                            audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=None, audio_cutoff_hz=18000)
                        else:
                            audio = fm_demodulate(c, DECIMATION)
                    except Exception:
                        audio = b''
                    if audio:
                        state.push_live_audio(audio)

                    if state.is_recording and state.current_wav:
                        try:
                            if audio:
                                state.current_wav.writeframes(audio)
                        except Exception as e:
                            state.log_console(f"WAV write error: {e}", "error")

        except Exception as e:
            state.log_console(f"SDR thread error (dongle {serial}): {e}", "error")
        try: proc.kill()
        except: pass
        if primary:
            try:
                if state.current_wav:
                    state.current_wav.close()
            except: pass
            state.is_recording = False
            state.current_wav = None
            state.current_wav_path = None
        time.sleep(5)
