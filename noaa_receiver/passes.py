"""Satellite TLE handling and pass prediction (skyfield)."""
import json
import os
import time
import urllib.request
from datetime import timedelta

from . import state
from .config import LAT, LON, TRACKED_SATS, PASS_MIN_ALT, TLE_CACHE_FILE, TLE_USER_AGENT, UTC_OFFSET

try:
    from skyfield.api import load, wgs84, EarthSatellite
    HAS_SKYFIELD = True
except ImportError:
    load = wgs84 = EarthSatellite = None
    HAS_SKYFIELD = False

def _fetch_tle_celestrak(catnr):
    """Fetch raw TLE lines from Celestrak."""
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"
    req = urllib.request.Request(url, headers={"User-Agent": TLE_USER_AGENT})
    text = urllib.request.urlopen(req, timeout=15).read().decode()
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if len(lines) >= 3:
        return lines[:3]
    return None


def _fetch_tle_satnogs(catnr):
    """Fetch raw TLE lines from the SatNOGS DB API (fallback source)."""
    url = f"https://db.satnogs.org/api/tle/?norad_cat_id={catnr}"
    req = urllib.request.Request(url, headers={"User-Agent": TLE_USER_AGENT, "Accept": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    if data:
        entry = data[0]
        return [entry["tle0"].strip(), entry["tle1"].strip(), entry["tle2"].strip()]
    return None


def fetch_tle_lines(catnr):
    """Fetch raw TLE lines; Celestrak first, SatNOGS DB as fallback."""
    last_err = None
    for source in (_fetch_tle_celestrak, _fetch_tle_satnogs):
        try:
            lines = source(catnr)
            if lines:
                return lines
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise last_err
    return None


def _sat_from_lines(lines, ts):
    """Build an EarthSatellite from raw TLE lines."""
    return EarthSatellite(lines[1], lines[2], lines[0], ts)


def refresh_tles():
    """Refresh TLE data from Celestrak, with an on-disk cache fallback."""
    if not HAS_SKYFIELD:
        state.log_console("Skyfield not available, cannot predict passes", "warn")
        return {}
    ts = load.timescale()
    sats = {}
    tle_data = {}
    with state.status_lock:
        state.tle_progress = {"active": True, "done": 0,
                              "total": len(TRACKED_SATS), "current": None}
    try:
        for catnr, (name, freq) in TRACKED_SATS.items():
            with state.status_lock:
                state.tle_progress["current"] = name
            try:
                lines = fetch_tle_lines(catnr)
                if lines:
                    sats[catnr] = (_sat_from_lines(lines, ts), name, freq)
                    tle_data[str(catnr)] = lines
                    state.log_console(f"TLE loaded: {name} (cat #{catnr}), epoch={sats[catnr][0].epoch.utc_datetime()}")
            except Exception as e:
                state.log_console(f"TLE fetch failed for {name} (cat #{catnr}): {e}", "error")
            finally:
                with state.status_lock:
                    state.tle_progress["done"] += 1
        if sats:
            # Persist for future outages
            try:
                with open(TLE_CACHE_FILE, 'w') as f:
                    json.dump(tle_data, f)
            except Exception as e:
                state.log_console(f"TLE cache write failed: {e}", "warn")
        elif os.path.exists(TLE_CACHE_FILE):
            # Celestrak unreachable: reuse the last good TLEs
            state.log_console("TLE fetch failed for all satellites, using cached TLEs", "warn")
            try:
                with open(TLE_CACHE_FILE, 'r') as f:
                    cached = json.load(f)
                for catnr_str, lines in cached.items():
                    catnr = int(catnr_str)
                    name, freq = TRACKED_SATS[catnr]
                    sats[catnr] = (_sat_from_lines(lines, ts), name, freq)
                state.log_console(f"Loaded {len(sats)} TLEs from cache")
            except Exception as e:
                state.log_console(f"TLE cache read failed: {e}", "error")
    finally:
        with state.status_lock:
            state.tle_progress["active"] = False
            state.tle_progress["current"] = None
    state.last_tle_refresh = time.time()
    return sats

def predict_passes(sats, hours=24):
    """Predict satellite passes over the next `hours` hours."""
    if not sats or not HAS_SKYFIELD:
        return []
    ts = load.timescale()
    site = wgs84.latlon(LAT, LON)
    now = ts.now()
    end = ts.tt_jd(now.tt + hours / 24.0)
    
    passes = []
    for catnr, (sat, name, freq) in sats.items():
        try:
            t, events = sat.find_events(site, now, end, altitude_degrees=PASS_MIN_ALT)
            for i, (ti, event) in enumerate(zip(t, events)):
                if event == 0:  # rise
                    rise_time = ti.utc_datetime()
                    max_alt = 0
                    culm_time = rise_time
                    set_time = rise_time
                    duration_min = 0
                    if i + 1 < len(events) and events[i + 1] == 1:
                        culm_time = t[i + 1].utc_datetime()
                        topocentric = (sat - site).at(t[i + 1])
                        alt, az, dist = topocentric.altaz()
                        max_alt = alt.degrees
                        if i + 2 < len(events) and events[i + 2] == 2:
                            set_time = t[i + 2].utc_datetime()
                            duration_min = (set_time - rise_time).total_seconds() / 60
                    passes.append({
                        "sat": sat,
                        "sat_name": name,
                        "frequency": freq,
                        "rise_utc": rise_time,
                        "culm_utc": culm_time,
                        "set_utc": set_time,
                        "max_alt": round(max_alt, 1),
                        "duration_min": round(duration_min, 1),
                    })
        except Exception as e:
            state.log_console(f"Pass prediction failed for {name}: {e}", "error")
    
    passes.sort(key=lambda p: p["rise_utc"])
    return passes

def passes_to_json(passes):
    """Convert pass list to JSON-serializable format for the API."""
    result = []
    for p in passes:
        result.append({
            "sat_name": p["sat_name"],
            "frequency_mhz": round(p["frequency"] / 1e6, 4),
            "rise_local": (p["rise_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
            "culm_local": (p["culm_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "set_local": (p["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "max_alt": p["max_alt"],
            "duration_min": p["duration_min"],
            "quality": "high" if p["max_alt"] >= 35 else ("medium" if p["max_alt"] >= 15 else "low"),
            "rise_timestamp": p["rise_utc"].timestamp(),
            "set_timestamp": p["set_utc"].timestamp(),
        })
    return result
