# NOAAh's CrabArk — Dashboard Enhancement Instructions

## Project Overview

**NOAAh's CrabArk** is a NOAA weather satellite receiver running on a Raspberry Pi 4 with an RTL-SDR dongle. It predicts satellite passes, automatically switches frequency, records APT audio during passes, and serves a web dashboard.

- **GitHub repo:** https://github.com/Derusi/Prawnsceiver.git
- **Server file:** `server_noaa.py` — Python HTTP server with SDR control, pass prediction (skyfield), and WAV recording
- **Dashboard file:** `index.html` (source: `index_noaa.html`) — vanilla HTML/CSS/JS, no frameworks
- **No build step** — plain HTML served directly by Python's `http.server`
- **Backend APIs** the dashboard calls via fetch:
  - `GET /status.json` — system status, current frequency, pass active, next pass info, signal strength
  - `GET /waterfall.json` — array of 512-value FFT rows (120 rows max), updated ~every 200ms
  - `GET /passes.json` — array of predicted passes (24h), each with sat_name, frequency_mhz, rise_local, culm_local, set_local, max_alt, duration_min, quality (high/medium/low), rise_timestamp, set_timestamp
  - `GET /recordings.json` — array of recordings with filename, size_mb, decoded (bool), png (filename if decoded)
  - `GET /decode/<filename>` — triggers `noaa-apt` to decode a WAV recording, returns JSON {success, png} or {success:false, error}
  - `GET /images/<filename>` — serves decoded PNG images from the recordings directory

## Current Dashboard Structure

The dashboard (`index.html`) has:
1. **Header** — title, current frequency/satellite display
2. **Pass banner** — shows next pass countdown or active pass info (changes color: idle=blue, soon=yellow, active=green+pulsing)
3. **Waterfall section** — canvas waterfall + spectrum bar + frequency labels
4. **Main panel** (left) — upcoming passes list + recordings list with decode buttons
5. **Status panel** (right) — dongle status, waterfall status, recording status, signal strength bar, recording count, uptime, RTL log, NOAA frequencies

## Enhancements to Implement

### 1. Auto-decode after pass

**Goal:** When a satellite pass ends and a new recording appears, automatically trigger the `/decode/<filename>` API and display the result without the user clicking a button.

**Backend changes (`server_noaa.py`):**

In the `scheduler_thread()` function, when a pass ends (the `elif not triggered and current_pass is not None:` branch), after setting `is_pass_active = False`, find the most recent recording and auto-decode it:

```python
# After pass ends, auto-decode the recording
import glob
recordings = sorted(glob.glob(os.path.join(RECORD_DIR, "*.wav"), reverse=True)
if recordings:
    latest = os.path.basename(recordings[0])
    latest_png = recordings[0].replace('.wav', '.png')
    # Check if not already decoded
    if not os.path.exists(latest_png):
        print(f"Auto-decoding: {latest}")
        try:
            result = subprocess.run(
                ['noaa-apt', recordings[0], '-o', latest_png, '-q'],
                capture_output=True, text=True, timeout=120
            )
            if os.path.exists(latest_png):
                print(f"Auto-decode successful: {latest_png}")
            else:
                print(f"Auto-decode failed: {result.stderr}")
        except Exception as e:
            print(f"Auto-decode error: {e}")
```

**Frontend changes (`index.html`):**

In the `fetchRecordings()` function, detect new recordings that have `decoded: true` and auto-display the image. Also, if a recording appears that isn't decoded yet, automatically trigger decode after a short delay:

```javascript
// Inside fetchRecordings(), after rendering the list:
for (const r of recordings) {
    if (!r.decoded && !decodingInProcess[r.filename]) {
        decodingInProcess[r.filename] = true;
        setTimeout(() => decodeRecording(r.filename), 3000); // wait 3s for WAV to finalize
    }
}
```

Add at top of script:
```javascript
const decodingInProcess = {};
```

### 2. Signal strength timeline graph

**Goal:** Show a small graph of signal strength over the last ~5 minutes. During a satellite pass, you'll see a bell curve as the signal rises and falls.

**Frontend changes (`index.html`):**

Add a canvas in the status panel, below the signal strength bar:

```html
<div class="status-item">
    <div class="status-label">Signal Timeline (5 min)</div>
    <canvas id="signal-canvas" width="240" height="60" style="width:100%;height:60px;background:#000;border:1px solid #0f3460;border-radius:4px;"></canvas>
</div>
```

Add JavaScript to collect and draw the timeline:

```javascript
const signalHistory = [];
const MAX_SIGNAL_POINTS = 300; // 5 min at 1Hz

async function fetchSignal() {
    try {
        const res = await fetch('/status.json');
        const data = await res.json();
        signalHistory.push(data.signal_strength || 0);
        if (signalHistory.length > MAX_SIGNAL_POINTS) signalHistory.shift();
        drawSignalGraph();
    } catch(e) {}
}

function drawSignalGraph() {
    const canvas = document.getElementById('signal-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, w, h);
    
    if (signalHistory.length < 2) return;
    
    const maxVal = Math.max(...signalHistory, 50);
    const step = w / (MAX_SIGNAL_POINTS - 1);
    
    // Draw filled area
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < signalHistory.length; i++) {
        const x = i * step;
        const y = h - (signalHistory[i] / maxVal) * h;
        ctx.lineTo(x, y);
    }
    ctx.lineTo((signalHistory.length - 1) * step, h);
    ctx.closePath();
    ctx.fillStyle = 'rgba(83, 215, 105, 0.2)';
    ctx.fill();
    
    // Draw line
    ctx.beginPath();
    for (let i = 0; i < signalHistory.length; i++) {
        const x = i * step;
        const y = h - (signalHistory[i] / maxVal) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#53d769';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    
    // Draw "now" marker
    ctx.fillStyle = '#53d769';
    ctx.fillRect((signalHistory.length - 1) * step - 1, 0, 2, h);
}

// Add to the init section:
setInterval(fetchSignal, 1000);
fetchSignal();
```

**Backend changes (`server_noaa.py`):**

Add a new API endpoint for historical signal data (optional — the frontend can also just accumulate from polling):

```python
elif self.path == '/signal_history.json':
    self.send_response(200)
    self.send_header('Content-type', 'application/json')
    self.send_header('Access-Control-Allow-Origin', '*')
    self.end_headers()
    self.wfile.write(json.dumps(list(signal_history)).encode())
```

And in the SDR thread, keep a rolling buffer:

```python
# Add near the top with other globals:
signal_history = deque(maxlen=300)  # 5 min at 1 Hz

# In sdr_thread(), inside the FFT section after computing signal_strength:
with signal_lock:
    signal_history.append(float(band))
```

### 3. Pass history log

**Goal:** A section showing past passes: satellite, max altitude, duration, signal peak, decode status, and a link to the decoded image if available.

**Backend changes (`server_noaa.py`):**

Add a pass history log file:

```python
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")

def log_pass(sat_name, frequency, max_alt, duration_min, rise_time, signal_peak, decoded, png_file):
    """Log a completed pass to the history file."""
    history = []
    if os.path.exists(PASS_HISTORY_FILE):
        try:
            with open(PASS_HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except:
            pass
    history.append({
        "sat_name": sat_name,
        "frequency_mhz": round(frequency / 1e6, 4),
        "max_alt": max_alt,
        "duration_min": duration_min,
        "rise_local": (rise_time + timedelta(hours=UTC_OFFSET)).strftime("%d.%m %H:%M"),
        "signal_peak": round(signal_peak, 1),
        "decoded": decoded,
        "png": png_file,
        "timestamp": datetime.now().isoformat(),
    })
    # Keep last 50 passes
    history = history[-50:]
    with open(PASS_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)
```

Call `log_pass()` in the scheduler thread when a pass ends (in the `elif not triggered and current_pass is not None:` branch). Track `signal_peak` by reading the max signal_strength during the pass.

Add an API endpoint:

```python
elif self.path == '/pass_history.json':
    self.send_response(200)
    self.send_header('Content-type', 'application/json')
    self.send_header('Access-Control-Allow-Origin', '*')
    self.end_headers()
    history = []
    if os.path.exists(PASS_HISTORY_FILE):
        try:
            with open(PASS_HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except:
            pass
    self.wfile.write(json.dumps(history).encode())
```

**Frontend changes (`index.html`):**

Add a new section in the main panel, between the passes list and recordings:

```html
<div class="section-title">📜 Pass History</div>
<div id="pass-history-list">No passes logged yet.</div>
```

Add JavaScript:

```javascript
async function fetchPassHistory() {
    try {
        const res = await fetch('/pass_history.json');
        const history = await res.json();
        const list = document.getElementById('pass-history-list');
        if (history.length === 0) {
            list.innerHTML = '<div style="color:#666;padding:10px;">No passes logged yet.</div>';
            return;
        }
        let html = '';
        // Show most recent first
        for (const h of history.reverse()) {
            const qIcon = h.max_alt >= 35 ? '🟢' : (h.max_alt >= 15 ? '🟡' : '🔴');
            const decIcon = h.decoded ? '🖼️' : '❌';
            const imgLink = h.png ? ` <a href="/images/${h.png}" target="_blank" style="color:#53d769;">view</a>` : '';
            html += `<div class="pass-row ${h.max_alt >= 35 ? 'high' : (h.max_alt >= 15 ? 'medium' : 'low')}">
                <div class="pass-sat">${qIcon} ${h.sat_name}</div>
                <div class="pass-time">${h.rise_local}</div>
                <div class="pass-alt">${h.max_alt}°</div>
                <div style="font-size:0.75em;color:#888;width:50px;">${h.duration_min}min</div>
                <div style="font-size:0.75em;color:#888;width:50px;">peak:${h.signal_peak||'—'}</div>
                <div style="font-size:0.8em;">${decIcon}${imgLink}</div>
            </div>`;
        }
        list.innerHTML = html;
    } catch(e) { console.error('Error fetching pass history:', e); }
}

// Add to init:
setInterval(fetchPassHistory, 30000);
fetchPassHistory();
```

### 4. Audio preview (bonus)

**Goal:** A play button on recordings so you can hear the APT signal.

**Backend changes (`server_noaa.py`):**

Add a route to stream WAV files:

```python
elif self.path.startswith('/audio/'):
    filename = self.path[7:]
    if '..' in filename or '/' in filename:
        self.send_response(400)
        self.end_headers()
        return
    wav_path = os.path.join(RECORD_DIR, filename)
    if os.path.exists(wav_path):
        self.send_response(200)
        self.send_header('Content-type', 'audio/wav')
        self.end_headers()
        with open(wav_path, 'rb') as f:
            self.wfile.write(f.read())
    else:
        self.send_response(404)
        self.end_headers()
```

**Frontend changes (`index.html`):**

In the recording template, add an audio element:

```html
<button onclick="decodeRecording('${r.filename}')">🔄 Decode APT Image</button>
<audio controls preload="none" style="width:100%;margin-top:5px;height:30px;">
    <source src="/audio/${r.filename}" type="audio/wav">
</audio>
```

### 5. Crab counter (fun bonus)

**Goal:** A little counter showing how many images have been decoded.

**Frontend changes (`index.html`):**

In the header, next to the title:

```html
<span style="font-size:0.7em;color:#888;margin-left:10px;">🦀 Crabs caught: <span id="crab-count">0</span></span>
```

Update in `fetchRecordings()`:

```javascript
const decoded = recordings.filter(r => r.decoded).length;
const crabEl = document.getElementById('crab-count');
if (crabEl) crabEl.textContent = decoded;
```

## Implementation Notes

- All files are plain HTML/CSS/JS and Python 3 — no build step, no npm, no frameworks
- The server runs on port 8085, nginx proxies port 8000 → 8085
- The dashboard polls APIs via `fetch()` at intervals (waterfall 200ms, status 3s, passes 30s, recordings 5s)
- `noaa-apt` binary is at `/usr/local/bin/noaa-apt` (symlinked from `/opt/noaa-apt/`)
- Recordings are saved to `/var/log/noaa/recordings/`
- Pass prediction uses `skyfield` library with TLEs from Celestrak
- The `current_pass` variable in the scheduler thread tracks the active pass; when it transitions from a pass to None, that's when auto-decode and pass logging should trigger
- Signal strength is a float representing the average magnitude of the center 20 FFT bins — noise floor is typically 100-200, a satellite signal would spike higher

## Testing

After making changes:
1. Restart the server: `bash /home/eugene/noaa_receiver.sh` (kills old process first)
2. Check the dashboard: `http://[2a02:810d:f390:8182:7656:3f6f:820c:ab59]:8000` or `http://192.168.3.244:8000`
3. Verify `/status.json`, `/passes.json`, `/recordings.json` return valid JSON
4. Wait for a pass or manually trigger decode on an existing recording

## Commit Convention

Use emoji prefixes: `🛰️`, `🦀`, `📜`, `🎨`, `🔊`, etc.
