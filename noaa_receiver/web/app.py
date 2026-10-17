"""Application entry point: background threads + HTTP server."""
import os
import socketserver
import threading

from .. import db
from .. import state
from ..config import (LAT, LON, LOGDIR, PASS_MIN_ALT, PORT, RECORD_DIR,
                     RECORD_LOCAL_DIR)

from ..archive import archive_thread
from .handler import NOAAHandler
from ..tracking.plan import prime_transmitters
from ..sdr.radio import sdr_thread
from ..tracking.scheduler import scheduler_thread


def main():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True

    os.makedirs(LOGDIR, exist_ok=True)
    # Capture dir first (always the LOCAL disk — a network share must
    # never sit under a running IQ stream), then the archive dir.
    os.makedirs(RECORD_LOCAL_DIR, exist_ok=True)
    if os.path.realpath(RECORD_DIR) != os.path.realpath(RECORD_LOCAL_DIR):
        os.makedirs(RECORD_DIR, exist_ok=True)
    # If the recording ARCHIVE was pointed at a network share
    # (PRAWN_RECORD_DIR) but no share is mounted, the path silently sits
    # on the local disk — the exact way the VM root disk once filled
    # overnight. Captures land on the local disk either way now; the
    # warn says the archive will not drain anywhere else. ismount() is
    # not enough (RECORD_DIR is usually a subdirectory of the real
    # mount point): compare filesystem devices instead.
    if os.environ.get("PRAWN_RECORD_DIR") and \
            os.stat(RECORD_DIR).st_dev == os.stat("/").st_dev:
        state.log_console("⚠️ RECORD_DIR is set via PRAWN_RECORD_DIR but "
                          "sits on the LOCAL disk (no network share "
                          "mounted) — recordings stay local and may "
                          "fill it!", "warn")
    # One-time import of the legacy JSON stores into station.db,
    # then load the AIS ship registry from it. ORDER MATTERS: the
    # registry lives in memory and save_ships() mirrors memory back
    # into the database - loading it before the import would mirror
    # an EMPTY table over the just-imported ships and delete them
    # (observed live 2026-10-10: 9 imported ships reduced to 1).
    db.import_legacy(None)
    from ..decoding import ais as _ais
    _ais.load_ships()
    # Every ADS-B aircraft ever received, restored from the
    # adsb_aircrafts table (queried via /adsb_aircrafts.json and the
    # WS 'adsb_aircrafts' topic; the page shows the last 24 h)
    from ..decoding import adsb as _adsb
    _adsb.load_aircrafts()
    # Startup summary of the persistent Orbcomm packet log (count only —
    # the log itself stays in the database, queried via /orbcommlog.json)
    from ..decoding import orbcomm as _orbcomm
    _orbcomm.load_log_summary()
    # Startup summary of the persistent APRS frame log (count only —
    # the log itself stays in the database, queried via /aprslog.json)
    from ..decoding import aprs as _aprs
    _aprs.load_log_summary()
    # Every APRS station ever received (queried via /aprs_stations.json
    # and the WS 'aprs_stations' topic)
    _aprs.load_stations()
    # Startup summary of the persistent POCSAG page log (count only)
    # plus the newest pages into the live ring
    from ..decoding import pocsag as _pocsag
    _pocsag.load_pages()

    # Start scheduler thread (TLE refresh + pass prediction + frequency switching)
    threading.Thread(target=scheduler_thread, daemon=True).start()

    # Start SDR thread
    threading.Thread(target=sdr_thread, daemon=True).start()

    # Recording archiver: drains the local capture dir into the
    # archive share once recordings are decoded and the share is up
    # (no-op on single-directory setups — see archive.py)
    threading.Thread(target=archive_thread, daemon=True).start()

    # Background: fetch SatNOGS transmitter metadata for the pass-list
    # receive plans (never on the request path — see plan.py)
    prime_transmitters()

    state.log_console(f"CrabArk receiver started (Regensburg {LAT}N {LON}E)")
    state.log_console(f"Auto pass tracking enabled, recording only during passes (>{PASS_MIN_ALT}°)")
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), NOAAHandler) as httpd:
        state.log_console(f"Server running on port {PORT}")
        httpd.serve_forever()
