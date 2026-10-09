# Receiver event log — overnight monitoring 2026-10-07/08

This file is the running log of unattended monitoring of the prawnceiver
ground station. It exists so that a fresh session can pick up the watch
without context.

## How to read / continue this file (session-restart guide)

- Entries are chronological, newest at the bottom. Times are local
  Regensburg time (CEST, UTC+2) unless marked UTC.
- Each entry has: timestamp, what happened, evidence (files, console
  lines, JSON values), and what (if anything) to do next.
- To continue the watch after a restart:
  1. Read this file bottom-up to the last "state: healthy/broken" line.
  2. `ssh eugene@prawnceiver.derusi.de` (key auth) — check
     `pgrep -f 'server_noaa[.]py'` and
     `curl http://127.0.0.1:8085/status.json` (expect < 1 s).
  3. Console log of the receiver: `curl http://127.0.0.1:8085/console.json`
     (in-memory ring, resets on restart).
  4. Recordings: `/var/log/noaa/recordings/` (WAV audio, `.iq.u8` raw IQ,
     `_lrpt/`/`_dsb/` SatDump product dirs). Pass history:
     `/var/log/noaa/pass_history.json` (also `/pass_history.json` API).
  5. Update the cron schedule for the remaining passes (see
     "Tonight's pass plan" below), verify each pass after its set time,
     append an entry, commit.
- Receiver restart (only if broken, never during an active pass —
  `status.json: pass_active`):
  `pkill -f 'server_noaa[.]py'; sleep 3; nohup ~/noaa_receiver.sh > /dev/null 2>&1 &`
  Sudo password: ask the user (deliberately not stored in this repo).
  Deploy new code: push to GitHub, then on the Pi
  `cd /home/eugene/aprs_website && git pull -q` (index.html is static;
  Python changes need the restart above).
- Commands from the Pi time out fast (< 10 s) — if an endpoint does not
  answer immediately, something is broken; investigate, do not wait.

## Tonight's pass plan (all times CEST, from /passes.json)

| Rise  | Satellite        | Mode  | Max alt | What to verify after set            |
|-------|------------------|-------|---------|--------------------------------------|
| 02:51 | Meteor-M 2-4     | LRPT  | 12.7°   | IQ files, SatDump meteor_m2-x_lrpt run (low pass: products unlikely) |
| 04:29 | Meteor-M 2-4     | LRPT  | 80.8°   | Headline: first real digital-image attempt. IQ + SatDump + products |
| 06:10 | Meteor-M 2-4     | LRPT  | 19.2°   | IQ + SatDump                          |
| 07:23 | NOAA 15          | APT   | 24.7°   | WAV + noaa-apt PNG (APT transmitter believed alive) |
| 08:45 | ISS (Zarya)      | SSTV  | 11.3°   | WAV only; no ARISS event expected — snowstorm/empty is normal |
| 09:02 | NOAA 15          | APT   | 54.0°   | Best image chance of the night        |
| 09:31 | Meteor-M 2-3     | LRPT  | 19.9°   | IQ + SatDump                          |
| 10:19 | ISS (Zarya)      | SSTV  | 53.7°   | WAV only                              |

Per-pass checks: (1) console.json for "Recording started/stopped", live
retune lines, "SatDump decode started (…)" and "… done"; (2) recording
files on disk (WAV ~35 MB/min APT, IQ ~29 MB/min per dongle); (3)
`pass_history.json` entry with signal_peak; (4) receiver RSS memory
(`ps -o rss= -p $(pgrep -f server_noaa[.]py)` — flag if > 1.5 GB; the
receiver was OOM-killed once at ~00:48 with 2.4 GB).

## Known context going into the night

- NOAA 18/19 APT transmitters are dark (verified by receive + decode):
  their passes run in DSB mode (137.35 / 137.77 MHz) with raw IQ + SatDump
  `noaa_dsb`. NOAA 15 APT is believed alive — tonight is its test.
- db.satnogs.org and celestrak.org are TCP-blocked from the Pi (SYNs
  dropped on v4+v6; ICMP+DNS fine; other internet fine; user's PC fine).
  Suspected blocklist. Consequences: no SatNOGS transmitter metadata in
  the pass list (mode chips still work), and TLE syncs will fail until it
  clears — the receiver runs on its cached TLEs (cache is fresh, good for
  days). Retry: receiver retries DB every 30 min automatically.
- Open bugs (do NOT chase tonight, on the backlog): decode-attempt
  markers missing (OOM risk at pass-end decode loops of old ISS WAVs);
  estimate_quality always 0%; pass-end attribution may duplicate history
  entries.
- Cron schedule for this watch (verify after each pass, append entries):
  - 03:06 CEST — 02:51 Meteor-M 2-4 LRPT pass (id f5f22472)
  - 04:45 CEST — 04:29 Meteor-M 2-4 LRPT 80.8° headline pass (id a7fca383)
  - 06:25 CEST — 06:10 Meteor-M 2-4 LRPT pass (id ef3f8fa6)
  - 07:35 CEST — 07:23 NOAA 15 APT pass, first image chance (id e6ffffae)
  - 09:15 CEST — 09:02 NOAA 15 APT 54° pass, best image chance (id 5fa7237a)
  - 10:40 CEST — morning wrap-up + block recheck (id e96e0bcc)
  Crons fire only while the session is live and idle; if a slot was
  missed, run that check manually on resume.

## Entries

### 2026-10-08 01:40 CEST — watch starts, state: healthy
- Pi at origin/main 42891d5, receiver up (pid 203005), all endpoints
  < 10 ms. Pass list shows receive plans (mode chips) for all passes.
- History page font fix (2a5d8bc) and receive-plan feature (e787b88,
  527d8e2, 42891d5) deployed. The API-freeze bug (SatNOGS fetch under
  status_lock) is fixed and verified.
- SatNOGS/Celetrak block: diagnosed ~00:50-01:20 CEST, left as-is per user.
- Upcoming: 02:51 Meteor-M 2-4 LRPT (12.7°) — pipeline shakedown pass.

### 2026-10-08 01:45 CEST — unattended work: OOM root fix deployed
Changes made while the user is asleep (all pushed, Pi restarted at
01:42 CEST, idle window before the 02:51->04:29 passes):

1. Decode-attempt markers (ef52533) — the OOM root cause fix.
   - decode.py: every decode attempt's outcome is persisted in
     `<wav>.decode.json`; later attempts short-circuit on the marker.
     `decode_recording(wav, force=True)` re-runs (Retry button).
   - handler.py: `/decode` accepts `?force=1`; `/delete` also removes
     the marker.
   - history.py / history.html: recordings list exposes
     `decode_attempted`/`decode_error`; the history page shows the
     failure reason + a Retry button instead of re-decoding, and the
     auto-decode loop skips attempted recordings. Previously every
     page load re-ran in-process SSTV/noaa-apt decodes on permanently
     undecodable WAVs (ISS without ARISS, dark transmitters) until the
     receiver OOMed at 2.4 GB.
   - scheduler.py: pass-end auto-decode skips previously-failed
     recordings with a single console line instead of re-running.
   - Seeded 28 skip markers ("skipped in bulk before decode markers
     existed — Retry to decode") into existing un-decoded recordings
     so the next history-page open does not spawn a decode storm.
2. Quality scorer (ff3d5a0): routing now uses the exact satellite name
     from decode.satellite_from_filename instead of `'iss' in filename`
     (a "swiss_sat" recording would have been scored as SSTV). New
     tests/test_quality.py synthesizes good APT / good Robot 36 /
     desynced / noise WAVs: good=100, desynced=65, noise=0 — the
     scorer itself was CORRECT; the "0% for everything" reports were
     genuine noise (dark transmitters). NOAA 15 tonight will get real
     quality values.
3. Test fixes: test_radio fixture gained the 'iq' entry field (the IQ
     recording commit 54eb116 had missed updating the test fixture);
     test_decode re-runs now use force=True where a marker would
     legitimately short-circuit.
- Full suite passes on the Pi (test_decode, test_dsp, test_radio,
  test_quality). Receiver restarted, both dongles running, RSS 47 MB,
  armed for the 02:51 pass (runs with these fixes).

### 2026-10-08 01:55 CEST — unattended work: pass-history dedup deployed
- Pass-end history attribution (a5a9bb2), the last open backlog bug:
  - history.log_pass merges a second log of the same physical pass
    (same satellite, rise within 120 s) instead of appending a
    duplicate — receiver flip-flops between overlapping
    same-frequency passes and manual-tune re-triggers used to log one
    pass twice with split decode/peak state.
  - scheduler attribution now matches recordings by the pass's
    satellite name prefix and takes only the newest primary fragment
    (previously a Meteor entry could carry NOAA 18's WAV — seen in the
    23:01 Meteor-M 2-3 entry).
  - tests/test_history.py covers merge/different-pass/different-sat.
  - One-time cleanup on the Pi: existing pass_history.json deduped
    48 -> 38 entries (backup at pass_history.json.bak2).
- Receiver restarted again (idle), endpoints fast. Note: earlier
  timestamps in this log were corrected — the Pi clock runs CEST and
  the console log stamps are CEST as well.

### 2026-10-08 03:08 CEST — 02:51 Meteor-M 2-4 LRPT pass: pipeline works
- Pass 02:50:30-02:57:10 (12.7°). Both dongles recorded WAV + raw IQ
  simultaneously; PASS END closed all 4 files; auto-decode started two
  detached SatDump meteor_m2-x_lrpt runs at 02:57 (decode markers
  written with "started in the background" — the new marker flow works).
- Files: Meteor-M_2-4_20261008_025030{,_00000991}.wav 38 MB each,
  .iq.u8 192 MB each (~400 s at 480 kB/s — correct).
- Products: both _lrpt dirs empty at 03:07; satdumps still running
  (9 min in, 50 MB RSS each). At 12.7° no lock is expected; the 1 h
  timeout bounds the noise-chewing worst case.
- History: single entry, peak 350.7, wav correctly attributed to the
  pass satellite (satellite-aware attribution works), 39 entries —
  no duplicate.
- Memory: server RSS 92 MB (up from 47 MB during recording —
  buffers, fine), no satdump blowup. No OOM risk.

### 2026-10-08 04:50 CEST — 04:29 Meteor 80.8° pass + re-trigger bug fix
- Pass recorded fully: 349 MB IQ + 69 MB WAV per dongle, peak 420.5,
  single correct history entry. Primary's SatDump on the full IQ
  started 04:41:06 — products dir still empty at 05:10 (decode
  running; LRPT lock unconfirmed so far).
- BUG FOUND AND FIXED (deployed 04:52, restart in idle window): the
  04:29 pass logged FOUR "PASS START" lines at 04:30:00-04:30:09.
  Root cause: pass identity compared rise_utc with exact equality;
  the 30-min re-prediction recomputes rise times that can drift by
  microseconds, so a re-predict landing mid-pass re-triggered the
  running pass each tick (the 30-min break condition spans several
  10 s ticks). Each re-trigger "finished" the pass prematurely: the
  secondary's PARTIAL IQ (first ~100 s) got decoded at 04:30 and its
  _lrpt dir now blocks the full decode via the out-dir marker.
  Fix: _same_pass uses a 30 s rise tolerance; the trigger compares
  via _same_pass. Critical to land before the 07:23 NOAA 15 pass
  (a mid-pass re-decode marker would have blocked the full APT
  decode of the first good image chance).
- FALLOUT (pending): the secondary dongle's 04:29 full IQ needs a
  manual re-decode once the primary finishes: kill the 04:30 partial
  satdump if still running, delete the empty
  Meteor-M_2-4_20261008_042820_00000991_lrpt dir, then
  curl '/decode/Meteor-M_2-4_20261008_042820_00000991.wav?force=1'.
- Note: restarts kill SatDump's supervision thread (1 h timeout +
  product logging) while the satdump process itself survives as an
  orphan — the 02:51 pair and the 04:41 primary therefore have no
  completion console lines; watch orphans manually and kill them if
  they run absurdly long (> 1 h).

### 2026-10-08 06:40 CEST — LRPT decode engineering + a dead end
The 04:29 headline pass: signal captured perfectly (349 MB IQ), but
NO SatDump products. Root-caused through the whole chain:

1. RAW IQ decode can never work: SatDump baseband pipelines demodulate
   around 0 Hz, our raw stream has the satellite at ~-(SDR_OFFSET_HZ) —
   centered decode required (commit 9bd4592).
2. SatDump's Celestrak TLE fetch: each retry blocks 134 s on this
   network before the demod even starts. Fixed: decodes now run under
   `unshare -rn` (instant connection failure) and the receiver seeds
   `~/.config/satdump/satdump_tles.txt` from its own TLE cache
   (commit c07f341).
3. Static centering is untrustworthy: corrections are modeled, and
   transmitters can be off-frequency. Now the decode MEASURES the
   signal position in the IQ: width-matched sliding window (72 kHz for
   LRPT, 6 kHz for DSB), negative-side only (satellite is always below
   center), window selection by total elevated power. Synthetic tests:
   plateau/narrow-carrier/noise all measured correctly (commits
   19bd932, 6bd08e9... see git log). Validated on the real 04:29 file:
   measures -71.4 kHz, my independent spectrogram says the plateau is
   centered ~-75 kHz, 62 kHz wide.
4. THE DEAD END: with the signal correctly centered, a fine sweep of
   satdump pipelines (meteor_m2_lrpt qpsk-72k, meteor_m2-x_lrpt
   oqpsk-72k, meteor_m2-x_lrpt_80k oqpsk-80k) x 8 rotation offsets
   (66..80 kHz, 60 s culmination slices) produced ZERO frames in
   every combination. The plateau is pass-synced (Doppler-drifts with
   the satellite) but its 62 kHz width does not match LRPT-72k
   (108 kHz occupied) or LRPT-80k (120 kHz).
5. RF position: the plateau sits ~16 kHz below where the model puts
   137.9125 MHz — either Meteor-M 2-4 transmits at ~137.8965 MHz in a
   nonstandard mode, or the dongle's correction at 137.9125 is wrong by
   ~90 ppm (which would contradict NOAA 15's in-window peaks at
   137.62). DECISIVE TEST: Meteor-M 2-3's 09:31 pass at 137.1 MHz —
   if its LRPT decodes with the new pipeline, the station is fine and
   M2-4's transmitter is the anomaly.
- Also found: a CONSTANT narrowband interferer at raw ~-55 kHz
  (post-shift +5 kHz, INSIDE the ±4.7 kHz signal-strength window!) —
  present outside passes too; it may be inflating "signal peak"
  numbers on Meteor passes. Worth excluding from the strength window
  in a future change.
- The 06:10 Meteor pass ran the complete new pipeline cleanly:
  record → measure (-71.4 kHz, consistent) → rotate → decode →
  no products (same anomalous signal). Automation works.
- NEXT MAJOR TEST: 07:23 NOAA 15 APT (transmitter believed alive) —
  the audio-path image chance; then 09:31 Meteor-M 2-3.

### 2026-10-08 06:45 CEST — 06:10 Meteor pass verification (cron)
- Complete pipeline ran unattended end to end: PASS 06:09:27-06:18:48,
  both dongles WAV (54 MB) + IQ (269 MB), auto-decode with markers,
  measured centering, quality scores, history entry — all automatic,
  no operator action.
- Primary: signal measured at -71.4 kHz (identical to the 04:29 pass
  — the anomaly is reproducible, satellite-side). Decode ran, no
  products (0-byte CADU, as established).
- Secondary (FC0013): signal NOT FOUND in its IQ (weaker antenna at
  19.2°, or its different ppm puts the plateau outside the search
  range) — fell back to the +60 kHz assumption, decode ran, no
  products. Expected at this elevation.
- Reception quality scored 0% on both (the FM-demod audio of a
  digital pass is noise — honest).
- History: single entry, peak 695.4 (note: likely inflated by the
  constant -55 kHz interferer sitting in the strength window;
  see the 06:40 entry), decoded=True (decode-started semantics).
- Memory: server 140 MB after the pass-end decodes (was 108 MB) —
  watch the slow creep (47 -> 92 -> 108 -> 140 over the night);
  nowhere near the 1.5 GB flag.
- Receiver healthy, no restart performed; next: NOAA 15 APT 07:23.

### 2026-10-08 07:50 CEST — 07:23 NOAA 15 APT: transmitter ALIVE, image decoded
- Pass recorded cleanly on both dongles (60.5 MB WAVs, closed 07:32:50),
  auto-decode produced PNGs on BOTH (10.4 MB each — 1258x2080 proper
  APT geometry, noaa-apt found sync, sync-strip bars present).
- Image content verified numerically: adjacent-row correlation 0.21
  (whole image) / 0.15-0.26 per third — 4-6x the 0.03-0.05 of the
  dark-transmitter snowstorms, but well below a crisp pass's 0.7+:
  a REAL but weak image, consistent with 24.7 deg max elevation.
- Peak 1132 — strongest signal of the night. NOAA 15's APT transmitter
  is definitively alive and the analog path (retune, demod, noaa-apt,
  markers, attribution) works end to end.
- ANOMALY OPEN (quality scorer): estimate_quality scored 0% on this
  recording, and the audio spectrum at mid-pass is unexpectedly FLAT
  (2400 Hz subcarrier band only +0.7 dB over neighboring bands, no
  hump) despite strong reception and a decoded image. Only 0.06% of
  samples clip, so overdrive is not the explanation. The scorer is
  honest to what it measures; the puzzle is why the real audio lacks
  the subcarrier hump (signal position/demod subtlety?). The 09:02
  NOAA 15 pass at 54 deg gives much better data to continue this.
- Memory: server 152 MB (creep continues: 47->92->108->140->152;
  decodes + quality analyses add up — still 10x under the flag).

### 2026-10-08 09:25 CEST — 09:02 NOAA 15 + honest image-quality verdict
- Both passes clean: 08:45 ISS recorded, auto-decode failed correctly
  ("No SSTV transmission found" — markers written, no decode storm; no
  ARISS event, expected). 09:02 NOAA 15: WAVs (71 MB) + PNGs (12.2 MB)
  on both dongles, peak 474 (curiously LOWER than 07:23's 1132 at
  half the elevation — the strength window likely caught the constant
  interferer on 07:23).
- IMAGE VERDICT (deep dive): NOAA 15's decoded images are NOT useful
  weather imagery. Adjacent-ROW corr ~0.2 for every NOAA 15 image of
  the last two days INCLUDING yesterday's 86.9-degree pass — and
  adjacent-PIXEL corr (0.25) is indistinguishable from the dark
  transmitter snowstorms (0.23-0.26). The row correlation that looked
  like "real signal content" is mostly the periodic sync bars
  noaa-apt renders every row. Carrier + sync alive, video modulation
  weak/noisy. Consistent across days => NOT a regression from
  tonight's changes; NOAA 15's APT video is degraded (old satellite).
  The 07:23 entry's "real but weak image" verdict was too optimistic.
- The flat-audio anomaly fits this story: no 2400 Hz subcarrier hump
  because the video modulation is mostly noise; the quality scorer's
  0% is honest after all.
- Memory: 204 MB (creep: 47->...->204 over 8 passes — worth a
  tracemalloc look in the morning; maybe quality-analysis buffers).
- Remaining: 09:31 Meteor-M 2-3 LRPT (decisive pipeline test) and the
  10:40 wrap-up.

### 2026-10-08 09:45 CEST — 09:31 Meteor-M 2-3: inconclusive at 19.9°
- Pass recorded cleanly (182 MB IQ + 37 MB WAV per dongle), decode
  ran automatically — but the signal measurement found NOTHING above
  threshold on either dongle ("NOT FOUND, assuming 60.0 kHz"), and the
  fallback-position decodes produced 0-byte CADUs.
- At 19.9° this is inconclusive (weak elevation + V-dipole geometry).
  The decisive test moves to the 11:10 Meteor-M 2-3 pass at 67.4°:
  a strong LRPT there proves the pipeline; nothing found at that
  elevation points at the transmitter.
- Memory at 09:37: 260 MB (night creep 47 -> 260 MB across 9 passes —
  tracemalloc investigation recommended today).

### 2026-10-08 10:05 CEST — fixed-gain experiment deployed (user-approved)
- Following both NOAA guides' advice (no AGC, fixed RF gain — see the
  morning comparison against apbouwens' guide), the primary R820T now
  runs 29.7 dB MANUAL gain; the FC0013 secondary stays on AGC as the
  A/B reference (its gain table differs).
- Implementation: calibration.SDR_DONGLE_GAIN = {"77771111153705700":
  29.7}; the capture thread sends manual-gain-mode (0x03) + gain in
  tenths of dB (0x04) over the rtl_tcp control protocol right after
  the handshake — the rtl_tcp CLI parses -g as an int, so the
  R820T's fractional gain steps would be mangled on the command line.
- Console confirms both dongles up, primary at 29.7 dB manual.
- EVALUATION: compare both dongles' audio on the same NOAA 15 passes
  (subcarrier SNR around 2.4 kHz + waterfall noise floor stability).
  Note: NOAA 15's video modulation is degraded regardless — the
  experiment tests station SNR, not the satellite.
- Next passes: 10:19 ISS 53.7°, 10:45 NOAA 15 10.6° (gain smoke test),
  11:10 Meteor-M 2-3 67.4° (decisive LRPT test), 11:12 NOAA 18 20°.

### 2026-10-08 10:15 CEST — prep for a third dongle (NESDR SMArt v5)
User receives a NooElec NESDR SMArt v5 today (R820T2 + 0.5 ppm TCXO).
Changes deployed:
- calibration.py: unknown serials now get (0, "unmeasured dongle")
  instead of inheriting the primary's -11 kHz correction — a TCXO dongle
  would have been mis-tuned on first use. Verified live: both existing
  dongles unchanged; deployed with a restart at 10:13 (idle window).
RUNBOOK for when the dongle arrives (see also the 11:10 LRPT test first):
1. USB: 3 dongles on a Pi 4 is at the power-budget edge (~300 mA
   each) — a POWERED USB HUB is strongly recommended; watch for
   brownouts (rtl_tcp restart loops in the console).
2. Connector: the v5 is SMA — get an SMA cable/adapter for whichever
   antenna it should feed.
3. Serial: plug in, check `rtl_sdr -d 99` output for a UNIQUE serial
   vs 77771111153705700 / 00000991. If it clashes or is generic,
   set one in an idle window (rtl_eeprom needs exclusive access):
   stop receiver, `rtl_eeprom -d <idx> -s <new-serial>`, restart.
4. Enumeration is automatic: the receiver picks it up within 10 s,
   assigns the next rtl_tcp port (1237), a waterfall card, per-dongle
   recordings — no restart needed just for detection.
5. Calibration: its tuning infobox will show "unmeasured dongle".
   Measure the actual ppm (FM pilot method or rtl_test -p) and add a
   SDR_DONGLE_CORRECTIONS entry for its serial; expect near 0
   (TCXO). Until then it records ~correctly (0 correction is much
   closer than the old generics' -11 kHz).
6. Primary role (recommended): the v5 (R820T2, TCXO, better thermal)
   should replace the ancient R820T as PRIMARY_DONGLE_SN — one-line
   change in calibration.py + restart; the V-dipole then moves to it.
   Add its serial to SDR_DONGLE_GAIN (measure best fixed gain first).
7. CPU: a third capture thread adds ~50% DSP load; FFT_EVERY was
   budgeted for two dongles — watch demod stalls / load average.

### 2026-10-08 10:42 CEST — morning wrap-up: night summary, state: healthy
All 9 monitored passes since 02:51 recorded and logged; pipeline
automated end to end every time (record, auto-decode with markers,
quality, history attribution — no operator action all night).

| Rise | Satellite        | Alt   | Peak  | Decoded | Products |
|------|------------------|-------|-------|---------|----------|
| 02:51 | Meteor-M 2-4    | 12.7° | 350.7 | yes (decode-started) | _lrpt: 0-byte CADU |
| 04:29 | Meteor-M 2-4    | 80.8° | 420.5 | yes | _lrpt: 0-byte CADU |
| 06:10 | Meteor-M 2-4    | 19.2° | 695.4 | yes | _lrpt: 0-byte CADU |
| 07:23 | NOAA 15         | 24.7° | 1132  | yes | PNG (weak/degraded video) |
| 08:45 | ISS (Zarya)     | 11.3° | 717.9 | no (correct, no ARISS) | — |
| 09:02 | NOAA 15         | 54.0° | 474   | yes | PNG (weak/degraded video) |
| 09:31 | Meteor-M 2-3    | 19.9° | 418.4 | yes | _lrpt: 0-byte CADU |
| 10:19 | ISS (Zarya)     | 53.7° | 22.4  | no (correct) | — |

1. LRPT PRODUCTS: ZERO digital images from any Meteor pass. All 7
   _lrpt dirs (both dongles x 3 Meteor passes + 09:31 M2-3) contain
   only a 0-byte .cadu + empty MSU-MR + 4-byte telemetry.json. No
   _dsb product dirs exist yet (NOAA 18/19 DSB decodes: the 00:36
   NOAA 18 pass predates the watch window and recorded no IQ-era
   products). The decisive test remains 11:10 Meteor-M 2-3 at 67.4°.
2. MEMORY: no OOM, no crash overnight — the receiver ran from the
   01:42 restart through all passes until the deliberate 10:07
   fixed-gain restart (RSS crept 47 -> 260 MB as logged; tracemalloc
   investigation stays on the backlog). Post-restart RSS 89 MB.
   Console ring was reset by the restart, so no overnight console
   evidence remains — the per-pass entries above are the record.
3. SATNOGS BLOCK LIFTED: db.satnogs.org answers in ~0.12 s from the
   Pi with real JSON (satellite 25338 fetch verified). The loader's
   30-min retries self-populated tonight's history entries with
   transmitter metadata — nothing was triggered manually. Notable
   data now in pass_history: NOAA 15 APT 137.62 marked INACTIVE,
   NOAA 18/19 APT marked inactive (confirms our dark-transmitter
   findings), Meteor-M 2-4 lists LRPT at 137.9125/137.9/137.1 MHz in
   both 80k and 72k modes.
4. CELESTRAK STILL BLOCKED (timeout, HTTP 000 after 8 s) — but
   tle_age_min is 33 (fresh): TLE syncs are succeeding (SatNOGS-side
   source), so no action needed.
5. ANOMALY (fixed gain, for the 11:35 evaluation): the 10:19 ISS
   pass — the FIRST recorded after the 29.7 dB manual-gain deploy —
   peaked at 22.4 vs the usual ~150 noise floor. Recordings are
   normal-sized (48 MB WAVs), so capture worked; the low peak
   suggests 29.7 dB leaves the primary under-driven relative to AGC
   (or the RSSI scaling changed with manual gain mode). The 10:45
   NOAA 15 pass + 11:35 cron must judge this from the audio/waterfall
   before deciding whether 29.7 dB is the right fixed value.
- Next: 10:45 NOAA 15 10.6° (gain smoke test), 11:10 Meteor-M 2-3
  67.4° (decisive LRPT test), 11:12 NOAA 18 DSB 20°, then the 11:35
  cron evaluation (gain A/B + LRPT verdict). NESDR SMArt v5 arriving
  today — runbook in the 10:15 entry.

### 2026-10-08 11:40 CEST — gain A/B verdict + 11:10 LRPT test (contaminated)
(1) GAIN A/B — 29.7 dB IS TOO LOW. The audio comparison was a wash:
    on the 10:45 NOAA 15 pass both dongles produce statistically
    identical audio (RMS -16.0 vs -16.4 dBFS, 2.4 kHz band SNR -0.1 dB
    both — pure noise at 10.6° with a degraded transmitter), and the
    10:19 ISS noise floors are also identical. The FM demod path
    normalizes output level, so audio can NOT distinguish front-end
    drive. The IQ tells the real story:
      primary AGC (04:29/09:31): IQ rms 9.4-15.5 u8-units
      primary fixed 29.7 (11:10): IQ rms 1.29  => ~17-22 dB under AGC
      secondary FC0013 AGC: 2.1 before, 1.05 at 11:10 (also dropped)
    1.29/127.5 = 1% of ADC range: quantization eats ~3 of the 8 bits —
    fatal for weak digital (LRPT) decodes. The 22.4/31.3 pass peaks
    were the same effect (strength metric scales with front-end gain).
    VERDICT: AGC wins; if we want manual gain it must sit near the
    R820T max (~42-49.6 dB), not 29.7. Recommend reverting the
    primary to AGC (user decision; needs a restart in an idle window)
    or retrying fixed ~42 dB on a good NOAA 15 pass. The NESDR v5 will
    redo this experiment properly on a TCXO device.
(2) 11:10 METEOR-M 2-3 67.4° LRPT: NO PRODUCTS (0-byte CADU both
    dongles, empty MSU-MR) — but the test is CONTAMINATED by the
    under-driven primary. My independent IQ re-measure (5 windows,
    4096-pt FFTs, elevated-power scan on the negative side):
      primary (fixed 29.7): no clean plateau; broad +2..+3 dB
        humps only, peak excess 18 dB = narrow spur (the -55 kHz
        interferer), no LRPT-shaped band.
      secondary (AGC): no elevated power above 6 dB anywhere.
    So no strong LRPT signal at 67.4° on either dongle — leaning
    "Meteor-M 2-3's LRPT is quiet too" (same family as M2-4), but the
    primary's quantization under-drive weakens the evidence. Repeat
    the test with proper gain on the next high M2-3 pass.
(3) CONSOLE RING DESTROYED BY RE-PREDICT SPIN: at 11:30:06-11:30:10
    the 30-min re-prediction loop filled all 100 console lines
    ("Predicted 31 passes in next 24h" + per-pass lines every tick),
    evicting ALL evidence of the 10:45/11:10 passes including the
    "Baseband centering ... measured at" line. The backlog quirk is
    worse than assumed: every half hour the ring loses its last
    30 min of pass history. Escalate: re-predict should log once, not
    per tick, or the ring should be larger / re-predict lines filtered.
(4) 11:12 NOAA 18 DSB pass: never happened — no recordings, no
    history entry; current predictions list NOAA 18 next at 12:52
    (70°). The 10:05 plan line was stale (prediction moved).
- Passes at 11:35: NOAA 19 53° at 11:38 (active during this check),
  ISS 58° 11:56, NOAA 18 70° 12:52 (good DSB test), Meteor-M 2-3 13°
  12:52 (too low to matter).

### 2026-10-08 12:35 CEST — NESDR SMArt v5 is the new primary
User removed the FC0013 (00000991) and connected the NESDR SMArt v5.
Enumeration at 12:24 was automatic: SN 48263793, "Nooelec NESDR
SMArt v5", rtl_tcp up, waterfall card, no restart needed. A brief
"Connection reset by peer" storm at 12:23 (replug) self-healed; the
stale FC0013 retry loop cleared on the restart below.
- Deployed (commit 325ccee): PRIMARY_DONGLE_SN = 48263793; the old
  R820T (77771111153705700) demoted to secondary. SDR_DONGLE_GAIN
  emptied — both dongles on AGC per the 11:40 A/B verdict (29.7 dB
  under-drove the ADC ~17-22 dB).
- v5 correction: 0 Hz "unmeasured dongle" — near-correct for a 0.5
  ppm TCXO (±69 Hz at 137 MHz). To measure properly: FM pilot method
  (103.0 MHz pilot) or rtl_test -p in an idle window; add a
  SDR_DONGLE_CORRECTIONS entry for 48263793 then. A fixed-gain value
  for the v5 also needs MEASURING (sweep gain, find the knee), not a
  guide number — see the 29.7 dB lesson.
- Test suite on the Pi: all 5 test scripts pass STANDALONE
  (test_radio: "ALL RADIO TESTS PASSED"). NOTE: `unittest discover`
  breaks test_radio (shared-process state — the rtl_tcp stand-in
  'sleep' spawn fails) — run the scripts individually, the docstring
  way: python3 -u tests/<name>.py.
- Receiver restarted 12:32 (idle window before the 12:52 NOAA 18
  pass). Both dongles up: v5 primary AGC (signal 524 idle — notably
  above the R820T's 145 noise floor; waterfall shows whether that is
  antenna gain, AGC drive, or local RF — watch), R820T secondary AGC
  at its normal 145.
- v5'S FIRST PASS as primary: NOAA 18 70° at 12:52 (DSB mode, IQ +
  SatDump noaa_dsb) — the shakedown. Meteor-M 2-4 32° at 14:15 gives
  the v5's first LRPT attempt with a TCXO.

### 2026-10-08 13:08 CEST — v5 shakedown pass done; DC-spike verdict + blanking removed; TLE sync bar
(1) 12:52 NOAA 18 (DSB 137.35, 70°) — NESDR SMArt v5's first pass as
    primary: CAPTURE OK, one anomaly. signal_peak 939.3, full coverage,
    file sizes normal. ANOMALY: the v5's WAV/IQ SPLIT mid-pass —
    125108 recorded 12:51:08 to ~12:53:2x (62 MB IQ), then a fresh file
    125329 ran to pass end (302 MB); together they match the R820T's
    unbroken 370 MB. Cause unknown: the console ring with the exact log
    line was destroyed by the 13:07 restart (below). Suspects: a
    momentary rtl_tcp/USB hiccup on the v5 (precedent: the 12:23 replug
    "Connection reset by peer" storm) or a WAV write error (disk NOT
    the cause — 37G free). WATCH: whether it recurs at 13:19 NOAA 19;
    if it does, escalate before trusting the v5 for unattended passes.
(2) DSB DECODE — first _dsb attempt ever (no baseline; the 00:36 NOAA 18
    pass predates IQ-era products):
      v5 125108 (short segment): PRODUCTS — HIRS + SEM (real NOAA DSB
        instruments), dataset.json "Unknown NOAA", timestamp 0.0.
      v5 125329 (main segment): 0-byte tip, no products.
      R820T full pass: 8216-byte tip, no products.
    Pattern: the only decode with products ran from the RAW iq.u8; the
    two failures are exactly the files with freshly generated
    .centered.c32 (1.2/1.5 GB mistune-centering products of the new
    centering path; no c32 exists for 125108). Next step when idle:
    re-run SatDump noaa_dsb on a big file directly from iq.u8 and/or
    inspect the centering output — prime suspect for the DSB failure.
(3) DC SPIKE VERDICT (from the 12:52 pass IQ, 120 FFT windows each):
    v5 0.43x row max, 0% of windows dominated; R820T 0.32x, 0%;
    removed FC0013 reference 0.73x, 11%. The waterfall blanking
    (c9fc3f9) existed only for the FC0013 → REMOVED in 4bfc8ae (rows
    normalize by the plain row max again, pre-2026-10-06 behavior).
    Deployed + receiver restarted 13:07:17 (idle window, NOAA 19 next
    at 13:19); live row verified unblanked (spike bins now vary, spike
    peak 135 vs row max 255). Both dongles back up after restart.
(4) TLE sync progress bar (2817a6d, static index.html — no restart):
    bar beneath the Pass Data box on Sync-now click; starting state
    bridges the <=10 s scheduler pickup, then tle.active/done/total/
    current from status.json drive it; hides itself when finished.
- Next: 13:19 NOAA 19 28° (watch for the WAV-split recurrence on the
  v5), 13:33 ISS 62°, 14:15 Meteor-M 2-4 32° (v5's first TCXO LRPT try).

### 2026-10-08 14:30 CEST — decode chain deep-dive: two real bugs fixed; all tracked digital transmitters dark
(1) SIGN BUG in the decode centering (fixed, ac7d0ad): _measure_signal_offset
    returns the signal's POSITION (negative Hz; -71425 measured live on the
    04:29 M2-4 file) but _decode_iq passed that value as the ROTATION.
    frequency_shift rotates BY its argument, so every decode whose
    measurement succeeded rotated the signal to twice its offset — aliased
    out of the demod band. The +60 kHz fallback is a rotation and was
    correct, so only measurement-failed (weak) decodes were ever centered
    right. Mechanically explains the whole 0-byte-CADU LRPT streak.
    Fixed: shift_hz = -measured. Verified: synthetic signal at the measured
    -71425.78 Hz lands at 0.0 Hz; live console now logs "measured at
    -84.3 kHz, rotating 84.3 kHz" / "NOT FOUND, assuming -60.0 kHz,
    rotating 60.0 kHz".
(2) BUT: no satellite signal was ever in the failing files. Time-resolved
    FFT drift analysis (fixed peaks only, zero Doppler drift — impossible
    for a satellite carrier) across 04:29 M2-4 (80.8°, healthy AGC, rms
    9.4-15.5), 06:10 M2-4, 09:31 M2-3, 12:52 NOAA 18 (BOTH dongles, 70°)
    and 13:18 NOAA 19 (v5): no satellite anywhere. The 10:15 entry's
    "clear +8 dB / 72 kHz LRPT plateau" does not reproduce — the
    measurement was fooled by a broad ~2x noise hump (AGC pumping/tuner
    shape), and the "-13 kHz off-nominal M2-4 LRPT" claim came from the
    same fooled measurement (noise hump sits at raw -71.4 kHz = -11.4 kHz
    in waterfall coords).
(3) M2-3 TRACKING FREQUENCY (fixed, c594048): tracked at 137.1 MHz —
    ~800 kHz below its LRPT (SatNOGS: 137.9125/137.9; the 137.1 entries
    are stale duplicates). Every M2-3 recording tuned dead spectrum.
    Now 137912500. First correctly-tuned M2-3 passes: TODAY 20:56 (46.1°)
    and 22:36 (29.4°).
(4) DSB verdict: NOAA 18 DSB (137.35, tuned right, 70°) and NOAA 19 DSB
    (137.77 per SatNOGS, tuned right, 28°) — no carrier on either dongle.
    The 12:52 "HIRS/SEM products" were SatDump scaffolding on noise
    (telemetry words all -1, "Unknown NOAA", timestamp 0). The dashboard
    also cannot show _dsb products at all (only APT PNGs) — feature gap,
    moot while the DSBs are dark.
(5) v5 PASS-TIME STREAM CORRUPTION (open hardware issue): during passes
    the v5's IQ shows equal-power mirror pairs (±15.7/±45.4 kHz at 12:52,
    ±25.3 kHz at 13:18) and, live at 14:15-14:21, an artifact cluster at
    waterfall -85.8 kHz (= raw -145.8 kHz — outside the band,
    impossible for a real signal) averaging 206/255. Parked streams are
    clean. Correlates with recording activity → suspect USB/CPU load:
    the 10:15 runbook's POWERED HUB recommendation, plus port/cable
    swap if it persists. The one-off WAV split at 12:53:29 did NOT recur
    (13:18 and 14:15 passes: single continuous files, no console errors).
(6) CORRECTION to the 13:08 entry: the 13:07 restart killed two in-flight
    SatDump decodes (proof: their .centered.c32 temp files survived —
    the finally-clause never ran). "Centering = prime suspect for the
    12:52 DSB failure" was wrong: those files contained no satellite.
    Leftover c32s (2.7 GB) removed 13:44.
(7) 14:15 M2-4 32.4°: LRPT confirmed OFF live — at max elevation both
    dongles show nothing at the target (R820T target-band/median 1.07);
    fixed-code decode ran cleanly on empty spectrum.
(8) Real-data outlook today (pipeline correct end-to-end for the first
    time): M2-4 15:55 (43.8°), M2-3 20:56 (46.1°, first ever correctly
    tuned), M2-3 22:36 (29.4°). NOAA 15 APT keeps producing its degraded
    "snowstorm" images. Watches scheduled 16:08 / 21:10 / 22:52.

### 2026-10-08 14:40 CEST — dashboard recording-pause switch deployed (user request)
User wants to stop collecting garbage recordings while reception quality
is being fixed. Added a global pause: System Status panel -> "Automatic
Recordings" -> Pause button (also reachable as /record_pause?paused=1|0,
state in status.json:recordings_paused). While paused the receiver still
tracks passes (tuning, Doppler, waterfall, live audio) but no WAV/IQ
files are opened; a running WAV closes within one IQ block. Paused passes
still get a pass-history entry (peak, no wav). Flag is in-memory — a
restart resumes recording.
Deploy: restart in the idle window after the 14:39 NOAA 18 set, before
the 15:11 ISS rise. Recordings LEFT PAUSED after deploy per user intent
— note this skips the 15:55 M2-4 43.8 deg LRPT outlook pass (and the
16:08 watch) unless the user resumes first.

### 2026-10-08 14:38 CEST — pause switch deployed; restart was mid-pass (user-ordered)
Correction to the entry above: the user ordered the restart immediately
("I still only receive garbage"), so it happened at 14:36 DURING the
low 14.7 deg NOAA 18 DSB pass (dark transmitter, garbage either way),
not in the idle window. Sequence verified live from console.json:
14:37:36 scheduler re-triggered the pass after restart, 14:37:38 both
dongles opened WAV+IQ, 14:37:46 /record_pause?paused=1 -> both closed
within the same second; status.json recording:False while pass_active
stays True (tracking/waterfall/live audio keep running). Recordings are
PAUSED now. Leftover truncated NOAA_18_20261008_143738*.{wav,iq.u8}
(both dongles, ~0.8 MB / 3.9 MB each) from the restart window — left on
disk for now. The 15:55 M2-4 LRPT outlook pass will not be recorded
while paused (16:08 watch moot unless resumed).

### 2026-10-08 14:41 CEST — all recordings wiped (user request: "not a good one yet")
User confirmed none of the recordings so far were usable. Wiped
/var/log/noaa/recordings/ entirely (291 entries, 11 GB: NOAA 15/18/19,
Meteor-M 2-3/2-4, ISS; WAV, IQ, PNGs, thumbs, decode markers, SatDump
product dirs). Recordings were paused at the time (nothing in-flight);
recordings.json now lists 0, 48 GB free. pass_history.json KEPT — its
signal-peak-per-pass data is diagnostic value for the reception-quality
work, but its wav/png links now dangle (decode/delete buttons will 404).

### 2026-10-08 16:05 CEST — AIS receiver built: Danube ship traffic on a dedicated dongle (user request)

User wants to listen to AIS messages from Danube ships. Built the full
receive chain as a new module `noaa_receiver/ais.py` — pure NumPy, no new
dependencies, mirroring the existing rtl_tcp architecture:

- A dongle pinned by calibration.AIS_DONGLE_SN (currently None — feature
  is OFF until one is plugged in and its serial set; any spare dongle
  works, the console prints serials at enumeration) is excluded from
  satellite tracking, never becomes primary, and /tune_dongle rejects
  it. Its rtl_tcp is parked at 162.000 MHz +60 kHz offset; both AIS
  channels (A 161.975 / B 162.025) sit at -25/-75 kHz and are demodulated
  from the same 240 kHz IQ stream: per-channel rotation, 61-tap 14 kHz
  low-pass, discriminator, decimation to 48 kHz (5 samples/bit).
- Radio layer conventions cross-checked against dgiardini/rtl-ais'
  aisdecoder source (NRZI 0=transition, flags 0x7E, stuff-0-after-five-1s
  on the transmitted stream, SDLC CRC-16 with the 0xF0B8 magic residue,
  AIVDM bit vector = transmitted stream with each byte REVERSED) and
  then VALIDATED against a real over-the-air capture: the Helsinki
  210-messages recording from the freerange/ais-on-sdr wiki decodes —
  known Finnish ships, names (AILA, JOANNA SATURNA...), plausible
  positions/speeds/courses. A 3 s slice + one real type-5 payload are
  committed as test fixtures.
- Demod note (measured): AIS is GMSK BT<=0.4 (ITU M.1371: 0.4 max on
  25 kHz channels, index 0.5 = +/-2.4 kHz). Frequency slicing must use
  near-transparent smoothing — a wide low-pass or matched filter crushes
  the 4800 Hz level alternation of 00 bit pairs (their amplitude is only
  ~200-500 Hz after BT=0.3/0.4 premodulation) and mis-slices them; the
  rtl-ais-style approach (light smoothing + point sampling at bit
  centers, 5-phase search per burst) works. Single-bit errors are caught
  by the frame CRC; positions are also sanity-checked (|lat|<90,
  |lon|<180 — a CRC-passing garbage position was observed in the capture).
- Messages 1-5, 9, 11, 18, 19, 21, 24, 27 decode into a ship table
  (state.ais_ships, keyed by MMSI); /ais.json serves it plus per-channel
  stats and a raw AIVDM feed (receiver-side armored, NMEA-checksummed,
  2 fragments for type-5-length payloads). Ships expire after 30 min of
  silence. Dashboard: new 'Danube Traffic' section under Upcoming Passes
  (hidden while AIS_DONGLE_SN is None), 10 s refresh, Google Maps links
  per ship.
- tests/test_ais.py: CRC test vector, armoring round-trip incl. the
  gpsd example sentence, end-to-end synthetic RF through the PRODUCTION
  per-block chain, and the real-capture conformance asserts. All pass;
  test_dsp also still passes (test_decode/test_radio need noaa-apt and
  rtl_tcp binaries and fail identically with and without these changes
  on the Windows dev box).
- NOT YET DEPLOYED/TESTED LIVE on the Pi: no AIS dongle attached yet,
  and real-antenna behavior at 162 MHz is unmeasured. Next step when a
  third dongle arrives: set AIS_DONGLE_SN in calibration.py, watch the
  console for the rtl_tcp start line, and check the waterfall for two
  faint carriers at +/-25 kHz of center; ships should appear on any
  Danube movement.

### 2026-10-08 16:30 CEST — AIS dongle assigned: the old R820T (77771111153705700)

User picked the generic R820T (the previous primary, currently the
comparison dongle) for AIS. AIS_DONGLE_SN is set; consequences:
- The R820T leaves satellite duty: satellite passes are now received by
  the v5 alone, and the dashboard's per-dongle receive comparison is
  down to one satellite dongle (dongle-comparison features degrade
  gracefully — the AIS dongle still shows as a card with its 162 MHz
  waterfall).
- Tuning correction at 162.000 MHz comes from the R820T's 80 ppm
  fallback: +12960 Hz. Its measured band corrections were all in the
  +72..82 ppm range, so the residual after the fallback should be well
  under 1 kHz — harmless for the demod (per-burst DC removal, 14 kHz
  channel filter). If the two AIS carriers sit visibly off +/-25 kHz in
  the waterfall, measure and pin 162000000 in the 'freqs' table.
- Antenna: dedicated 162 MHz vertical recommended (AIS is vertical
  pol); until one is mounted, the dongle can test-decode through the
  137 MHz antenna (mismatch costs a few dB but close Danube traffic
  should still decode). To verify after the next restart: console shows
  'AIS receiver on dongle 77771111153705700', waterfall shows two faint
  carriers +/-25 kHz around center.

### 2026-10-08 16:50 CEST — AIS deployed to the Pi; two deploy bugs fixed; Pi hard-crashed once; v5 off USB (replug needed)

Deploy via SSH (eugene@192.168.3.245, repo clone = /home/eugene/aprs_website,
start via crontab @reboot noaa_receiver.sh). Timeline and findings:

- 16:22 first start: satellite capture threads ALL died with NameError —
  the AIS wiring edit had dropped SDR_DONGLE_GAIN from radio.py's
  calibration import (test_dsp/test_ais didn't exercise radio's runtime
  namespace; test_radio's identical-looking pre-existing failure on the
  Windows box masked it). Fixed (9da7166), added a namespace guard at
  the top of tests/test_radio.py, restarted 16:25: clean.
- Dashboard was broken by a syntax error in the new AIS panel
  (fetchAIS): a heredoc-escaping accident wrote a RAW NEWLINE inside
  the .join() string literal -> the whole <script> block failed to
  parse, blanking the entire dashboard (user reported index:2237:101).
  Fixed byte-verified, whole script block now checked with
  node --check (236a237).
- 16:36-16:38 the Pi hard-crashed mid-diagnostic (was briefly parked on
  161.975 via /tune_dongle for a spectrum comparison; command timed
  out, machine stopped answering ping AND the public site went down,
  then rebooted itself at 16:38:14). No journal survives (volatile
  journald), no undervoltage flag in the new boot. Cause unknown —
  power brownout or USB cascade are the candidates (known flaky-USB
  history). Watch for recurrence; if it crashes again under AIS load,
  suspect the PSU.
- After the reboot the v5 (48263793) is OFF the USB bus (lsusb shows
  only the R820T) -> satellite tracking/recording is DOWN until it is
  physically replugged (sdr_thread re-enumerates every 10 s, no restart
  needed). The R820T auto-became primary per the fallback rule, so the
  main waterfall currently shows the AIS band. NOAA 15 pass 17:11 will
  be missed unless the v5 is back before rise.
- AIS side runs fine: rtl_tcp tuned 162,072,960 Hz (center+60k offset
  +12960 ppm fallback), both channels demodulating, floor ~2-6 u8
  units, no errors. BUT the AIS spectrum is FLAT at both channels
  (peaks == noise floor): the R820T hears nothing yet. What antenna is
  it on? If it shares the 137 MHz antenna it should still show close
  traffic; zero carriers plus zero decoded frames suggests a poor/
  disconnected antenna path. Next on-site: replug the v5, check the
  R820T antenna, watch the waterfall for two faint carriers +/-25 kHz
  around center.

### 2026-10-08 17:05 CEST — CORRECTION to 16:50: no crash — the Pi was moved indoors

The "hard crash" was the user unplugging the Pi and carrying it inside
ahead of rain (~16:33-16:38; the 16:38:14 boot is it coming back up on
the desk, same state as any power cycle). No PSU/kernel problem. The
v5 (48263793) is still off the USB bus after the move — replug pending;
satellite tracking stays down until then. AIS runs on the R820T as
before (0 frames so far; antenna situation after the move to be
re-checked — see next entry when the dongles/antennas are settled).

### 2026-10-08 18:15 CEST — AIS field measurements: system healthy, antenna is the bottleneck

Station moved indoors ahead of rain; each dongle has its own antenna (v5:
the 137 MHz V-dipole; R820T: a generic vertical whip). Measured with raw
rtl_sdr captures + the production decode chain offline:

- Tuning spot-on: AIS ch A/B land 0-37 Hz from expected (the 80 ppm
  fallback correction is right for this dongle at 162 MHz).
- RF path verified: strong FM station at 99.59 MHz received at +34 dB
  over the floor through the same dongle+antenna — antenna, coax and
  dongle all work.
- AIS signals PRESENT but FAINT: ch A +6.2 dB, ch B +8.5 dB over the
  noise floor (1-s FFT peaks exactly at the channel frequencies). No
  burst exceeded 1.9x the burst-gate floor in 90+90 s outside — ships
  in current range are too far/too few for the demod (~+13 dB needed).
  One strong +22 dB burst was seen earlier (indoor position), so closer
  ships will be decodable.
- Gain experiments: the R820T's manual gain table tops out ~9 dB BELOW
  what its AGC achieves (AGC floor 62 dB vs manual-max 53 dB in the
  same 1-Hz FFT units) — AGC (SDR_GAIN=0) stays the right choice for
  weak-signal AIS here; fixed gain only makes sense for strong-signal
  sites. rtl_sdr -g sweeps 29.3..49.6 dB all showed rms ~0.9 (the
  floor barely moves — ADC/post-tuner noise dominates at low RF input).
- CONCLUSION: everything except the antenna is proven (decoder
  validated on the real Helsinki capture; live pipeline healthy; both
  dongles running after the rain move). The generic whip is not
  resonant at 162 MHz and loses >10 dB — exactly the gap between the
  measured +6..9 dB carriers and the ~+13 dB decode threshold. Next
  hardware step: 46.3 cm quarter-wave ground plane (or a commercial AIS
  whip) outside in the clear. Expected: distant Danube traffic becomes
  decodable; close ships decode today's setup only when they pass.

### 2026-10-08 evening - frequency scanner + recorded-bandwidth fit (new feature)

Requested: per-dongle "scan" button that sweeps for strong signals and
stops on the next one found, plus a mechanism to keep the recorded band
from being too wide or too narrow. Implemented:

- `noaa_receiver/scan.py` (new): scan thread per dongle. It only writes
  the per-dongle frequency override - the capture thread applies each
  step as a live rtl_tcp retune (no stream gap), so the waterfall shows
  the swept spectrum in real time. Detection works on the raw FFT row
  now published as `entry['last_mag']` (radio.py): peak/floor ratio,
  median over a 0.7 s dwell, DC-spike window (+/-15 kHz around the
  +60 kHz offset center) masked. Hit threshold: ratio 3 (+9.5 dB).
  On a hit the dongle re-centers on the measured peak bin, confirms,
  then fits the recorded demod BW from the signal's measured width
  (half width + 30%, clamped 1-120 kHz).
- `/scan_dongle?d=&start=&end=&step=&ratio=` + `&stop=1`;
  `/fit_bw?d=` fits the BW at the current tune any time (card button).
  Scan state in `dongles.json` (`scan` field).
- Dashboard: per-card Scan row (from/to/step + Scan/Stop toggle, live
  status line) and a Fit button in the Recorded-BW row.
- Safety: scanning parks the dongle (no pass recording on it, same as a
  manual tune). Primary dongle: scan refused during a pass and
  auto-aborts (rejoins the shared frequency) when a pass rises
  mid-scan. Nothing found -> override restored to pre-scan state.
- Tests: `tests/test_scan.py` (measurement, spike masking, width fit,
  end-to-end sweep with fake dongle rows: nothing/found/stop cases) -
  all pass. test_decode/test_radio failures on this Windows box are
  pre-existing POSIX dependencies (verified identical on clean HEAD);
  they pass on the Pi.

State: feature complete locally; deploy + restart pending (avoid
mid-pass windows).

### 2026-10-08 ~18:00 CEST - scan shakedown: three live hardening rounds, validated

The scanner's first two live FM-band runs parked on PHANTOMS (87.5408,
98.403, 88.42 MHz - nothing at center once parked, widths 1-15 kHz
instead of broadcast FM's 150+). Three root causes, each fixed and
regression-tested (tests/test_scan.py 4d/4e/4f):

1. Site impulse noise: every 34 ms FFT row carries a saturating bin at
   a WANDERING offset (visible in any waterfall batch - each row is
   peak-normalized to 255, so the row max tells nothing; the OFFSET
   wandering is the tell). Per-row peak/floor detection hit on it at
   every tune. Fix: detection runs on the TIME-AVERAGED row (0.7 s
   dwell, ~40 rows), each row clipped at 8x its median first - a
   stationary carrier keeps its level, impulses are bounded and
   diluted. This matches how the +34 dB 99.59 MHz station was measured
   (1 s averaged FFTs).
2. rtl_tcp delivery stall right after a retune (<5 rows in one dwell)
   ended a scan as 'stopped'. Fix: _sample extends the window until
   the dwell elapsed AND >=5 rows arrived (hard bound 4 dwells); only
   a dead stream ends the scan, as 'nodata' (override restored).
3. Tune-relative artifacts (spurs that follow the tuner, e.g. +40.8
   kHz - just outside the DC-spike mask): passed the confirm because
   the confirm only re-checked the ratio. Fix: after re-centering, the
   confirm requires the peak within +/-5 kHz of 0 Hz - a real signal
   stays put when the dongle tunes onto it, a spur moves away.
   (Watch: repeated hit-recenter-reject cycles double the step time in
   spur-infested bands - cosmetic, the sweep keeps going.)

FINAL LIVE VALIDATION (v5 primary, FM band 87.5-108, 200 kHz steps):
scan parked at 92.0212 MHz, +10 dB, card center-band signal 1255 (vs
~150 floor - a real, persistent, strong carrier), fitted BW 35 kHz;
/fit_bw at the parked tune measured 68.4 kHz wide / +16.9 dB and set
the demod to +/-44.5 kHz (FM broadcast modulation makes the
instantaneous peak wander ~10 kHz, so re-fitting after parking gives a
better width estimate - that is what the Fit button is for). Sync
released the dongle afterwards; tracking resumed, NOAA 15 18:46 pass
unaffected.

Tooling notes for this repo (Windows dev box): Git Bash heredocs
mangle \uXXXX sequences into U+FFFD - patches touching JS escapes must
go through write_file'd Python scripts, not heredocs; the edit tool
cannot match CRLF files (normalize to LF first); test_decode/test_radio
fail on Windows identically with and without changes (POSIX fakes /
chmod), they pass on the Pi.

## 2026-10-09 — dongles decoupled: network rtl_tcp servers

The receiver no longer owns USB dongles. Each dongle is now served by a
persistent `rtl_tcp` daemon on the machine it is plugged into (the Pi
"Krabstral", 192.168.3.245: `rtl-tcp@<serial>.service` user units bound to
0.0.0.0, serial→port map in `~/rtl_tcp_daemon.sh`), and the receiver
(now on 192.168.2.73) connects over the network. Dongles are keyed by
`host:port`, added/removed at runtime from the dashboard
(`/add_dongle`, `/remove_dongle`; persisted in `dongles.json`), and the
primary/AIS/correction tables in calibration.py moved from serials to
addresses. Removed: local rtl_tcp spawning, serial enumeration
(rtl_sdr -d 99 probing), stale-process killing, port-conflict checks,
PLL log checking. The old receiver on the Pi was stopped and its
@reboot entry removed; the Pi now runs only the daemons.
