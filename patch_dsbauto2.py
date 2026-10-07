# -*- coding: utf-8 -*-
def patch(path, old, new, count=1):
    s = open(path, 'r', encoding='utf-8', newline='').read()
    crlf = '\r\n' in s[:300]
    o = old.replace('\n', '\r\n') if crlf else old
    n = new.replace('\n', '\r\n') if crlf else new
    assert s.count(o) == count, (path, old[:60], s.count(o))
    open(path, 'w', encoding='utf-8', newline='').write(s.replace(o, n, count))
    print('patched', path)

patch('noaa_receiver/radio.py',
"                     IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SDR_GAIN,",
"                     IQ_BLOCK, LOGDIR, RECORD_DIR, RTL_LOG, SAT_DSB_DEMOD_BW_HZ,\n                     SAT_DSB_FREQ, SDR_GAIN,")

patch('noaa_receiver/radio.py',
'''                    else:
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz or 22000, st=dst)''',
'''                    else:
                        # Auto demod width: DSB receive frequencies carry a
                        # narrowband digital stream — 6 kHz instead of the
                        # 22 kHz APT default tightens the recording SNR
                        auto_bw = SAT_DSB_DEMOD_BW_HZ if freq_now in SAT_DSB_FREQ.values() else 22000
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz or auto_bw, st=dst)''')

patch('noaa_receiver/handler.py',
'''            status["current_pass"] = {
                "sat_name": cur_pass["sat_name"],
                "catnr": cur_pass["catnr"],
                "frequency_mhz": round(cur_pass["frequency"] / 1e6, 4),''',
'''            status["current_pass"] = {
                "sat_name": cur_pass["sat_name"],
                "catnr": cur_pass["catnr"],
                "dsb": cur_pass.get("catnr") in SAT_DSB_FREQ,
                "frequency_mhz": round(cur_pass["frequency"] / 1e6, 4),''')

patch('noaa_receiver/handler.py',
'''            status["next_pass"] = {
                "sat_name": p["sat_name"],
                "frequency_mhz": round(SAT_DSB_FREQ.get(p.get("catnr"), p["frequency"]) / 1e6, 4),''',
'''            status["next_pass"] = {
                "sat_name": p["sat_name"],
                "dsb": p.get("catnr") in SAT_DSB_FREQ,
                "frequency_mhz": round(SAT_DSB_FREQ.get(p.get("catnr"), p["frequency"]) / 1e6, 4),''')

patch('index.html',
'''        let satTrackInfo = null;''',
'''        let satTrackInfo = null;
        // Auto demod width for a dongle WITHOUT a manual override:
        // 120 kHz on broadcast FM tunes, 6 kHz on a DSB receive frequency
        // (narrowband digital stream), else the 22 kHz satellite default
        function autoDemodKhz(centerMHz) {
            if (centerMHz !== null && centerMHz >= 87.5 && centerMHz <= 108) return 120;
            if (satTrackInfo && satTrackInfo.dsb && centerMHz !== null
                    && Math.abs(centerMHz - satTrackInfo.freqMHz) < 0.0005) return 6;
            return 22;
        }''')

patch('index.html',
'''                if (data.pass_active && data.current_pass) {
                    satTrackInfo = { name: data.current_pass.sat_name, freqMHz: data.current_pass.frequency_mhz, active: true };
                } else if (data.next_pass) {
                    satTrackInfo = { name: data.next_pass.sat_name, freqMHz: data.next_pass.frequency_mhz, active: false };
                } else {
                    satTrackInfo = null;
                }''',
'''                if (data.pass_active && data.current_pass) {
                    satTrackInfo = { name: data.current_pass.sat_name, freqMHz: data.current_pass.frequency_mhz, active: true, dsb: !!data.current_pass.dsb };
                } else if (data.next_pass) {
                    satTrackInfo = { name: data.next_pass.sat_name, freqMHz: data.next_pass.frequency_mhz, active: false, dsb: !!data.next_pass.dsb };
                } else {
                    satTrackInfo = null;
                }''')

patch('index.html',
'''            // APT signal band: FM deviation ±17.5 kHz around the target center
            ctx.fillStyle = 'rgba(83, 215, 105, 0.08)';
            ctx.fillRect(x(-17.5), 0, x(17.5) - x(-17.5), h);
            ctx.strokeStyle = 'rgba(83, 215, 105, 0.5)';
            ctx.setLineDash([]);
            for (const k of [-17.5, 17.5]) {
                ctx.beginPath(); ctx.moveTo(x(k), 0); ctx.lineTo(x(k), h); ctx.stroke();
            }''',
'''            // Expected signal band at the target center: ±17.5 kHz APT FM
            // deviation on image satellites; the DSB stream is narrowband
            // (~2-3 kHz) on DSB-received satellites
            const sigHalf = (satTrackInfo && satTrackInfo.dsb) ? 2.5 : 17.5;
            const sigName = (satTrackInfo && satTrackInfo.dsb) ? 'DSB stream ~\\u00b12.5 kHz' : 'APT signal \\u00b117.5 kHz';
            ctx.fillStyle = 'rgba(83, 215, 105, 0.08)';
            ctx.fillRect(x(-sigHalf), 0, x(sigHalf) - x(-sigHalf), h);
            ctx.strokeStyle = 'rgba(83, 215, 105, 0.5)';
            ctx.setLineDash([]);
            for (const k of [-sigHalf, sigHalf]) {
                ctx.beginPath(); ctx.moveTo(x(k), 0); ctx.lineTo(x(k), h); ctx.stroke();
            }''')

patch('index.html',
"            if (inView(0)) wfOverlayLabel(ctx, x(0), 44, 'APT signal \\u00b117.5 kHz', '#53d769');",
"            if (inView(0)) wfOverlayLabel(ctx, x(0), 44, sigName, '#53d769');")

patch('index.html',
'''                const c = (block.centerMHz !== null && block.centerMHz !== undefined) ? block.centerMHz : lastSharedMHz;
                bwKhz = (c !== null && c >= 87.5 && c <= 108) ? 120 : 22;''',
'''                const c = (block.centerMHz !== null && block.centerMHz !== undefined) ? block.centerMHz : lastSharedMHz;
                bwKhz = autoDemodKhz(c);''')

patch('index.html',
'''                    if (selBwKhz === null || selBwKhz === undefined) {
                        const effC = curDongleMHz(d.id);
                        selBwKhz = (effC !== null && effC >= 87.5 && effC <= 108) ? 120 : 22;
                    }''',
'''                    if (selBwKhz === null || selBwKhz === undefined) {
                        selBwKhz = autoDemodKhz(curDongleMHz(d.id));
                    }''')
print('done')
