"""Per-pass receive plan: what a pass is listened to with and how the
recording is decoded, derived from the static config plus the SatNOGS
transmitter status. Shown next to every upcoming pass on the dashboard so
the operator knows what to expect before a pass starts."""
from . import satnogs
from .config import RECORD_ISS, SAT_DSB_DEMOD_BW_HZ, SAT_DSB_FREQ


def receive_plan(catnr, sat_name):
    """(mode, plan, demod_bw_khz or None, iq_recording) for a pass."""
    name = (sat_name or '').lower()
    if catnr in SAT_DSB_FREQ:
        return ('DSB',
                'DSB instrument telemetry (APT transmitter off) — raw IQ recording, SatDump noaa_dsb',
                SAT_DSB_DEMOD_BW_HZ // 1000, True)
    if 'meteor' in name:
        return ('LRPT',
                'LRPT digital weather image — raw IQ recording, SatDump meteor_m2-x_lrpt',
                None, True)
    if 'iss' in name:
        if not RECORD_ISS:
            return ('SSTV',
                    'SSTV Robot 36 (only during ARISS events) — tracked, NOT recorded (RECORD_ISS off)',
                    None, False)
        return ('SSTV',
                'SSTV Robot 36 (only during ARISS events) — audio recording, sstv decoder',
                None, False)
    return ('APT', 'APT analog weather image — audio recording, noaa-apt PNG',
            None, False)


def transmitter_status(catnr, freq_hz):
    """The SatNOGS transmitter record matching the frequency we tune, or
    None. Cached server-side for a day; DB outages return None silently."""
    info, _err = satnogs.satellite_info(catnr)
    if not info:
        return None
    best = None
    for t in info.get('transmitters') or []:
        dl = t.get('downlink_hz')
        if dl and abs(dl - freq_hz) < 25_000:
            best = t   # keep the last match
    return best
