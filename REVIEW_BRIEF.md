# Review Brief: prawnceiver RF/DSP core (radio.py, dsp.py, decode.py)

You are reviewing the three most critical files of **Prawnsceiver**, a
Raspberry Pi 4 based NOAA/Meteor weather-satellite ground station. This
brief is self-contained: everything you need to know about the surroundings
is summarized below. The files under review implement the entire receive
path from raw dongle IQ to demodulated audio, plus post-pass image decoding.

## System context

- Hardware: Raspberry Pi 4 + two RTL-SDR dongles (RTL2832U/R820T "primary",
  RTL2832U/FC0013 "secondary"), each served by its own `rtl_tcp` process
  (localhost, one TCP port per dongle). A V-dipole antenna for 137 MHz.
- Software: one Python process, three thread groups: a pass scheduler, one
  capture thread per dongle (this code), and an HTTP server (dashboard/API).
- Config constants that matter here: sample rate 240,000 Hz, FFT size 512,
  IQ block 1024 bytes (= 512 complex samples), audio rate 48,000 Hz,
  tuner offset +60,000 Hz (dongle is tuned above the target and the signal
  is shifted back in software to displace the tuner's DC spike), FM band
  87.5-108 MHz, per-dongle crystal corrections in calibration.py.
- Satellites: NOAA 15/18/19 (APT analog weather images, 137.1-137.9 MHz FM),
  ISS (SSTV Robot 36 during events, 437.55 MHz), Meteor-M 2-3/2-4 (LRPT
  digital, 137.1/137.9125 MHz - recorded but NOT decodable, see decode.py).

Signal flow per dongle:

```
rtl_tcp socket ──1024 B IQ blocks──▶ offset shift (-60 kHz rotator)
    ├─▶ Doppler rotation (live NCO, scheduler-updated during passes)
    │       ├─▶ FFT every 8th block ──▶ waterfall + signal strength
    │       └─▶ FM demodulate ──▶ 48 kHz int16 audio
    │               ├─▶ live audio ring (streamed as /live.wav)
    │               └─▶ WAV file, only while a pass is active
WAV ──(pass end)──▶ decode_recording() ──▶ PNG image
```

## What each file does

### radio.py — dongle control and capture

- `enumerate_dongles()`: runs `rtl_sdr -d 99` as a probe (it prints the
  device list to stderr, then fails), regex-parses `idx: label, tuner, SN:
  serial` lines, registers each dongle in `state.sdrs` keyed by serial
  (USB indices are unstable across replugs), assigns one rtl_tcp port per
  dongle, and picks the primary dongle once.
- `kill_stale_rtl_tcp()`: called once at startup. rtl_tcp children survive
  a kill of the parent server and keep both the dongle and their port
  claimed; a streaming rtl_tcp ignores SIGTERM (verified live), hence
  `pkill -9`.
- `sdr_thread()`: supervises one capture thread per dongle; also a stall
  watchdog (process alive but no IQ for 30 s -> SIGKILL, capture thread
  then restarts it).
- `sdr_capture_thread(serial)`: the core loop.
  - Spawns `rtl_tcp -a 127.0.0.1 -p <port> -d <serial> -f <tuned+offset+correction> -s <rate> -g <gain>`; waits for the port, reads the 12-byte
    `RTL0` handshake; refuses to silently use a foreign listener if its
    own child died immediately (port conflict detection).
  - Reads IQ blocks from the socket (`_recv_exact` handles partial reads),
    offset-shifts, applies live Doppler, computes the waterfall/signal FFT
    every 8th block (CPU budget on the Pi), blanks the DC spike bin, and
    normalizes each row to 0-255.
  - **Live retuning**: when the scheduler moves to another satellite's
    frequency, sends a 5-byte rtl_tcp command on the open stream
    (`_rtl_tcp_set`, command 0x01 + big-endian uint32) - no process
    restart, no stream gap (this replaced a kill/restart design that cost
    ~8 s of dead air per retune).
  - **Recording**: while a pass is active, each dongle writes its own WAV
    (primary without suffix, secondary suffixed with its serial). A band
    change or a pass switch between two satellites SHARING a frequency
    closes and reopens the WAV so one file never spans two passes/satellites.
    A dongle with a manual frequency override does not record at all.
  - FM demod band handling: broadcast FM tunes use the whole capture band
    with an 18 kHz audio low-pass; satellite modes use a 22 kHz IQ filter.
  - Restart path: backoff 5 s doubling to 60 s if the process keeps dying
    immediately (unplugged dongle), reset after a stable 30 s run.

### dsp.py — the per-block DSP chain

All filters/rotators carry state in a per-capture-thread dict (`new_state()`)
so block processing is mathematically identical to whole-signal processing
(two dongles demodulate concurrently - state must never be shared).

- `iq_to_complex()`: u8 IQ bytes -> complex64 ((v - 127.5)).
- `frequency_shift()`: rotates the band by the tuner offset. The phasor is
  periodic (period = fs/gcd(offset, fs) = 4 samples for 60 kHz @ 240 kHz),
  so it uses a tiny cached LUT with a continuous sample counter.
- `lowpass()` / `lowpass_audio()`: 25-tap windowed-sinc FIRs, block-tail
  carried across calls (mode='valid' convolution).
- `doppler_shift()`: the live Doppler rotator. The scheduler steps
  `doppler_hz` every ~10 s during a pass. The NCO phase accumulates in
  `nco_phase` across blocks AND across doppler updates (a frequency change
  must not jump the phase - each jump is a full-scale click in the
  demodulated audio). The phasor for a fixed doppler comes from a cached
  LUT (period fs/gcd), multiplied by a scalar `exp(i*phase)`; pathological
  gcds (huge LUT) fall back to direct `exp` evaluation. NOTE: an earlier
  implementation computed phase as `-2*pi*d*k/fs` with an absolute sample
  counter k - that jumps the phase at every doppler update and caused
  audible clicks every 10 s; the current design fixed it.
- `fm_demodulate()`: discriminator via conjugate product + arctan2 (phase
  advance per sample; chosen over diff(arctan) to avoid 2*pi branch-cut
  spikes), optional IQ/audio low-pass, decimation to 48 kHz on a continuous
  phase counter across blocks, scaled to int16.

### decode.py — post-pass image decoding

- `decode_recording()`: dispatches on the recording FILENAME (the capture
  thread names files `<sat>_<timestamp>.wav`): 'iss' -> `sstv` Python lib
  (Robot 36, saves repeated images as _2, _3, ...); 'meteor' -> refuse
  (LRPT is digital, noaa-apt would burn CPU for minutes and fail; the
  recordings are kept for the waterfall/history only); otherwise
  `noaa-apt` subprocess (satellite name guessed from the filename for the
  `-s` map-overlay argument, `-R auto` for rotation, 120 s timeout,
  success == output PNG exists).

## What to evaluate (suggested focus areas)

1. **DSP block continuity**: verify the claims that block processing equals
   whole-signal processing: FIR tails, decimation phase counter, rotator
   sample counter, NCO phase accumulation. Any per-block reset is a bug.
2. **Doppler NCO math**: the LUT periodicity argument
   (exp(i*(phase + dphase*j)) == exp(i*phase) * LUT[j mod period] requires
   dphase*period to be an exact multiple of 2*pi), float64 phase
   accumulation/modulo drift over hours, behavior at doppler == 0 and on
   doppler sign changes (approach vs receding).
3. **rtl_tcp protocol handling**: `struct.pack('!BI')` framing, partial
   socket reads in `_recv_exact`, the 30 s socket timeout vs the 30 s
   watchdog (interplay/edge cases), handshake validation, the foreign-
   listener detection (its own race: the child may die between the poll
   check and later reads).
4. **Concurrency**: which state accesses are under which lock
   (status_lock, per-dongle entry lock, signal_lock, the audio ring's
   Condition); the retune block reads `state.current_frequency` /
   `current_sat_name` and `manual_dongle_freq` - torn reads or stale values
   that could cause a missed retune, double WAV open, or recording while
   tuned elsewhere.
5. **Recording state machine**: WAV open/close on (a) pass start/end,
   (b) band switch during a pass, (c) same-frequency satellite switch,
   (d) capture-thread restart mid-pass (WAV splits into fragments - the
   pass-end history attribution then relies on file mtimes), (e) process
   cleanup paths. Can a WAV be left open, double-closed, or span bands?
6. **decode.py robustness**: filename-substring dispatch (a satellite whose
   name contains 'iss' or 'meteor' would misroute; also noaa-apt sat_arg
   silently absent for unknown satellites), subprocess timeout/error
   paths, output-PNG-exists as the only success signal.
7. **Performance sanity (Pi 4)**: per-block numpy costs at ~469 blocks/s
   per dongle x2 dongles, FFT every 8th block, the exp() cost in
   `doppler_shift`'s fallback path, string/list conversions of FFT rows.
8. **Restart/edge behavior**: backoff logic reset conditions, watchdog
   kill vs in-flight socket read, port collisions when dongles are
   re-enumerated after replug (ports are assigned once per serial).

## Known issues (already on the list - do not re-report unless you find more)

- `estimate_quality()` (quality.py, outside these files) returns 0% for
  every recording including good ones.
- The scheduler's pass-end attribution can log duplicate history entries
  for overlapping/same-frequency passes, and can attribute recording
  fragments between passes (mtimes decide).
- SatNOGS DB metadata for NOAA 19's APT transmitter is stale (marked
  inactive) - not a code issue, just context for test expectations.

## Behaviors already verified live (do not flag as unproven)

- Live retune latency measured at 0.04-0.41 s (5 band hops, no restarts,
  no audio gap); first real pass retune logged cleanly at pass start.
- The phase-continuity of the Doppler NCO was validated numerically (50 Hz
  step -> exactly the smooth per-sample advance; LUT vs direct exp match
  to 4e-8).
- rtl_tcp ignores SIGTERM while streaming (SIGKILL required) - this was
  established by experiment on this exact system.
- FM broadcast pilot-tone measurements through this chain validated the
  per-dongle crystal corrections to ~0.02 ppm.

## Environment for reproduction

Python 3.13, numpy, skyfield; rtl-sdr package (rtl_sdr, rtl_tcp, rtl_test);
noaa-apt binary at /opt/noaa-apt; recordings and logs under
/var/log/noaa/. The modules can be imported standalone (only `state`,
`config`, `calibration` dependencies) - DSP functions are pure enough for
unit testing with synthetic signals (generate a complex exponential, feed
blocks through the chain, check phase/frequency/continuity numerically).
