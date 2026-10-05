Prawnsceiver is a lightweight APRS (Automatic Packet Reporting System) monitoring station that turns a Raspberry Pi and a cheap DVB-T dongle into a live radio dashboard. It decodes APRS packets on the 2-meter band (144.8 MHz Europe simplex) and displays them on a web dashboard with a real-time RF waterfall.

Features
📡 Live FFT waterfall — real-time RF spectrum visualization from raw IQ samples
📦 APRS packet decoding — direwolf-powered 1200 baud AFSK decoding
🗺️ Web dashboard — packet list, system status, and live logs in your browser
🦀 Single-process architecture — Python server manages SDR, FFT, FM demodulation, and direwolf in one process
🍓 Raspberry Pi optimized — runs on Pi 4 with RTL2832U (R820T tuner), gain-optimized for 2m band
🔄 Auto-restart — crash-resistant with automatic SDR/direwolf recovery
🌐 APRS-IS IGate — relays received packets to the global APRS network
Hardware
Raspberry Pi 4 (or any Linux ARM device)
RTL-SDR dongle (RTL2832U with R820T tuner recommended)
Antenna for 144-146 MHz (stock whip works, dipole is better)
Software Stack
rtl_sdr — raw IQ capture from the dongle
direwolf — APRS demodulator (1200 baud AFSK)
Python 3 + NumPy — FFT computation and software FM demodulation
nginx — reverse proxy for the web dashboard
Quick Start
# Install dependencies
sudo apt install rtl-sdr direwolf nginx python3-numpy ffmpeg

# Clone and run
git clone https://github.com/yourname/Prawnsceiver.git
cd Prawnsceiver
python3 server.py
Then open http://your-pi-ip:8000 in your browser.

Why "Prawnsceiver"?
Because it's a prawn-ceiver — a transceiver with claws. Built with OpenClaw. 🦐
