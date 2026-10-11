"""Per-pass receive plan: what a pass is listened to with and how the
recording is decoded, derived from the static config plus the SatNOGS
transmitter status. Shown next to every upcoming pass on the dashboard so
the operator knows what to expect before a pass starts.

The SatNOGS lookups happen ONLY in a background loader thread — the pass
list itself must never touch the network (it is served under
state.status_lock; a slow DB fetch there would freeze the whole API)."""
import threading
import time

from . import satnogs
from ..config import RECORD_ISS, TRACKED_SATS

# catnr -> matching transmitter dict (or None: fetched, no match / DB down).
# Absent = not fetched yet — the pass list omits the tx details until then.
_tx_cache = {}
_loader_started = False


def receive_plan(catnr, sat_name):
    """(mode, plan, demod_bw_khz or None, iq_recording) for a pass."""
    name = (sat_name or '').lower()
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
    return (None, 'no receive plan for this satellite', None, False)


def _tuned_freq(catnr):
    return TRACKED_SATS.get(catnr, (None, 0))[1]


def _match_transmitter(info, freq_hz):
    best = None
    for t in info.get('transmitters') or []:
        dl = t.get('downlink_hz')
        if dl and abs(dl - freq_hz) < 25_000:
            best = t   # keep the last match
    return best


def _loader():
    """Fetch SatNOGS metadata for every tracked satellite (serially —
    db.satnogs.org is rate-limited) and cache the transmitter matching
    each satellite's tuned frequency. Retries every 30 min until every
    satellite is loaded (the DB is unreachable for hours at a time)."""
    while True:
        missing = [c for c in TRACKED_SATS if c not in _tx_cache]
        if not missing:
            return
        for catnr in missing:
            info, _err = satnogs.satellite_info(catnr)
            if info:
                freq = _tuned_freq(catnr)
                _tx_cache[catnr] = _match_transmitter(info, freq)
            time.sleep(2)   # be gentle with the rate-limited DB
        if any(c not in _tx_cache for c in TRACKED_SATS):
            time.sleep(1800)


def prime_transmitters():
    """Start the background loader once (idempotent, returns instantly)."""
    global _loader_started
    if _loader_started:
        return
    _loader_started = True
    threading.Thread(target=_loader, daemon=True,
                     name='satnogs-tx-loader').start()


def transmitter_status(catnr):
    """Cached transmitter record for the satellite's tuned frequency, or
    None when not loaded yet / no match / DB unreachable. Never blocks."""
    return _tx_cache.get(catnr)
