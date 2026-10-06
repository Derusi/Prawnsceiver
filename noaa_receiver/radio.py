"""SDR thread: rtl_sdr capture, waterfall FFT, FM demod, WAV recording."""
import os
import subprocess
import time
import wave
from datetime import datetime

from . import state
from .config import AUDIO_RATE, DECIMATION, FFT_SIZE, IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SDR_FREQ_CORRECTION_HZ, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE

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

def sdr_thread():
    """Main SDR thread: rtl_sdr → FFT waterfall + FM demod → WAV recording during passes."""
    import numpy as np
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    rtl_log_f = open(RTL_LOG, 'w')
    pll_warned = False

    while True:
        try:
            correction = SDR_FREQ_CORRECTION_HZ.get(state.current_frequency, 0)
            freq_str = f"{state.current_frequency + SDR_OFFSET_HZ + correction}"
            tuned_freq = state.current_frequency
            record_sat = None
            # Fresh log per rtl_sdr start so the PLL check below only sees
            # messages from the process it is validating
            rtl_log_f.seek(0)
            rtl_log_f.truncate()
            state.rtl_sdr_proc = subprocess.Popen(
                ['rtl_sdr', '-f', freq_str, '-s', str(SDR_RATE), '-g', str(SDR_GAIN), '-'],
                stdout=subprocess.PIPE, stderr=rtl_log_f
            )
            state.log_console(f"rtl_sdr started (pid {state.rtl_sdr_proc.pid}), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={SDR_GAIN}dB")
            if _rtl_pll_failed(state.rtl_sdr_proc) and not pll_warned:
                pll_warned = True
                state.log_console("R820T PLL lock not confirmed — continuing anyway (lock bit unreliable on this dongle, mistune handled via SDR_FREQ_CORRECTION_HZ)", "warn")
            last_history_append = 0.0

            while True:
                raw = state.rtl_sdr_proc.stdout.read(IQ_BLOCK)
                if not raw or len(raw) < IQ_BLOCK:
                    state.log_console("rtl_sdr stdout closed, restarting...", "warn")
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
                        with state.waterfall_lock:
                            state.waterfall_buffer.append(row)
                except Exception:
                    pass

                # Retune when the scheduler moves to another satellite's
                # frequency: restart rtl_sdr on the new frequency. Between
                # passes (idle) this parks the dongle on NOAA 15; at a pass
                # boundary it switches bands (e.g. 137 MHz -> 437 MHz ISS).
                # Mid-recording retunes only happen when the satellite itself
                # changed, so each WAV holds exactly one satellite's audio.
                with state.status_lock:
                    freq_now = state.current_frequency
                    sat_now = state.current_sat_name
                if freq_now != tuned_freq and (not state.is_recording or sat_now != record_sat):
                    state.log_console(f"Retuning: {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                    break

                # Record to WAV only during passes
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
                # and is written to the WAV during passes
                try:
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
            state.log_console(f"SDR thread error: {e}", "error")
        try: state.rtl_sdr_proc.kill()
        except: pass
        try:
            if state.current_wav:
                state.current_wav.close()
        except: pass
        state.is_recording = False
        state.current_wav = None
        state.current_wav_path = None
        time.sleep(5)
