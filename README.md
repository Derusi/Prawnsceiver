# Prawnsceiver — NOAAh's CrabArk

A Raspberry Pi and a cheap RTL-SDR dongle turned into a fully automated NOAA
weather-satellite ground station. It predicts passes, tunes the SDR to the
right satellite at the right time, records the APT signal, decodes it into
weather images, and serves everything on a live web dashboard.

Live at: https://prawnceiver.derusi.de

## Features

- 🛰️ Auto pass tracking — skyfield-based pass prediction for NOAA 15/18/19
  and the Russian Meteor-M 2-3/2-4 weather satellites, with automatic
  frequency switching and recording during passes (above 10°)
- 📡 Live FFT waterfall — real-time RF spectrum from raw IQ samples, with a
  frequency-feature overlay (APT band, demod filter, DC spike)
- 🔊 Live audio — software FM demodulation streams the downlink as a
  browser-playable WAV
- 🖼️ APT decoding — recordings are decoded to weather images with SatDump
  (wedge-calibrated channels, false-color composite, map overlay), one
  click from the dashboard
- 🛰 LRPT/DSB digital decoding — Meteor LRPT (OQPSK) and NOAA DSB
  recordings cannot be FM-demodulated: their raw IQ is decoded with SatDump
  (meteor_m2-x_lrpt / noaa_dsb) and the composite image lands next to the
  recording like an APT PNG
- 🛰️ ISS SSTV — ISS (Zarya) passes on 437.550 MHz are tracked and recorded;
  Robot 36 images from ARISS events are decoded automatically with the sstv
  tool (`RECORD_ISS` in config.py turns ISS recording off outside events)
- 🧭 Polar pass tracker — live az/el ground-track view of the active pass
- 📚 Pass history — every pass and recording is kept in a browsable history
  with decoded images
- 🦀 Crabs caught — every successfully decoded satellite image counts as a crab
- 🚢 AIS ship traffic — the dongle decodes Danube vessels on
  the marine AIS channels (161.975/162.025 MHz, GMSK 9600 baud) in software:
  name, position, speed and course of every ship within VHF range show up
  live on the dashboard. Any dongle can be switched to AIS and back from
  its own dashboard card (`🚢 Listen for AIS ships`) — with a single dongle
  the satellites pause while it listens for ships; a second dongle pinned
  via `AIS_DONGLE` in calibration.py keeps both running at the same time
- 🖥️ Network dongles — the SDR dongles are decoupled from the
  receiver: each dongle is served by an `rtl_tcp` daemon on the machine it
  is plugged into, the receiver connects over the network, and dongles
  are added/removed at runtime from the dashboard by IP address + port


## Hardware

- Raspberry Pi 4
- RTL-SDR dongle (RTL2832U with R820T tuner)
- Antenna for 137 MHz (VHF 137 MHz SATCOM or a crossed dipole works)
- Optional: any spare RTL-SDR dongle as a dedicated AIS receiver (the
  137 MHz antenna hears 162 MHz ships fine at close range; a ~46 cm
  quarter-wave whip is better)

## Software Stack

- rtl_tcp — one daemon per dongle, running on the machine the dongle is
  plugged into: raw IQ stream + tuning commands over TCP, offset-tuned
  +60 kHz to dodge the center DC spike
- Python 3 + NumPy — FFT waterfall and software FM demodulation
- Skyfield — TLE-based pass prediction (TLEs refreshed from SatNOGS with a
  Celestrak fallback every 3 h, plus a local cache)
- SatDump — APT (NOAA), LRPT (Meteor-M) and DSB (NOAA) downlink decoding
- sstv — ISS Slow-Scan TV decoding (Robot 36)
- nginx — HTTPS reverse proxy (Let's Encrypt) in front of the Python server
- noaa_receiver/ais.py — AIS demodulation/decoding (GMSK discriminator,
  HDLC deframing, CRC-16/SDLC, ship database) — pure NumPy, no extra deps

## Architecture

The dongles are decoupled from the receiver: each one is served by an
`rtl_tcp` daemon on the machine it is plugged into (see Setup below), and
the receiver is just a network client. Dongles are identified by their
`host:port` address and can be added and removed at runtime from the
dashboard (persisted across restarts in `dongles.json`).

The receiver itself runs in a single Python process (`server_noaa.py` → the
`noaa_receiver/` package) with three threads:

- `scheduler_thread` — TLE refresh + pass prediction + frequency switching
- `sdr_thread` — one capture thread per dongle: rtl_tcp IQ stream → FFT
  waterfall + FM demodulated audio (retunes in-band while streaming)
- HTTP server — dashboard, JSON API, recordings, decode/delete endpoints

```
rtl_tcp (dongle host) ──TCP IQ──▶ capture thread ──▶ FFT ──▶ live waterfall
                                        │
                                        └─▶ FM demod ──▶ live audio + WAV recording ──▶ SatDump ──▶ weather PNG
```

The scheduler tunes the dongle to the next satellite 60 s before each pass
rise and records until 60 s after set. Between passes it parks on NOAA 15.

## Setup

```bash
# Install dependencies
sudo apt install rtl-sdr python3-numpy nginx
pip install skyfield

# SatDump (APT / LRPT / DSB decoder)
sudo apt install satdump

# sstv (ISS SSTV decoder)
pip install sstv

# Clone and run
git clone https://github.com/Derusi/Prawnsceiver.git
cd Prawnsceiver
python3 server_noaa.py
```

Then open http://your-pi:8085 in your browser.

### Dongle host: rtl_tcp daemons

The dongles live on a separate machine (a Raspberry Pi in this station).
Install the RTL tools, keep the kernel from claiming the dongles for DVB-T,
and run one persistent `rtl_tcp` per dongle, bound to the LAN:

```bash
sudo apt install rtl-sdr
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf

# ~/rtl_tcp_daemon.sh: map each dongle serial to a port (see the repo's
# dongle-host/rtl_tcp_daemon.sh for the template), then:
# ~/.config/systemd/user/rtl-tcp@.service runs it with Restart=always.
systemctl --user enable --now rtl-tcp@48263793          # serial → port 1234
loginctl enable-linger                                   # start at boot
```

The receiver picks dongles up by address: open the dashboard and use
the **Add** field under "SDR Dongles (rtl_tcp)" (IP + port), or start
with a default list in `calibration.py` (`DEFAULT_DONGLES`).

For unattended operation, start the receiver at boot:

```bash
crontab -e
# add:
@reboot /path/to/noaa_receiver.sh
```

Put nginx with a proxy_pass to 127.0.0.1:8085 in front for HTTPS.

## Configuration

Station parameters live in `noaa_receiver/config.py`: coordinates
(`LAT`, `LON`), timezone offset (`UTC_OFFSET`), pass selection
(`PASS_MIN_ALT`, `PASS_PREDICT_HOURS`), SDR settings (`SDR_RATE`, `SDR_GAIN`,
`SDR_OFFSET_HZ`), log/record directories and the web port.

## Why "Prawnsceiver"?

Because it's a prawn-ceiver — a transceiver with claws. Built with OpenClaw. 🦐
