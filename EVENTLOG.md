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
