"""Application entry point: background threads + HTTP server."""
import os
import socketserver
import threading

from .. import db
from .. import state
from ..config import LAT, LON, LOGDIR, PASS_MIN_ALT, PORT, RECORD_DIR

from .handler import NOAAHandler
from ..tracking.plan import prime_transmitters
from ..sdr.radio import sdr_thread
from ..tracking.scheduler import scheduler_thread


def main():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True

    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    # One-time import of the legacy JSON stores into station.db,
    # then load the AIS ship registry from it. ORDER MATTERS: the
    # registry lives in memory and save_ships() mirrors memory back
    # into the database - loading it before the import would mirror
    # an EMPTY table over the just-imported ships and delete them
    # (observed live 2026-10-10: 9 imported ships reduced to 1).
    db.import_legacy(None)
    from ..decoding import ais as _ais
    _ais.load_ships()
    # Startup summary of the persistent Orbcomm packet log (count only —
    # the log itself stays in the database, queried via /orbcommlog.json)
    from ..decoding import orbcomm as _orbcomm
    _orbcomm.load_log_summary()
    # Startup summary of the persistent APRS frame log (count only —
    # the log itself stays in the database, queried via /aprslog.json)
    from ..decoding import aprs as _aprs
    _aprs.load_log_summary()

    # Start scheduler thread (TLE refresh + pass prediction + frequency switching)
    threading.Thread(target=scheduler_thread, daemon=True).start()

    # Start SDR thread
    threading.Thread(target=sdr_thread, daemon=True).start()

    # Background: fetch SatNOGS transmitter metadata for the pass-list
    # receive plans (never on the request path — see plan.py)
    prime_transmitters()

    state.log_console(f"CrabArk receiver started (Regensburg {LAT}N {LON}E)")
    state.log_console(f"Auto pass tracking enabled, recording only during passes (>{PASS_MIN_ALT}°)")
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), NOAAHandler) as httpd:
        state.log_console(f"Server running on port {PORT}")
        httpd.serve_forever()
