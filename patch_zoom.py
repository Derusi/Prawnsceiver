# -*- coding: utf-8 -*-
def patch(path, old, new, count=1):
    s = open(path, 'r', encoding='utf-8', newline='').read()
    crlf = '\r\n' in s[:300]
    o = old.replace('\n', '\r\n') if crlf else old
    n = new.replace('\n', '\r\n') if crlf else new
    assert s.count(o) == count, (path, old[:60], s.count(o))
    open(path, 'w', encoding='utf-8', newline='').write(s.replace(o, n, count))
    print('patched', path)

# ---- 1. zoom helpers (before ensureWfBlock) ----
patch('index.html', '''        function ensureWfBlock(meta) {''',
'''        // ---------- waterfall zoom (Ctrl+wheel / trackpad pinch) ----------
        // Per-block view state: zoom is the visible fraction of the 240 kHz
        // capture span, zoomOffKHz pans the window around the tuned center.
        // 1x = the full capture, max 32x = 7.5 kHz window (16 FFT bins).
        function wfSpanKhz(b) { return 240 / (b.zoom || 1); }
        function wfKhzToPct(b, khz) {
            const span = wfSpanKhz(b);
            return 50 + ((khz - (b.zoomOffKHz || 0)) / (span / 2)) * 50;
        }
        function clearWfCanvas(b) {
            // zoom changes the row geometry — the accumulated history would
            // be torn, so the waterfall restarts empty
            b.wfCtx.fillStyle = '#000';
            b.wfCtx.fillRect(0, 0, WF_WIDTH, WF_HEIGHT);
            b.specCtx.fillStyle = '#000';
            b.specCtx.fillRect(0, 0, WF_WIDTH, SPEC_HEIGHT);
            if (b.timeCtx) { b.timeCtx.fillStyle = '#000'; b.timeCtx.fillRect(0, 0, 54, WF_HEIGHT); }
            b.tickCounter = 0;
        }
        function updateZoomBadge(b) {
            if (!b.zoomBadge) return;
            const z = b.zoom || 1;
            if (z > 1.01) {
                b.zoomBadge.style.display = 'block';
                b.zoomBadge.textContent = '\U0001F50D ' + z.toFixed(1) + '\u00d7';
            } else {
                b.zoomBadge.style.display = 'none';
            }
        }

        function ensureWfBlock(meta) {''')

# ---- 2. ensureWfBlock: hoist the recording highlight, add zoom badge,
#         computed-band styling, extended block entry ----
patch('index.html', '''            if (meta.primary) {
                const hl = document.createElement('div');
                hl.id = 'wf-highlight';
                hl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;left:50%;width:20%;transform:translateX(-50%);pointer-events:none;background:rgba(83,215,105,0.10);border-left:1px solid rgba(83,215,105,0.7);border-right:1px solid rgba(83,215,105,0.7);';
                const lbl = document.createElement('div');
                lbl.id = 'wf-highlight-label';
                lbl.style.cssText = 'position:absolute;top:2px;left:50%;transform:translateX(-50%);background:#53d769;color:#1a1a2e;font-size:0.75em;padding:1px 6px;border-radius:3px;white-space:nowrap;';
                hl.appendChild(lbl);
                wrap.appendChild(hl);
            }''',
'''            let hl = null;
            if (meta.primary) {
                hl = document.createElement('div');
                hl.id = 'wf-highlight';
                // left/width are computed from the zoom view in
                // updateDongleCard (zoom-aware band mapping)
                hl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;pointer-events:none;background:rgba(83,215,105,0.10);border-left:1px solid rgba(83,215,105,0.7);border-right:1px solid rgba(83,215,105,0.7);';
                const lbl = document.createElement('div');
                lbl.id = 'wf-highlight-label';
                lbl.style.cssText = 'position:absolute;top:2px;left:50%;transform:translateX(-50%);background:#53d769;color:#1a1a2e;font-size:0.75em;padding:1px 6px;border-radius:3px;white-space:nowrap;';
                hl.appendChild(lbl);
                wrap.appendChild(hl);
            }
            // zoom badge: shown while zoomed in; click resets to 1x
            const zoomBadge = document.createElement('div');
            zoomBadge.title = 'Click to reset the zoom';
            zoomBadge.style.cssText = 'position:absolute;top:2px;right:4px;background:rgba(0,0,0,0.65);color:#a8b8d8;font-size:0.75em;padding:1px 7px;border-radius:3px;cursor:pointer;display:none;z-index:5;';
            zoomBadge.addEventListener('mousedown', e => e.stopPropagation());
            wrap.appendChild(zoomBadge);''')

patch('index.html', '''                block, collapseBtn, cardHolder, freqLabels,
                serial: meta.id, centerMHz: null,
            };''',
'''                block, collapseBtn, cardHolder, freqLabels,
                serial: meta.id, centerMHz: null,
                zoom: 1, zoomOffKHz: 0, recHl: hl, zoomBadge,
            };''')

# selHl: computed band (no fixed centering transform — zoom-aware sizing)
patch('index.html', '''            selHl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;left:50%;transform:translateX(-50%);pointer-events:none;background:rgba(243,156,18,0.10);border-left:1px solid rgba(243,156,18,0.75);border-right:1px solid rgba(243,156,18,0.75);';''',
'''            selHl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;pointer-events:none;background:rgba(243,156,18,0.10);border-left:1px solid rgba(243,156,18,0.75);border-right:1px solid rgba(243,156,18,0.75);';''')

# wire the badge reset once the block exists
patch('index.html', '''            wireWfInteractions(b, wrap, wfCanvas, meta);
            applyDongleCollapse(meta.id);''',
'''            wireWfInteractions(b, wrap, wfCanvas, meta);
            zoomBadge.onclick = ev => {
                ev.stopPropagation();
                b.zoom = 1; b.zoomOffKHz = 0;
                clearWfCanvas(b);
                updateZoomBadge(b);
                updateFreqLabels(lastSharedMHz);
                if (wfOverlayOn) drawWfOverlay();
            };
            applyDongleCollapse(meta.id);''')

# ---- 3. drawWaterfall: zoom-aware row + spectrum slicing ----
patch('index.html', '''            const row = data[data.length - 1];
            for (let x = 0; x < WF_WIDTH && x < row.length; x++) {
                const [r, g, b2] = magnitudeToColor(row[x]);
                b.wfCtx.fillStyle = `rgb(${r},${g},${b2})`;
                b.wfCtx.fillRect(x, 0, 1, 1);
            }''',
'''            const row = data[data.length - 1];
            // Zoom: the visible slice of the 512-bin row is stretched across
            // the canvas (nearest bin); at 1x this is the identity mapping
            const zoom = b.zoom || 1, offKhz = b.zoomOffKHz || 0;
            const zbin0 = (offKhz - wfSpanKhz(b) / 2 + 120) / 0.46875;
            const zspan = wfSpanKhz(b) / 0.46875;
            const srcBin = x => {
                if (zoom <= 1.01) return x;
                let s = Math.floor(zbin0 + (x / WF_WIDTH) * zspan);
                return s < 0 ? 0 : (s >= row.length ? row.length - 1 : s);
            };
            for (let x = 0; x < WF_WIDTH && x < row.length; x++) {
                const [r, g, b2] = magnitudeToColor(row[srcBin(x)]);
                b.wfCtx.fillStyle = `rgb(${r},${g},${b2})`;
                b.wfCtx.fillRect(x, 0, 1, 1);
            }''')

patch('index.html', '''            if (row) {
                for (let x = 0; x < WF_WIDTH && x < row.length; x++) {
                    const h = (row[x] / 255) * SPEC_HEIGHT;
                    const [r, g, b2] = magnitudeToColor(row[x]);
                    b.specCtx.fillStyle = `rgb(${r},${g},${b2})`;
                    b.specCtx.fillRect(x, SPEC_HEIGHT - h, 1, h);
                }
            }''',
'''            if (row) {
                for (let x = 0; x < WF_WIDTH && x < row.length; x++) {
                    const h = (row[srcBin(x)] / 255) * SPEC_HEIGHT;
                    const [r, g, b2] = magnitudeToColor(row[srcBin(x)]);
                    b.specCtx.fillStyle = `rgb(${r},${g},${b2})`;
                    b.specCtx.fillRect(x, SPEC_HEIGHT - h, 1, h);
                }
            }''')

# ---- 4. per-block frequency labels follow the zoom window ----
patch('index.html', '''                const c = (b.centerMHz !== null && b.centerMHz !== undefined) ? b.centerMHz : freqMHz;
                b.freqLabels.forEach((el, i) => {
                    el.textContent = (c + (i - 2) * 0.06).toFixed(3);
                });''',
'''                const c = (b.centerMHz !== null && b.centerMHz !== undefined) ? b.centerMHz : freqMHz;
                const step = (0.240 / (b.zoom || 1)) / 4;
                const cView = c + (b.zoomOffKHz || 0) / 1000;
                const dec = step >= 0.01 ? 3 : 4;
                b.freqLabels.forEach((el, i) => {
                    el.textContent = (cView + (i - 2) * step).toFixed(dec);
                });''')

# ---- 5. feature overlay: zoom-aware mapping + adaptive axis + view checks ----
patch('index.html', '''            // x position of a frequency offset in kHz relative to the
            // target center — the waterfall spans ±120 kHz (SDR_RATE/2)
            const x = khz => w / 2 + (khz / 120) * (w / 2);''',
'''            // x position of a frequency offset in kHz relative to the
            // target center — zoom-aware (the visible window is
            // [off - span/2, off + span/2] around the target center)
            const spanHalf = wfSpanKhz(block) / 2, vOff = block.zoomOffKHz || 0;
            const x = khz => w / 2 + ((khz - vOff) / spanHalf) * (w / 2);
            const inView = k => k >= vOff - spanHalf - 0.5 && k <= vOff + spanHalf + 0.5;''')

patch('index.html', '''            for (const k of [-120, -60, 0, 60, 120]) {
                ctx.fillText((k > 0 ? '+' : '') + k, x(k), h - 4);
            }''',
'''            // axis ticks adapt to the zoom window
            const stepKhz = spanHalf <= 4 ? 1 : spanHalf <= 8 ? 2.5 : spanHalf <= 15 ? 5
                          : spanHalf <= 30 ? 10 : spanHalf <= 60 ? 30 : 60;
            const t0 = Math.ceil((vOff - spanHalf) / stepKhz) * stepKhz;
            for (let k = t0; k <= vOff + spanHalf + 0.01; k += stepKhz) {
                ctx.fillText((k > 0 ? '+' : '') + (Math.round(k * 10) / 10), x(k), h - 4);
            }''')

patch('index.html', '''            // Labels
            wfOverlayLabel(ctx, x(0), 44, 'APT signal \u00b117.5 kHz', '#53d769');
            if (bwKhz < 120) {
                wfOverlayLabel(ctx, x(-bwKhz) + 6, 16, '\u25c4 recorded \u00b1' + bwKhz + ' kHz', '#f39c12', 'left');
                wfOverlayLabel(ctx, x(bwKhz) - 6, 16, 'recorded \u00b1' + bwKhz + ' kHz \u25ba', '#f39c12', 'right');
            } else {
                wfOverlayLabel(ctx, x(0), 16, 'recorded: full \u00b1120 kHz band', '#f39c12');
            }
            wfOverlayLabel(ctx, x(0), h - 30, 'signal meter \u00b15 kHz', '#2ecc71');
            wfOverlayLabel(ctx, x(60), h - 12, 'DC spike +60 kHz', '#e74c3c');''',
'''            // Labels (only what is inside the visible window)
            if (inView(0)) wfOverlayLabel(ctx, x(0), 44, 'APT signal \u00b117.5 kHz', '#53d769');
            if (bwKhz < 120) {
                if (inView(-bwKhz)) wfOverlayLabel(ctx, x(-bwKhz) + 6, 16, '\u25c4 recorded \u00b1' + bwKhz + ' kHz', '#f39c12', 'left');
                if (inView(bwKhz)) wfOverlayLabel(ctx, x(bwKhz) - 6, 16, 'recorded \u00b1' + bwKhz + ' kHz \u25ba', '#f39c12', 'right');
            } else {
                if (inView(0)) wfOverlayLabel(ctx, x(0), 16, 'recorded: full \u00b1120 kHz band', '#f39c12');
            }
            if (inView(0)) wfOverlayLabel(ctx, x(0), h - 30, 'signal meter \u00b15 kHz', '#2ecc71');
            if (inView(60)) wfOverlayLabel(ctx, x(60), h - 12, 'DC spike +60 kHz', '#e74c3c');''')

# ---- 6. wireWfInteractions: zoom-aware freq mapping + Ctrl+wheel handler ----
patch('index.html', '''            const freqAt = (clientX) => {
                const r = wfCanvas.getBoundingClientRect();
                const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
                return { frac, mhz: b.centerMHz + (frac - 0.5) * 0.240 };
            };''',
'''            const freqAt = (clientX) => {
                const r = wfCanvas.getBoundingClientRect();
                const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
                const span = wfSpanKhz(b);
                return { frac, mhz: b.centerMHz + ((b.zoomOffKHz || 0) + (frac - 0.5) * span) / 1000 };
            };''')

patch('index.html', '''                            b.centerMHz = center;
                            if (wfOverlayOn) drawWfOverlay();''',
'''                            b.centerMHz = center;
                            b.zoomOffKHz = 0;   // the view recenters on the new tune
                            if (wfOverlayOn) drawWfOverlay();''')

patch('index.html', '''            wrap.addEventListener('mousedown', e => {
                if (b.centerMHz === null || e.button !== 0) return;
                drag = { x0: e.clientX, x1: e.clientX };
                e.preventDefault();
            });''',
'''            // Ctrl+wheel (or trackpad pinch) zooms this waterfall, anchored
            // at the pointer; plain wheel still scrolls the page
            wrap.addEventListener('wheel', e => {
                if (!e.ctrlKey || b.centerMHz === null) return;
                e.preventDefault();
                const r = wfCanvas.getBoundingClientRect();
                const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
                const oldZoom = b.zoom || 1;
                let newZoom = oldZoom * (e.deltaY < 0 ? 1.18 : 1 / 1.18);
                if (newZoom < 1.05) newZoom = 1;
                newZoom = Math.min(32, newZoom);
                const cursorKhz = (b.zoomOffKHz || 0) + (frac - 0.5) * wfSpanKhz(b);
                const newSpan = 240 / newZoom;
                let newOff = newZoom === 1 ? 0 : cursorKhz - (frac - 0.5) * newSpan;
                const maxOff = Math.max(0, 120 - newSpan / 2);
                newOff = Math.min(maxOff, Math.max(-maxOff, newOff));
                b.zoom = newZoom;
                b.zoomOffKHz = newOff;
                clearWfCanvas(b);
                updateZoomBadge(b);
                updateFreqLabels(lastSharedMHz);
                if (wfOverlayOn) drawWfOverlay();
            }, { passive: false });
            wrap.addEventListener('mousedown', e => {
                if (b.centerMHz === null || e.button !== 0) return;
                drag = { x0: e.clientX, x1: e.clientX };
                e.preventDefault();
            });''')

# ---- 7. selected-range and recording bands follow the zoom ----
patch('index.html', '''                    wfB.selHl.style.display = 'block';
                    wfB.selHl.style.width = Math.min(100, (selBwKhz / 120) * 100) + '%';
                    wfB.selLbl.textContent = '\u00b1' + (Math.round(selBwKhz * 10) / 10) + ' kHz';
                } else {
                    wfB.selHl.style.display = 'none';
                }
            }''',
'''                    const p1 = Math.max(0, wfKhzToPct(wfB, -selBwKhz));
                    const p2 = Math.min(100, wfKhzToPct(wfB, selBwKhz));
                    wfB.selHl.style.display = 'block';
                    wfB.selHl.style.left = p1 + '%';
                    wfB.selHl.style.width = Math.max(0, p2 - p1) + '%';
                    wfB.selLbl.textContent = '\u00b1' + (Math.round(selBwKhz * 10) / 10) + ' kHz';
                } else {
                    wfB.selHl.style.display = 'none';
                }
                // recording band (primary only) follows the zoom too
                if (wfB.recHl) {
                    const p1 = Math.max(0, wfKhzToPct(wfB, -24));
                    const p2 = Math.min(100, wfKhzToPct(wfB, 24));
                    wfB.recHl.style.left = p1 + '%';
                    wfB.recHl.style.width = Math.max(0, p2 - p1) + '%';
                }
            }''')
print('done')
