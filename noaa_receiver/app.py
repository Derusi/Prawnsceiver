"""Application entry point: background threads + HTTP server."""
import os
import socketserver
import threading

from . import state
from .config import LAT, LON, LOGDIR, PASS_MIN_ALT, PORT, RECORD_DIR

from .handler import NOAAHandler
from .history import migrate_pass_history
from .plan import prime_transmitters
from .radio import sdr_thread
from .scheduler import scheduler_thread


def main():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True


    # Link pre-migration history entries to their recordings
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    migrate_pass_history()

    # Start scheduler thread (TLE refresh + pass prediction + frequency switching)
    threading.Thread(target=scheduler_thread, daemon=True).start()

    # Start SDR thread
    threading.Thread(target=sdr_thread, daemon=True).start()

    # Background: fetch SatNOGS transmitter metadata for the pass-list
    # receive plans (never on the request path — see plan.py)
    prime_transmitters()

    state.log_console(f"NOAA Receiver started (Regensburg {LAT}N {LON}E)")
    state.log_console(f"Auto pass tracking enabled, recording only during passes (>{PASS_MIN_ALT}°)")
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), NOAAHandler) as httpd:
        state.log_console(f"Server running on port {PORT}")
        httpd.serve_forever()
