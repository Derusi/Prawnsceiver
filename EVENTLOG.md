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
