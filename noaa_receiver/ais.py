"""AIS receiver: Danube ship traffic on 161.975 / 162.025 MHz.

A dongle pinned by calibration.AIS_DONGLE_SN is dedicated to AIS (it does
not join the satellite tracking). rtl_tcp is tuned to 162.000 MHz — the
midpoint of the two AIS channels — so both channels land at +/-25 kHz
inside one 240 kHz capture, and each is demodulated from the same IQ
stream: per-channel offset rotation -> 14 kHz low-pass -> FM
discriminator -> 48 kHz (5 samples/bit at 9600 baud).

Radio layer (ITU-R M.1371, conventions cross-checked against dgiardini/
rtl-ais' battle-tested aisdecoder and validated by decoding a real
over-the-air capture — see tests/test_ais.py):

- GMSK, modulation index 0.5 => +/-2400 Hz deviation; NRZI: a data 0
  toggles the level, a 1 holds it (only transitions matter, so the
  absolute level<->frequency polarity is irrelevant).
- HDLC framing: 0x7E flags; a 0 is stuffed after five consecutive 1s of
  the transmitted stream; payload + 16-bit FCS between flags.
- FCS: CRC-16/SDLC (poly 0x8408 reflected, init 0xFFFF) computed over
  the TRANSMITTED bit order; the register over payload+FCS ends at the
  magic residue 0xF0B8 (== ~0x0F47, the value aisdecoder checks).
- The AIS payload bit vector (the order AIVDM payloads and all field
  tables use) is the transmitted stream with each 8-bit group REVERSED
  (HDLC transmits bytes LSB first). Multi-slot messages (e.g. the
  424-bit type 5) are ONE continuous HDLC frame over both slots.

Demodulator: frequency slicing like rtl-ais (a wide low-pass or matched
filter would crush the 4800 Hz level alternation of "00" pairs, which
BT<=0.4 GMSK already attenuates to a few hundred Hz — measured; the eye
only stays open with near-transparent smoothing). Bit timing comes from
a 5-phase trial per burst (one candidate per 48 kHz sample offset within
a bit); frames are accepted only on flag pattern + CRC residue.

AIS transmitters occupy slots (26.67 ms) with >=3 ms guard between them,
so bursts are separated by quiet gaps: the capture thread tracks the
noise floor per channel, buffers 48 kHz discriminator samples while a
channel is active and decodes each closed burst offline.
"""
import json
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

from . import state
from .calibration import SDR_DONGLE_GAIN, correction_info, tuning_correction
from .config import (AIS_CENTER_HZ, AIS_CHANNEL_HZ, AIS_LOG_FILE,
                     AIS_SHIP_TTL_SECS,
                     LOGDIR, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE,
                     WATERFALL_ROWS)

# Discriminator decimation: 240 kHz -> 48 kHz = 5 samples per AIS bit
AIS_BITRATE = 9600
assert SDR_RATE % 48000 == 0, "AIS demod needs 240 kHz SDR_RATE decimating to 48 kHz"
AIS_DEC = SDR_RATE // 48000
AIS_FS = SDR_RATE // AIS_DEC

# ---------- HDLC / CRC primitives (transmitted-bit order) ----------

_FCS_POLY = 0x8408   # reflected CRC-16/CCITT (X.25 / SDLC)
FCS_RESIDUE = 0xF0B8  # register value after payload+FCS for a good frame
FLAG = (0, 1, 1, 1, 1, 1, 1, 0)

def crc16_register(bits, crc=0xFFFF):
    """SDLC CRC-16 register over transmitted bits, one bit at a time."""
    for b in bits:
        crc ^= int(b)
        crc = (crc >> 1) ^ (_FCS_POLY if crc & 1 else 0)
    return crc

def fcs_bits(payload_bits):
    """The 16 FCS bits to append (transmission order) for a payload."""
    return [(crc16_register(payload_bits) ^ 0xFFFF) >> i & 1 for i in range(16)]

def stuff_bits(bits):
    """Insert a 0 after five consecutive 1s (between flags only)."""
    out, run = [], 0
    for b in bits:
        if b:
            run += 1
            out.append(1)
            if run == 5:
                out.append(0)
                run = 0
        else:
            out.append(0)
            run = 0
    return out

def destuff_bits(bits):
    """Remove the 0 that follows five consecutive 1s; None on an abort
    (six 1s — that is a flag/abort, not data)."""
    out, run = [], 0
    for b in bits:
        if run == 5:
            if b:
                return None  # six 1s inside a frame = abort
            run = 0
            continue
        run = run + 1 if b else 0
        out.append(b)
    return out

def _find_flags(data):
    """Indices where the flag pattern 01111110 starts in a data-bit list."""
    d = np.fromiter(data, dtype=np.int8, count=len(data))
    if len(d) < 8:
        return []
    f = np.array(FLAG, dtype=np.int8) * 2 - 1
    corr = np.correlate(d * 2 - 1, f, mode="valid") == len(FLAG)
    return np.flatnonzero(corr).tolist()

def transmitted_to_payload(bits):
    """Transmitted-order bits -> AIS payload vector (reverse each byte)."""
    out = []
    for j in range(0, len(bits), 8):
        out.extend(reversed(bits[j:j + 8]))
    return out

def payload_to_transmitted(payload):
    """AIS payload vector -> transmitted bit order (reverse each byte)."""
    out = []
    for j in range(0, len(payload), 8):
        out.extend(reversed(payload[j:j + 8]))
    return out

def hdlc_frames(data_bits):
    """NRZI-decoded data bits -> valid AIS payload vectors.

    Frames are delimited by flags; content is destuffed and must carry a
    correct FCS (magic residue). Everything else is dropped silently —
    the CRC makes false positives vanishingly rare.
    """
    frames, seen = [], set()
    flags = _find_flags(data_bits)
    for a, b in zip(flags, flags[1:]):
        raw = data_bits[a + 8:b]
        if not 40 <= len(raw) <= 1200:   # 24 payload + 16 FCS min, 1008+stuffing max
            continue
        content = destuff_bits(raw)
        if content is None or len(content) < 40 or len(content) % 8:
            continue
        if crc16_register(content) != FCS_RESIDUE:
            continue
        payload = tuple(transmitted_to_payload(content[:-16]))
        if payload not in seen:
            seen.add(payload)
            frames.append(list(payload))
    return frames

# ---------- bit-field helpers (AIVDM payload-vector order) ----------

def u(bits, off, n):
    """Unsigned field, MSB first."""
    v = 0
    for b in bits[off:off + n]:
        v = v << 1 | int(b)
    return v

def i(bits, off, n):
    """Signed (two's complement) field, MSB first."""
    v = u(bits, off, n)
    return v - (1 << n) if v >> (n - 1) else v

def sixbit(bits, off, nchars):
    """Packed 6-bit ASCII text; '@' padding and trailing spaces stripped."""
    out = []
    for c in range(nchars):
        v = u(bits, off + 6 * c, 6)
        out.append(chr(v + 64) if v < 32 else chr(v + 32))
    return " ".join("".join(out).replace("@", " ").split())

def _pos(bits, lon_off, lat_off):
    """(lat, lon) in degrees from a position field pair; None when the
    position is not available or absurd (bit errors that pass the FCS,
    e.g. the 397 deg east 'ship' seen in the Helsinki capture)."""
    lon = i(bits, lon_off, 28) / 600000.0
    lat = i(bits, lat_off, 27) / 600000.0
    if abs(lon) >= 180.0 or abs(lat) >= 90.0:
        return None
    return lat, lon

NAV_STATUS = ("under way (engine)", "at anchor", "not under command",
              "restricted manoeuvring", "constrained by draught", "moored",
              "aground", "fishing", "under way (sailing)", "reserved",
              "reserved (WIG)", "towing astern", "pushing ahead",
              "reserved", "AIS-SART active", "undefined")

def _dims(bits, off):
    return {"a": u(bits, off, 9), "b": u(bits, off + 9, 9),
            "c": u(bits, off + 18, 6), "d": u(bits, off + 24, 6)}

def parse_payload(p):
    """AIS payload vector -> dict of the fields the ship table keeps.

    Returns None for unknown/uninteresting message types. Lengths are
    checked permissively (the wild shows 420-426 bit type 5s); fields
    beyond the available bits are simply not set.
    """
    if len(p) < 38:
        return None
    msg = u(p, 0, 6)
    mmsi = u(p, 8, 30)
    d = {"mmsi": mmsi, "msg": msg}
    if msg in (1, 2, 3):   # Position report class A (168 bits)
        if len(p) < 168:
            return None
        d["cls"] = "A"
        d["status"] = NAV_STATUS[u(p, 38, 4)] if u(p, 38, 4) < 15 else NAV_STATUS[15]
        rot = i(p, 42, 8)
        d["rot"] = None if rot == -128 else int(round((rot / 4.733) ** 2)) * (1 if rot >= 0 else -1)
        sog = u(p, 50, 10)
        d["sog"] = None if sog == 1023 else round(sog / 10.0, 1)
        pos = _pos(p, 61, 89)
        if pos:
            d["lat"], d["lon"] = pos
        cog = u(p, 116, 12)
        d["cog"] = None if cog == 3600 else round(cog / 10.0, 1)
        hdg = u(p, 128, 9)
        d["heading"] = None if hdg == 511 else hdg
    elif msg == 4 or msg == 11:   # Base station report / UTC response
        if len(p) < 134:
            return None
        d["cls"] = "BASE"
        pos = _pos(p, 79, 107)
        if pos:
            d["lat"], d["lon"] = pos
    elif msg == 5:   # Static + voyage data (424 bits, one 2-slot frame)
        if len(p) < 420:
            return None
        d["cls"] = "A"
        d["imo"] = u(p, 40, 30) or None
        d["callsign"] = sixbit(p, 70, 7)
        d["name"] = sixbit(p, 112, 20)
        d["shiptype"] = u(p, 232, 8)
        d["dims"] = _dims(p, 240)
        if len(p) >= 302:
            d["draught"] = round(u(p, 294, 8) / 10.0, 1)
            d["destination"] = sixbit(p, 302, 20)
    elif msg == 9:   # SAR aircraft position
        if len(p) < 128:
            return None
        d["cls"] = "SAR"
        d["sog"] = u(p, 50, 10)
        pos = _pos(p, 61, 89)
        if pos:
            d["lat"], d["lon"] = pos
        cog = u(p, 116, 12)
        d["cog"] = None if cog == 3600 else round(cog / 10.0, 1)
    elif msg == 18:   # Position report class B (168 bits)
        if len(p) < 112:
            return None
        d["cls"] = "B"
        sog = u(p, 46, 56 - 46)
        d["sog"] = None if sog == 1023 else round(sog / 10.0, 1)
        pos = _pos(p, 57, 85)
        if pos:
            d["lat"], d["lon"] = pos
        cog = u(p, 112, 12)
        d["cog"] = None if cog == 3600 else round(cog / 10.0, 1)
        hdg = u(p, 124, 9)
        d["heading"] = None if hdg == 511 else hdg
    elif msg == 19:   # Extended class B position (312 bits)
        if len(p) < 301:
            return None
        d["cls"] = "B"
        sog = u(p, 46, 10)
        d["sog"] = None if sog == 1023 else round(sog / 10.0, 1)
        pos = _pos(p, 57, 85)
        if pos:
            d["lat"], d["lon"] = pos
        cog = u(p, 112, 12)
        d["cog"] = None if cog == 3600 else round(cog / 10.0, 1)
        d["name"] = sixbit(p, 143, 20)
        d["shiptype"] = u(p, 263, 8)
        d["dims"] = _dims(p, 271)
    elif msg == 21:   # Aid to navigation (272-360 bits)
        if len(p) < 219:
            return None
        d["cls"] = "ATON"
        d["aid_type"] = u(p, 38, 5)
        d["name"] = sixbit(p, 43, 20)
        if len(p) >= 272:
            d["name"] += sixbit(p, 272, (len(p) - 272) // 6)
        pos = _pos(p, 164, 192)
        if pos:
            d["lat"], d["lon"] = pos
    elif msg == 24:   # Static data report (class B, parts A/B)
        if len(p) < 160 or u(p, 38, 2) > 1:
            return None
        d["cls"] = "B"
        d["part"] = u(p, 38, 2)
        if d["part"] == 0:
            d["name"] = sixbit(p, 40, 20)
        elif len(p) >= 168:
            d["shiptype"] = u(p, 40, 8)
            d["callsign"] = sixbit(p, 90, 7)
            if str(mmsi).startswith("98"):
                d["mothership"] = u(p, 132, 30)
            else:
                d["dims"] = _dims(p, 132)
    elif msg == 27:   # Long-range position (96 bits)
        if len(p) < 96:
            return None
        d["cls"] = "LR"
        pos = _pos(p, 44, 62)
        if pos:
            d["lat"], d["lon"] = pos
        sog = u(p, 79, 6)
        d["sog"] = None if sog == 63 else sog
        d["cog"] = u(p, 85, 9)
    else:
        return None
    return d

# ---------- AIVDM armoring (for the dashboard raw feed) ----------

def payload_to_aivdm(p, channel, seq):
    """Payload vector -> list of complete !AIVDM sentences (82-char limit:
    max 56 armored chars per fragment, as in rtl-ais' protodec)."""
    pad = (-len(p)) % 6
    bits = list(p) + [0] * pad
    chars = []
    for j in range(0, len(bits), 6):
        v = 0
        for b in bits[j:j + 6]:
            v = v << 1 | int(b)
        chars.append(chr(v + 48) if v < 40 else chr(v + 56))
    total = max(1, -(-len(chars) // 56))
    sentences = []
    for frag in range(total):
        part = "".join(chars[frag * 56:(frag + 1) * 56])
        fill = pad if frag == total - 1 else 0
        body = ("AIVDM,%d,%d,%s,%s,%s,%d" %
                (total, frag + 1, seq if total > 1 else "", channel, part, fill))
        csum = 0
        for ch in body:
            csum ^= ord(ch)
        sentences.append("!%s*%02X" % (body, csum))
    return sentences

# ---------- burst demodulator ----------

# Near-transparent smoothing (passes the 4800 Hz "00"-pair alternation);
# anything wider mis-slices those bits (measured on the prototype).
_SMOOTH = np.array([0.15, 0.25, 0.2, 0.25, 0.15], dtype=np.float64)

def decode_burst(freq48):
    """48 kHz instantaneous-frequency samples of one AIS burst ->
    valid payload vectors.

    The NRZI data bit is decoded per phase trial (5 samples/bit => 5
    possible bit-grid offsets); the correct phase is whichever yields
    flag+CRC-valid frames — no preamble state machine needed.
    """
    x = np.asarray(freq48, dtype=np.float64)
    if len(x) < 60:
        return []
    x = x - np.median(x)
    s = np.convolve(x, _SMOOTH, mode="same")
    out, seen = [], set()
    for ph in range(AIS_FS // AIS_BITRATE):
        v = s[ph::AIS_FS // AIS_BITRATE]
        if len(v) < 40:
            continue
        lvl = (v > 0).astype(np.int8)
        data = np.zeros(len(lvl) - 1, dtype=np.int8)
        data[lvl[1:] == lvl[:-1]] = 1    # NRZI: 1 = level unchanged
        for p in hdlc_frames(data.tolist()):
            key = tuple(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out

# ---------- per-block channel DSP (block-continuous) ----------

def _sinc_taps(cutoff_hz, numtaps, fs=SDR_RATE):
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / fs * m) * np.hamming(numtaps)
    return (h / h.sum()).astype(np.float64)

# 61 taps at 240 kHz: -3 dB ~11 kHz, stopband by ~25 kHz — the other AIS
# channel (50 kHz away) and the dongle's DC spike (60 kHz, offset-tuned)
# are far outside; adjacent-channel 25 kHz marine voice would leak only
# at its edges. 14 kHz design cutoff keeps the whole GMSK spectrum.
_AIS_TAPS = _sinc_taps(14000, 61)

def new_channel_state():
    """Fresh per-channel DSP state (one per AIS channel, per dongle)."""
    return {
        "rot": 0,          # offset rotator sample counter (dsp.frequency_shift)
        "fir_tail": np.zeros(len(_AIS_TAPS) - 1, dtype=np.complex64),
        "last_c": None,    # discriminator: previous block's last sample
        "dec_pos": 0,      # 48 kHz decimation phase across blocks
        "floor": None,     # noise floor EMA of |IQ| rms
        "burst": [],       # open burst: list of 48 kHz freq blocks
        "burst_open": False,
        "frames": 0,       # stats
        "bad": 0,
        "last_frame": 0.0,
    }

def demod_channel_block(c, st):
    """One offset-rotated IQ block -> (freq48, energy) for one AIS channel.

    Channel filter -> FM discriminator -> 48 kHz decimation, all with
    state carried across blocks (see dsp.fm_demodulate for the decimation
    phase argument).
    """
    # channel filter (tail carried across blocks)
    x = np.concatenate([st["fir_tail"], c])
    st["fir_tail"] = x[-(len(_AIS_TAPS) - 1):].copy()
    c = np.convolve(x, _AIS_TAPS, mode="valid")
    energy = float(np.sqrt(np.mean(np.abs(c) ** 2)))
    # discriminator: instant frequency in Hz (carried last sample)
    prev = st["last_c"]
    if prev is None:
        prev = c[0]
    dd = np.empty(len(c), dtype=np.complex64)
    dd[0] = c[0] * np.conj(prev)
    np.multiply(c[1:], np.conj(c[:-1]), out=dd[1:])
    st["last_c"] = c[-1].copy()
    freq = np.arctan2(dd.imag, dd.real) * (SDR_RATE / (2 * np.pi))
    # decimate to 48 kHz with continuous phase (mirrors dsp.fm_demodulate)
    start = (-st["dec_pos"]) % AIS_DEC
    st["dec_pos"] = (st["dec_pos"] + len(freq)) % AIS_DEC
    return freq[start::AIS_DEC], energy

def collect_bursts(freq48, energy, st, max_blocks=48):
    """Noise-floor tracking + burst assembly: append a channel block to
    the open burst while it is active; when it goes quiet again the
    closed burst (48 kHz freq samples) is returned for decoding.

    AIS slots are 26.7 ms with a >=3 ms quiet guard between them, so a
    threshold on the block energy (2.13 ms resolution) separates bursts.
    """
    if st["floor"] is None:
        st["floor"] = energy
    active = energy > 1.6 * st["floor"]
    if not active:   # EMA tracks the quiet floor, never the bursts themselves
        st["floor"] += 0.05 * (energy - st["floor"])
        active = energy > 1.6 * st["floor"]
    closed = []
    if active:
        st["burst"].append(freq48)
        st["burst_open"] = True
        if len(st["burst"]) > max_blocks:   # noise stuck high: cut the losses
            closed.append(np.concatenate(st["burst"]))
            st["burst"] = []
    elif st["burst_open"]:
        # one quiet block ends the burst (guard 3 ms > one 2.13 ms block)
        if len(st["burst"]) >= 2:
            closed.append(np.concatenate(st["burst"]))
        st["burst"] = []
        st["burst_open"] = False
    return closed

# ---------- ship table ----------

def handle_frames(payloads, channel, ch_st):
    """Decode payload vectors, merge them into state.ais_ships, append
    each frame to the persistent message log and the raw NMEA ring."""
    now = time.time()
    for p in payloads:
        d = parse_payload(p)
        if d is None:
            continue
        mmsi = d["mmsi"]
        with state.ais_lock:
            ship = state.ais_ships.setdefault(mmsi, {
                "mmsi": mmsi, "first_seen": now, "msgs": 0})
            ship.update({k: v for k, v in d.items()
                         if v is not None and k not in ("msg",)})
            ship["msgs"] += 1
            ship["last_seen"] = now
            ship["last_channel"] = channel
            seq = ship["msgs"] % 10
        ch_st["frames"] += 1
        ch_st["last_frame"] = now
        sentences = payload_to_aivdm(p, channel, seq)
        with state.ais_lock:
            for sentence in sentences:
                state.ais_nmea.append(sentence)
        log_message(d, channel, sentences)

# ---------- persistent message log (JSONL, one line per frame) ----------

_log_lock = threading.Lock()
_log_appends = 0

def log_message(d, channel, sentences):
    """Append one decoded frame to AIS_LOG_FILE (jsonl, newest last).

    Survives restarts; bounded: when it outgrows 4 MB the oldest half is
    dropped (checked every 256 appends — cheap at AIS message rates).
    Log write failures (disk full, permissions) warn but never take the
    capture thread down.
    """
    global _log_appends
    entry = {
        "ts": round(time.time(), 2),
        "time": datetime.now().strftime("%H:%M:%S"),
        "ch": channel,
        "msg": d["msg"],
        "mmsi": d["mmsi"],
        "cls": d.get("cls"),
        "name": d.get("name"),
        "callsign": d.get("callsign"),
        "lat": d.get("lat"),
        "lon": d.get("lon"),
        "sog": d.get("sog"),
        "cog": d.get("cog"),
        "hdg": d.get("heading"),
        "status": d.get("status"),
        "nmea": " ".join(sentences),
    }
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(AIS_LOG_FILE), exist_ok=True)
            with open(AIS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            _log_appends += 1
            if _log_appends % 256 == 0 and os.path.getsize(AIS_LOG_FILE) > 4 << 20:
                with open(AIS_LOG_FILE, encoding="utf-8") as f:
                    lines = f.readlines()
                with open(AIS_LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
        except (OSError, ValueError) as e:
            state.log_console(f"AIS log write failed: {e}", "warn")

def ais_log(count=200):
    """Newest-first tail of the persistent message log for /aislog.json."""
    try:
        count = max(1, min(int(count), 2000))
    except ValueError:
        count = 200
    with _log_lock:
        try:
            with open(AIS_LOG_FILE, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
    out = []
    for line in lines[-count:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()   # newest first
    return out

def prune_ships():
    """Drop ships not heard for AIS_SHIP_TTL_SECS (called on read paths)."""
    now = time.time()
    with state.ais_lock:
        for mmsi in [m for m, s in state.ais_ships.items()
                     if now - s["last_seen"] > AIS_SHIP_TTL_SECS]:
            del state.ais_ships[mmsi]

def ais_status():
    """Snapshot for /ais.json: ship table, stats, recent raw sentences."""
    prune_ships()
    with state.ais_lock:
        ships = sorted(state.ais_ships.values(),
                       key=lambda s: -s["last_seen"])
        nmea = list(state.ais_nmea)
        stats = {k: dict(v) for k, v in state.ais_channels.items()}
        enabled = state.ais_enabled
        serial = state.ais_serial
    return {"enabled": enabled, "dongle": serial, "channels": stats,
            "ships": ships, "nmea": nmea}

# ---------- capture thread ----------

# Per-dongle AIS channels: shift each AIS channel (±25 kHz around
# AIS_CENTER_HZ after the +SDR_OFFSET_HZ rotation) onto 0 Hz.
def _ais_shifts():
    """Rotations that move each AIS channel onto 0 Hz: the dongle is
    tuned SDR_OFFSET_HZ above AIS_CENTER_HZ, so channel f arrives at
    -(SDR_OFFSET_HZ + AIS_CENTER_HZ - f); frequency_shift(c, x) centers
    a signal at -x."""
    return [int(SDR_OFFSET_HZ + (AIS_CENTER_HZ - f)) for f in AIS_CHANNEL_HZ]

def ais_capture_thread(serial):
    """rtl_tcp lifecycle + per-block demod for the dedicated AIS dongle.

    Mirrors radio.sdr_capture_thread's process management (port check,
    handshake, gain via the control protocol, liveness check, backoff)
    but tunes to AIS_CENTER_HZ and demodulates both AIS channels instead
    of tracking satellites. Waterfall/signal update the same state.sdrs
    entry, so the dongle card shows the AIS spectrum like any other.
    """
    import socket
    from .dsp import iq_to_complex, frequency_shift
    from .radio import (_IQReader, _get_fft_window, _port_in_use, _recv_exact,
                        _rtl_tcp_set, RTL_TCP_SET_GAIN, RTL_TCP_SET_GAIN_MODE)
    from .config import FFT_SIZE, IQ_BLOCK

    entry = state.sdrs[serial]
    channel_names = ["A", "B"]
    shifts = _ais_shifts()
    st = [new_channel_state() for _ in shifts]
    rtl_log_path = os.path.join(LOGDIR, f"rtl_sdr_{serial}.log")
    os.makedirs(LOGDIR, exist_ok=True)
    rtl_log_f = open(rtl_log_path, "w")
    with state.ais_lock:
        state.ais_enabled = True
        state.ais_serial = serial
        state.ais_channels = {}
    state.log_console(f"📡 AIS receiver on dongle {serial}: "
                      f"{AIS_CHANNEL_HZ[0]/1e6:.3f} + {AIS_CHANNEL_HZ[1]/1e6:.3f} MHz")
    restart_backoff = 5.0
    block_count = 0
    while True:
        run_started = time.time()
        proc = None
        sock = None
        try:
            correction = tuning_correction(AIS_CENTER_HZ, serial)
            tune = AIS_CENTER_HZ + SDR_OFFSET_HZ + correction
            # shown in the dongle card's tuning infobox, like the satellite
            # capture threads (the ppm fallback applies ~+13 kHz here)
            entry["correction"], entry["correction_src"] = correction_info(AIS_CENTER_HZ, serial)
            if _port_in_use(entry["port"]):
                raise RuntimeError(f'port {entry["port"]} in use (stale rtl_tcp?) — not starting AIS rtl_tcp')
            rtl_log_f.seek(0)
            rtl_log_f.truncate()
            proc = subprocess.Popen(
                ["rtl_tcp", "-a", "127.0.0.1", "-p", str(entry["port"]), "-d", serial,
                 "-f", str(tune), "-s", str(SDR_RATE), "-g", str(SDR_GAIN)],
                stdout=subprocess.DEVNULL, stderr=rtl_log_f)
            entry["proc"] = proc
            entry["last_data"] = time.time()
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    sock = socket.create_connection(("127.0.0.1", entry["port"]), timeout=2)
                    break
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
            if sock is None:
                raise RuntimeError(f"rtl_tcp did not open port {entry['port']}")
            sock.settimeout(30)
            header = _recv_exact(sock, 12)
            if header is None or header[:4] != b"RTL0":
                raise RuntimeError("bad rtl_tcp handshake")
            if proc.poll() is not None:
                raise RuntimeError("rtl_tcp exited immediately (port conflict? device busy?)")
            gain_db = SDR_DONGLE_GAIN.get(serial, SDR_GAIN)
            if gain_db:
                _rtl_tcp_set(sock, RTL_TCP_SET_GAIN_MODE, 1)
                _rtl_tcp_set(sock, RTL_TCP_SET_GAIN, int(round(gain_db * 10)))
            state.log_console(f"rtl_tcp started for AIS (pid {proc.pid}, dongle {serial}), tuned {tune}Hz "
                               f"(offset +{SDR_OFFSET_HZ + correction}Hz, correction {correction:+d}Hz)")
            last_proc_check = time.time()
            reader = _IQReader(sock, IQ_BLOCK)
            while True:
                raw = reader.read_block()
                if raw is None:
                    state.log_console(f"rtl_tcp stream ended (AIS dongle {serial}), restarting...", "warn")
                    break
                entry["last_data"] = time.time()
                now_ts = time.time()
                if now_ts - last_proc_check >= 1.0:
                    last_proc_check = now_ts
                    if proc.poll() is not None:
                        raise RuntimeError(f"rtl_tcp (pid {proc.pid}) exited but port still streams")
                c = iq_to_complex(raw)
                # Waterfall + signal for the dongle card (raw band)
                block_count += 1
                if block_count % 8 == 0 and len(c) >= FFT_SIZE:
                    try:
                        window = _get_fft_window()
                        magnitude = np.abs(np.fft.fftshift(np.fft.fft(c[:FFT_SIZE] * window)))
                        center = len(magnitude) // 2
                        with entry["lock"]:
                            entry["signal"] = float(
                                magnitude[center - 21:center + 21].mean())
                            peak = magnitude.max()
                            if peak > 0:
                                magnitude *= 255.0 / peak
                            entry["waterfall"].append(magnitude.astype(int).tolist())
                    except Exception:
                        pass
                # Demodulate both AIS channels from the same block
                for ch_idx, (shift, ch_st) in enumerate(zip(shifts, st)):
                    cc = frequency_shift(c, shift, SDR_RATE, ch_st)
                    freq48, energy = demod_channel_block(cc, ch_st)
                    for burst in collect_bursts(freq48, energy, ch_st):
                        payloads = decode_burst(burst)
                        if payloads:
                            handle_frames(payloads, channel_names[ch_idx], ch_st)
                        elif len(burst) > 100:
                            ch_st["bad"] += 1
                    # expose per-channel stats for /ais.json
                    with state.ais_lock:
                        state.ais_channels[channel_names[ch_idx]] = {
                            "frames": ch_st["frames"], "bad": ch_st["bad"],
                            "last_frame": ch_st["last_frame"],
                            "floor": round(ch_st["floor"] or 0.0, 2),
                        }
        except Exception as e:
            state.log_console(f"AIS thread error (dongle {serial}): {e}", "error")
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 60.0)
        time.sleep(restart_backoff)

# ---------- test/simulation helpers (used by tests/test_ais.py) ----------

def gmsk_modulate(data_bits, fs=SDR_RATE, amp=0.6, bt=0.4):
    """GMSK-modulate NRZI data bits (test signal generator; ITU allows
    BT<=0.4 on 25 kHz channels — 0.3 closes the eye at 00 pairs beyond
    what frequency slicing can read, same as any slicing demodulator)."""
    T = 1.0 / AIS_BITRATE
    level, levels = 0, []
    for b in data_bits:
        if not b:
            level ^= 1
        levels.append(level)
    up = np.repeat(np.array(levels, dtype=np.float64) * 2 - 1, fs // AIS_BITRATE)
    # gaussian premodulation pulse (gauss ⊛ one-bit rect), centered
    span = 4
    t = np.arange(-span * fs // AIS_BITRATE, span * fs // AIS_BITRATE + 1) / fs
    b = 2 * np.pi * bt / (T * np.sqrt(2 * np.log(2)))
    g = np.exp(-0.5 * (b * t) ** 2)
    g /= g.sum()
    h = np.convolve(g, np.full(fs // AIS_BITRATE, AIS_BITRATE / fs))
    fm = np.convolve(up, h, mode="same") * (AIS_BITRATE / 4.0)
    phase = 2 * np.pi * np.cumsum(fm) / fs
    return (amp * np.exp(1j * phase)).astype(np.complex64)

def build_frame_bits(p):
    """AIS payload vector -> NRZI data bits of a complete AIS burst
    (preamble, flag, stuffed payload+FCS, flag)."""
    t = payload_to_transmitted(p)
    frame = stuff_bits(t + fcs_bits(t))
    preamble = [0, 1] * 12
    return preamble + list(FLAG) + frame + list(FLAG)
