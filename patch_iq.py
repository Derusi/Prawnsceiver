# -*- coding: utf-8 -*-
def patch(path, old, new, count=1):
    s = open(path, 'r', encoding='utf-8', newline='').read()
    crlf = '\r\n' in s[:300]
    o = old.replace('\n', '\r\n') if crlf else old
    n = new.replace('\n', '\r\n') if crlf else new
    assert s.count(o) == count, (path, old[:60], s.count(o))
    open(path, 'w', encoding='utf-8', newline='').write(s.replace(o, n, count))
    print('patched', path)

# ---- config: IQ recording set ----
patch('noaa_receiver/config.py',
'''SAT_DSB_DEMOD_BW_HZ = 6000''',
'''SAT_DSB_DEMOD_BW_HZ = 6000

# Passes on these frequencies also record the RAW IQ stream (u8 complex,
# 240 kHz -> ~480 kB/s per dongle) next to the demod audio WAV: DSB and
# Meteor LRPT are digital modes the FM-demod audio cannot carry — decoding
# (SatDump) needs the baseband. APT/SSTV passes stay audio-only.
IQ_RECORD_FREQS = {
    137350000,    # NOAA 18 DSB
    137770000,    # NOAA 19 DSB
    137100000,    # Meteor-M 2-3 LRPT
    137912500,    # Meteor-M 2-4 LRPT
}''')

# ---- radio.py ----
patch('noaa_receiver/radio.py',
'''from .config import (AUDIO_RATE, DOPPLER_APPLY_RANGE_HZ, DECIMATION, FFT_SIZE,
                     IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SAT_DSB_DEMOD_BW_HZ,
                     SAT_DSB_FREQ, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS)''',
'''from .config import (AUDIO_RATE, DOPPLER_APPLY_RANGE_HZ, DECIMATION, FFT_SIZE,
                     IQ_BLOCK, IQ_RECORD_FREQS, LOGDIR, RECORD_DIR, RTL_LOG,
                     SAT_DSB_DEMOD_BW_HZ, SAT_DSB_FREQ, SDR_GAIN,
                     SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS)''')

patch('noaa_receiver/radio.py',
"            'is_recording': False, 'wav': None, 'wav_path': None,",
"            'is_recording': False, 'wav': None, 'wav_path': None, 'iq': None,")

patch('noaa_receiver/radio.py',
'''    def close_wav():
        """Finish this dongle's current WAV (pass end, band or pass switch)."""
        wav, path = entry['wav'], entry['wav_path']
        entry['wav'] = None
        entry['wav_path'] = None
        entry['is_recording'] = False
        set_primary_recording(None)
        if wav is None:
            return
        try:
            wav.close()
            state.log_console(f"🎬 Recording stopped (dongle {serial}): {path}")
        except Exception as e:
            state.log_console(f"WAV close error (dongle {serial}): {e}", "error")''',
'''    def close_wav():
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
            state.log_console(f"WAV close error (dongle {serial}): {e}", "error")''')

patch('noaa_receiver/radio.py',
'''        entry['wav'] = wav
        entry['wav_path'] = path
        entry['is_recording'] = True
        set_primary_recording(path)
        state.log_console(f"🎬 Recording started (dongle {serial}): {path}")
        return True''',
'''        entry['wav'] = wav
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
        return True''')

patch('noaa_receiver/radio.py',
'''                raw = reader.read_block()''',
'''                raw = reader.read_block()
                if entry['iq'] is not None:
                    try:
                        entry['iq'].write(raw)
                    except Exception as e:
                        state.log_console(f"IQ write error (dongle {serial}): {e} — stopping the IQ capture", "error")
                        try: entry['iq'].close()
                        except Exception: pass
                        entry['iq'] = None''')
print('done')
