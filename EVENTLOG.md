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
- Cron schedule for this watch: see bottom of this file.

## Entries

### 2026-10-08 02:20 CEST — watch starts, state: healthy
- Pi at origin/main 42891d5, receiver up (pid 203005), all endpoints
  < 10 ms. Pass list shows receive plans (mode chips) for all passes.
- History page font fix (2a5d8bc) and receive-plan feature (e787b88,
  527d8e2, 42891d5) deployed. The API-freeze bug (SatNOGS fetch under
  status_lock) is fixed and verified.
- SatNOGS/Celetrak block: diagnosed 01:50-02:15, left as-is per user.
- Upcoming: 02:51 Meteor-M 2-4 LRPT (12.7°) — pipeline shakedown pass.
