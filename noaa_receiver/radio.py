"""SDR threads: per-dongle rtl_sdr capture, waterfall FFT, FM demod, WAV recording.

All attached dongles run on the same frequency with the same settings (they
re-tune together via state.current_frequency). Dongle 0 is the primary: it
additionally feeds the live audio stream and records WAVs during passes.
Every dongle gets its own waterfall and signal strength, so receive quality
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
from .config import AUDIO_RATE, DECIMATION, FFT_SIZE, FM_BAND, IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS, tuning_correction

from .dsp import fm_demodulate, frequency_shift, iq_to_complex

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
    """Detect attached RTL-SDR dongles and register them in state.sdrs.

    rtl-sdr prints every attached device on stderr before opening one
    ("Found N device(s):" followed by "idx: label, tuner, SN: serial"), so a
    short probe run is enough — probing a nonexistent index (-d 99) fails
    right after the listing without claiming any dongle. Dongle 0 shares the
    legacy state.waterfall_buffer globals so the rest of the system (status,
    live audio, recording) keeps working unchanged.
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
    for idx_s, label, tuner, serial in found:
        idx = int(idx_s)
        if idx in state.sdrs:
            continue
        if idx == 0:
            waterfall, lock = state.waterfall_buffer, state.waterfall_lock
        else:
            waterfall = deque(maxlen=WATERFALL_ROWS)
            lock = threading.Lock()
        state.sdrs[idx] = {
            'label': label.strip(), 'tuner': tuner.strip(), 'serial': serial.strip(),
            'waterfall': waterfall, 'lock': lock,
            'signal': 0.0, 'proc': None,
        }
        state.log_console(f"🔌 Dongle {idx}: {label.strip()} ({tuner.strip()}), SN {serial.strip()}")

def sdr_thread():
    """Enumerate attached dongles and keep one capture thread per dongle.

    Re-enumerates periodically so dongles plugged in later are picked up
    without a restart. A capture thread whose dongle is unplugged simply
    retries until it comes back.
    """
    threads = {}
    while True:
        enumerate_dongles()
        for idx in sorted(state.sdrs):
            if idx in threads and threads[idx].is_alive():
                continue
            t = threading.Thread(target=sdr_capture_thread, args=(idx,),
                                 daemon=True, name=f'sdr{idx}')
            t.start()
            threads[idx] = t
        time.sleep(10)

def sdr_capture_thread(dev_index):
    """Capture loop for one dongle: rtl_sdr → FFT waterfall (+ FM demod,
    WAV recording during passes on the primary dongle 0)."""
    import numpy as np
    entry = state.sdrs[dev_index]
    primary = dev_index == 0
    rtl_log_path = RTL_LOG if primary else os.path.join(LOGDIR, f'rtl_sdr_{dev_index}.log')
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
                ['rtl_sdr', '-d', str(dev_index), '-f', freq_str, '-s', str(SDR_RATE), '-g', str(SDR_GAIN), '-'],
                stdout=subprocess.PIPE, stderr=rtl_log_f
            )
            entry['proc'] = proc
            if primary:
                state.rtl_sdr_proc = proc
                state.log_console(f"rtl_sdr started (pid {proc.pid}, dongle {dev_index}), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={SDR_GAIN}dB")
                if _rtl_pll_failed(proc) and not pll_warned:
                    pll_warned = True
                    state.log_console("R820T PLL lock not confirmed — continuing anyway (lock bit unreliable on this dongle, mistune handled via SDR_FREQ_CORRECTION_HZ)", "warn")
            else:
                state.log_console(f"rtl_sdr started (pid {proc.pid}, dongle {dev_index}), tuned {freq_str}Hz")
            last_history_append = 0.0

            while True:
                raw = proc.stdout.read(IQ_BLOCK)
                if not raw or len(raw) < IQ_BLOCK:
                    state.log_console(f"rtl_sdr (dongle {dev_index}) stdout closed, restarting...", "warn")
                    break

                # Offset-shift the baseband once: satellite to 0 Hz, DC spike
                # displaced to +SDR_OFFSET_HZ. Shared by waterfall FFT and demod.
                c = iq_to_complex(raw)
                c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE)

                # FFT for waterfall
                try:
                    if len(c) >= FFT_SIZE:
                        window = np.hamming(FFT_SIZE)
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
            state.log_console(f"SDR thread error (dongle {dev_index}): {e}", "error")
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
