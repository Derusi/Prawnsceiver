import io

def load(path):
    return io.open(path, newline='', encoding='utf-8').read().splitlines(keepends=True)

def save(path, lines):
    io.open(path, 'w', newline='', encoding='utf-8').write(''.join(lines))

def find_one(lines, needle, start=0):
    hits = [i for i, l in enumerate(lines) if needle in l and i >= start]
    assert len(hits) == 1, (needle, hits)
    return hits[0]

# ---- ais.py: ship_tracks() from the 24 h message log ----
lines = load('noaa_receiver/decoding/ais.py')
i = find_one(lines, 'def ais_status():')
new = [l + '\r\n' for l in '''def ship_tracks(mmsis, hours=24):
    """Position track (oldest first) per MMSI, from the persistent
    message log - the rolling frame history is the station's position
    archive. Only frames that carried a position become track points;
    MMSIs without any are returned as empty lists (the caller can say
    so instead of drawing nothing silently)."""
    try:
        wanted = {int(m) for m in mmsis}
    except (TypeError, ValueError):
        return {}
    if not wanted:
        return {}
    hours = max(1, min(float(hours), 48))
    cutoff = time.time() - hours * 3600
    tracks = {m: [] for m in wanted}
    for r in db.query("SELECT entry FROM ais_messages WHERE ts >= ?"
                      " ORDER BY id", (cutoff,)):
        try:
            e = json.loads(r["entry"])
        except ValueError:
            continue
        m = e.get("mmsi")
        if m in tracks and e.get("lat") is not None and e.get("lon") is not None:
            tracks[m].append({"ts": e.get("ts", 0),
                              "lat": e["lat"], "lon": e["lon"]})
    return tracks


def ais_status():''']
lines[i:i] = new
save('noaa_receiver/decoding/ais.py', lines)

import ast
ast.parse(''.join(load('noaa_receiver/decoding/ais.py')))
print('ais.py ship_tracks added')

# ---- handler.py: /ship_tracks.json ----
lines = load('noaa_receiver/web/handler.py')
i = find_one(lines, "elif self.path.startswith('/aislog.json'):")
lines[i:i] = [l + '\r\n' for l in [
    "        elif self.path.split('?')[0] == '/ship_tracks.json':",
    "            # 24 h position tracks for the AIS page's 'path on map'",
    "            # multi-select: ?mmsi=123,456&hours=24",
    "            query = parse_qs(urlparse(self.path).query)",
    "            mmsis = [m.strip() for m in (query.get('mmsi') or [''])[0].split(',')",
    "                      if m.strip()][:50]",
    "            try:",
    "                hours = float((query.get('hours') or ['24'])[0])",
    "            except ValueError:",
    "                hours = 24.0",
    "            tracks = ais.ship_tracks(mmsis, hours)",
    "            self.send_response(200)",
    "            self.send_header('Content-type', 'application/json')",
    "            self.send_header('Access-Control-Allow-Origin', '*')",
    "            self.end_headers()",
    "            self.wfile.write(json.dumps(tracks).encode())",
]]
save('noaa_receiver/web/handler.py', lines)
ast.parse(''.join(load('noaa_receiver/web/handler.py')))
print('handler endpoint added')
