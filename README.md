# Prawnsceiver — NOAAh's CrabArk

A Raspberry Pi and a cheap RTL-SDR dongle turned into a fully automated NOAA
weather-satellite ground station. It predicts passes, tunes the SDR to the
right satellite at the right time, records the APT signal, decodes it into
weather images, and serves everything on a live web dashboard.

Live at: https://prawnceiver.derusi.de

## Features

- 🛰️ Auto pass tracking — skyfield-based pass prediction for NOAA 15/18/19,
  with automatic frequency switching and recording during passes (above 10°)
- 📡 Live FFT waterfall — real-time RF spectrum from raw IQ samples, with a
  frequency-feature overlay (APT band, demod filter, DC spike)
- 🔊 Live audio — software FM demodulation streams the downlink as a
  browser-playable WAV
- 🖼️ APT decoding — recordings are decoded to weather images with noaa-apt
  (map overlay, auto-rotate), one click from the dashboard
- 🛰️ ISS SSTV — ISS (Zarya) passes on 437.550 MHz are tracked and recorded;
  Robot 36 images from ARISS events are decoded automatically with the sstv
  tool (`RECORD_ISS` in config.py turns ISS recording off outside events)
- 🧭 Polar pass tracker — live az/el ground-track view of the active pass
- 📚 Pass history — every pass and recording is kept in a browsable history
  with decoded images
- 🦀 Crabs caught — every successfully decoded satellite image counts as a crab

## Hardware

- Raspberry Pi 4
- RTL-SDR dongle (RTL2832U with R820T tuner)
- Antenna for 137 MHz (VHF 137 MHz SATCOM or a crossed dipole works)

## Software Stack

- rtl_sdr — raw IQ capture from the dongle (offset-tuned +60 kHz to dodge the
  center DC spike)
- Python 3 + NumPy — FFT waterfall and software FM demodulation
- Skyfield — TLE-based pass prediction (TLEs refreshed from Celestrak every
  6 h, with a local cache fallback)
- noaa-apt — APT image decoding
- sstv — ISS Slow-Scan TV decoding (Robot 36)
- nginx — HTTPS reverse proxy (Let's Encrypt) in front of the Python server

## Architecture

Everything runs in a single Python process (`server_noaa.py` → the
`noaa_receiver/` package) with three threads:

- `scheduler_thread` — TLE refresh + pass prediction + frequency switching
- `sdr_thread` — rtl_sdr IQ capture → FFT waterfall + FM demodulated audio
- HTTP server — dashboard, JSON API, recordings, decode/delete endpoints

```
rtl_sdr ──IQ──▶ FFT ──▶ /waterfall.json ──▶ live waterfall
   │
   └──────────▶ FM demod ──▶ live audio + WAV recording ──▶ noaa-apt ──▶ weather PNG
```

The scheduler tunes the dongle to the next satellite 60 s before each pass
rise and records until 60 s after set. Between passes it parks on NOAA 15.

## Setup

```bash
# Install dependencies
sudo apt install rtl-sdr python3-numpy nginx
pip install skyfield

# noaa-apt (APT image decoder) — grab a release binary from
# https://github.com/martinber/noaa-apt/releases and put it on PATH

# sstv (ISS SSTV decoder)
pip install sstv

# Clone and run
git clone https://github.com/Derusi/Prawnsceiver.git
cd Prawnsceiver
python3 server_noaa.py
```

Then open http://your-pi:8085 in your browser.

For unattended operation, start it at boot (this is how the Pi is set up):

```bash
crontab -e
# add:
@reboot /home/eugene/noaa_receiver.sh
```

Put nginx with a proxy_pass to 127.0.0.1:8085 in front for HTTPS.

## Configuration

Station parameters live in `noaa_receiver/config.py`: coordinates
(`LAT`, `LON`), timezone offset (`UTC_OFFSET`), pass selection
(`PASS_MIN_ALT`, `PASS_PREDICT_HOURS`), SDR settings (`SDR_RATE`, `SDR_GAIN`,
`SDR_OFFSET_HZ`), log/record directories and the web port.

## Why "Prawnsceiver"?

Because it's a prawn-ceiver — a transceiver with claws. Built with OpenClaw. 🦐
