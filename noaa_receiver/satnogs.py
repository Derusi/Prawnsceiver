"""SatNOGS DB metadata for tracked satellites (server-side proxy + cache).

The dashboard shows "what am I listening to" details during a live pass: the
satellite record (names, launch, status) and its transmitter list from the
SatNOGS DB API. Fetched once per satellite and cached for a day — the data
rarely changes, and the dashboard polls continuously.
"""
import json
import time
import urllib.request

from .config import TLE_USER_AGENT

_API = "https://db.satnogs.org/api"
_cache = {}        # catnr -> {'info': dict, 'ts': epoch}
_TTL = 24 * 3600

def _get_json(path):
    req = urllib.request.Request(
        _API + path,
        headers={"User-Agent": TLE_USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def satellite_info(catnr):
    """(info, error) — the SatNOGS satellite record plus its transmitters
    for a NORAD catalog number, cached for a day. Stale cache is served
    when the API is unreachable."""
    now = time.time()
    c = _cache.get(catnr)
    if c and now - c['ts'] < _TTL:
        return c['info'], None
    try:
        sats = _get_json(f"/satellites/?norad_cat_id={catnr}")
        if not sats:
            return None, f"satellite {catnr} not in the SatNOGS DB"
        sat = sats[0]
        transmitters = _get_json(f"/transmitters/?satellite={sat['sat_id']}")
    except Exception as e:
        if c:
            return c['info'], None     # serve stale data during outages
        return None, f"SatNOGS DB query failed: {e}"
    image = sat.get("image")
    info = {
        "name": sat.get("name"),
        "names": sat.get("names"),
        "status": sat.get("status"),
        "launched": (sat.get("launched") or "")[:10] or None,
        "countries": sat.get("countries"),
        "image": ("https://db.satnogs.org/media/" + image) if image else None,
        "transmitters": [
            {"description": t.get("description"),
             "downlink_hz": t.get("downlink_low"),
             "mode": t.get("mode"),
             "alive": t.get("alive"),
             "status": t.get("status")}
            for t in transmitters
        ],
    }
    _cache[catnr] = {'info': info, 'ts': now}
    return info, None
