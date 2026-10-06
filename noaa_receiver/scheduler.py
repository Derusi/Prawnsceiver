"""Pass scheduler: TLE refresh, pass windows, auto-decode, pass logging."""
import glob
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import state
from .config import PASS_MARGIN_SECS, PASS_PREDICT_HOURS, RECORD_DIR, TLE_REFRESH_HOURS, UTC_OFFSET

from .history import log_pass
from .passes import predict_passes, refresh_tles

def scheduler_thread():
    """Background thread: refresh TLEs, predict passes, trigger frequency switches."""
    while True:
        try:
            # Refresh TLEs if stale
            if time.time() - state.last_tle_refresh > TLE_REFRESH_HOURS * 3600:
                sats = refresh_tles()
            else:
                sats = refresh_tles()  # first run
            
            # Predict passes
            passes = predict_passes(sats, PASS_PREDICT_HOURS)
            with state.status_lock:
                state.upcoming_passes = passes
            
            state.log_console(f"Predicted {len(passes)} passes in next {PASS_PREDICT_HOURS}h")
            for p in passes[:5]:
                local_rise = p["rise_utc"] + timedelta(hours=UTC_OFFSET)
                state.log_console(f"  {p['sat_name']} {p['max_alt']:.0f}° at {local_rise.strftime('%H:%M')} ({round(p['frequency']/1e6,4)} MHz)")
            
            # Check every 10 seconds if we need to switch for an upcoming pass
            while True:
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                triggered = None
                for p in passes:
                    # Start pass recording 30s before rise
                    start_time = p["rise_utc"] - timedelta(seconds=PASS_MARGIN_SECS)
                    end_time = p["set_utc"] + timedelta(seconds=PASS_MARGIN_SECS)
                    if start_time <= now <= end_time:
                        triggered = p
                        break
                
                finished_pass = None
                pass_peak = 0.0
                with state.status_lock:
                    if triggered and (state.current_pass is None
                                      or state.current_pass["sat_name"] != triggered["sat_name"]
                                      or state.current_pass["rise_utc"] != triggered["rise_utc"]):
                        state.current_pass = triggered
                        state.current_frequency = triggered["frequency"]
                        state.current_sat_name = triggered["sat_name"]
                        state.is_pass_active = True
                        with state.signal_lock:
                            state.pass_signal_peak = 0.0
                        local_rise = triggered["rise_utc"] + timedelta(hours=UTC_OFFSET)
                        state.log_console(f"🔴 PASS START: {triggered['sat_name']} {round(triggered['frequency']/1e6,4)} MHz, max {triggered['max_alt']}° at {local_rise.strftime('%H:%M')}")
                    elif not triggered and state.current_pass is not None:
                        finished_pass = state.current_pass
                        local_set = finished_pass["set_utc"] + timedelta(hours=UTC_OFFSET)
                        state.log_console(f"✅ PASS END: {finished_pass['sat_name']} finished at {local_set.strftime('%H:%M')}")
                        state.current_pass = None
                        state.is_pass_active = False
                        # Return to NOAA 15 idle frequency
                        state.current_frequency = 137620000
                        state.current_sat_name = "NOAA 15 (idle)"
                        with state.signal_lock:
                            pass_peak = state.pass_signal_peak
                            state.pass_signal_peak = 0.0

                if finished_pass is not None:
                    # Wait for the SDR thread to finalize the WAV, then auto-decode it
                    time.sleep(2)
                    decoded = False
                    png_file = None
                    wav_name = None
                    recordings = sorted(glob.glob(os.path.join(RECORD_DIR, "*.wav")), key=os.path.getmtime, reverse=True)
                    if recordings:
                        latest = recordings[0]
                        wav_name = os.path.basename(latest)
                        latest_png = latest.replace('.wav', '.png')
                        if not os.path.exists(latest_png):
                            state.log_console(f"Auto-decoding: {wav_name}")
                            # Detect satellite from filename
                            sat_arg = None
                            wl = wav_name.lower()
                            if 'noaa_15' in wl or 'noaa15' in wl:
                                sat_arg = 'noaa_15'
                            elif 'noaa_18' in wl or 'noaa18' in wl:
                                sat_arg = 'noaa_18'
                            elif 'noaa_19' in wl or 'noaa19' in wl:
                                sat_arg = 'noaa_19'
                            cmd = ['noaa-apt', latest, '-o', latest_png, '-q', '-m', 'yes', '-R', 'auto',
                                    '-T', '/var/log/noaa/weather.txt']
                            if sat_arg:
                                cmd.extend(['-s', sat_arg])
                            try:
                                result = subprocess.run(
                                    cmd, capture_output=True, text=True, timeout=120, cwd='/opt/noaa-apt'
                                )
                                if os.path.exists(latest_png):
                                    decoded = True
                                    png_file = os.path.basename(latest_png)
                                    state.log_console(f"Auto-decode successful: {png_file}")
                                else:
                                    state.log_console(f"Auto-decode failed: {result.stderr}", "error")
                            except Exception as e:
                                state.log_console(f"Auto-decode error: {e}", "error")
                        else:
                            decoded = True
                            png_file = os.path.basename(latest_png)
                    log_pass(finished_pass["sat_name"], finished_pass["frequency"],
                             finished_pass["max_alt"], finished_pass["duration_min"],
                             finished_pass["rise_utc"], finished_pass["set_utc"],
                             pass_peak, decoded, png_file, wav_name)
                
                # Refresh passes list every 30 min
                if datetime.utcnow().minute % 30 == 0 and datetime.utcnow().second < 10:
                    break
                
                time.sleep(10)
        except Exception as e:
            state.log_console(f"Scheduler error: {e}", "error")
            time.sleep(60)
