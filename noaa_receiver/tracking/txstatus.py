"""Home-grown transmitter activity ledger + frequency fallback.

Two things every operator wants to know BEFORE a pass, neither of which
the pass prediction can answer on its own:

1. Is the transmitter we are about to listen to actually ON? SatNOGS
   keeps a per-transmitter "alive" flag (plan.transmitter_status, shown
   as the red tx-dead chip), but it is manually curated and often
   stale. The station's OWN decode history is the ground truth: a pass
   that recorded and decoded but produced no image means the transmitter
   was dark THAT pass. ledger() rolls that history into per-satellite
   "last heard" and "dark streak" numbers.

2. If SatNOGS says the tuned frequency's transmitter is dead, does the
   SAME satellite have another alive transmitter we can receive?
   effective_freq() answers that: it keeps the configured frequency
   unless SatNOGS flags it dead AND an alternate transmitter exists that
   is (a) alive, (b) inside the receivable band (VHF/UHF — no L-band
   without a dish) and (c) mode-compatible with the receive plan the
   station already runs for this satellite (LRPT stays LRPT, SSTV stays
   SSTV). The scheduler and the pass list both ask it, so the dashboard
   shows the frequency that will actually be tuned.

note_pass_result() runs once per finished pass (scheduler) and raises
the state-change console alerts: a satellite dark for ALERT_DARK_STREAK
passes gets one warning (once, not per pass), and a dark satellite
coming back is celebrated.
"""
import threading
import time

from .. import db
from .. import state
from ..config import IQ_RECORD_FREQS, TRACKED_SATS
from . import plan

# A satellite dark this many passes in a row gets a console warning
# (raised once at the threshold, not on every pass after it)
ALERT_DARK_STREAK = 3

# Receivable band for the fallback: VHF/UHF the current antennas cover.
# L-band HRPT (1.7 GHz) etc. are inside the dongle's tuning range but
# need a dish — no fallback there.
FALLBACK_MIN_HZ = 24_000_000
FALLBACK_MAX_HZ = 1_000_000_000

_ledger_lock = threading.Lock()
_ledger_cache = {}          # sat_name -> stats dict
_ledger_built_at = 0.0
_LEDGER_TTL = 60.0

# sat_name -> last alerted state ("dark"/"lit"/None = never alerted)
_alert_state = {}


def _build_ledger():
    """Roll the pass history into per-satellite activity stats.

    A pass is DARK when it recorded but produced no image (png is NULL)
    — the station's own measurement, current to the last pass. LIT means
    a decoded image exists for it."""
    global _ledger_built_at
    now = time.time()
    with _ledger_lock:
        if now - _ledger_built_at < _LEDGER_TTL and _ledger_cache:
            return _ledger_cache
        stats = {}
        try:
            rows = db.query(
                "SELECT sat_name, rise_ts, png FROM passes"
                " ORDER BY sat_name, rise_ts")
            for r in rows:
                name = r["sat_name"]
                s = stats.setdefault(name, {"dark_streak": 0,
                                            "passes": 0,
                                            "last_heard_ts": None})
                s["passes"] += 1
                if r["png"]:
                    s["dark_streak"] = 0
                    s["last_heard_ts"] = r["rise_ts"]
                else:
                    s["dark_streak"] += 1
        except Exception as e:
            state.log_console(f"TX ledger could not read pass history: {e}",
                              "warn")
            return _ledger_cache
        _ledger_cache.clear()
        _ledger_cache.update(stats)
        _ledger_built_at = now
        return _ledger_cache


def ledger(sat_name):
    """Activity stats for one satellite, or None when never tracked:
    {dark_streak, passes, last_heard_ts}. dark_streak counts the
    consecutive most-recent passes without a decoded image — the
    home-grown counterpart of the SatNOGS alive flag."""
    return _build_ledger().get(sat_name)


def cached_ledger(sat_name):
    """Non-rebuilding ledger read for the pass-list hot path (it is
    served under state.status_lock — no DB query there): the cached
    stats or None when stale/absent. The scheduler keeps the cache
    fresh: note_pass_result() invalidates AND rebuilds it after every
    logged pass, ledger() rebuilds on demand elsewhere."""
    return _ledger_cache.get(sat_name)


def invalidate():
    """Mark the ledger cache stale (called after a pass was logged, so
    the next ledger() rebuild reflects the new result). The stale
    entries stay readable for the dashboard hot path — one pass of
    staleness beats a blinking chip, and note_pass_result rebuilds
    within milliseconds anyway."""
    with _ledger_lock:
        global _ledger_built_at
        _ledger_built_at = 0.0


def _plan_mode_keyword(sat_name):
    """The mode keyword an alternate transmitter must carry to stay
    compatible with the receive plan this satellite already runs
    (LRPT for Meteor, SSTV for the ISS; None = no fallback)."""
    name = (sat_name or "").lower()
    if "meteor" in name:
        return "lrpt"
    if "iss" in name:
        return "sstv"
    return None


def effective_freq(catnr, sat_name, configured_hz):
    """The frequency the receiver actually tunes for this satellite:
    the configured one, unless SatNOGS flags its transmitter DEAD and
    the same satellite has another ALIVE transmitter that is receivable
    (VHF/UHF) and mode-compatible with the station's receive plan.
    Returns (freq_hz, note): note is None on the configured frequency,
    or a human explanation shown on the dashboard when the fallback
    fired. Reads only caches — safe under the status lock."""
    tx = plan.transmitter_status(catnr)
    if tx is None or tx.get("alive") is not False:
        return configured_hz, None
    keyword = _plan_mode_keyword(sat_name)
    if keyword is None:
        return configured_hz, None
    best = None
    for t in plan.all_transmitters(catnr) or []:
        if t.get("alive") is not True:
            continue
        dl = t.get("downlink_hz")
        if not dl or not (FALLBACK_MIN_HZ <= dl <= FALLBACK_MAX_HZ):
            continue
        mode = (t.get("mode") or "").lower()
        if keyword not in mode and keyword not in (t.get("description")
                                                   or "").lower():
            continue
        if abs(dl - configured_hz) < 25_000:
            continue          # that IS the dead one
        if best is None or abs(dl - configured_hz) < abs(best["downlink_hz"] - configured_hz):
            best = t
    if best is None:
        return configured_hz, None
    dl = best["downlink_hz"]
    # The LRPT decode chain needs the raw IQ of the tuned band
    # (radio.py keys the IQ recording on this set) — register the
    # fallback frequency so the capture writes baseband there too
    IQ_RECORD_FREQS.add(dl)
    note = (f"SatNOGS: {configured_hz / 1e6:.4f} MHz {tx.get('mode') or ''}"
            f" inactive \u2192 switched to {dl / 1e6:.4f} MHz"
            f" {best.get('mode') or ''} (active)")
    return dl, note


def note_pass_result(sat_name, decoded_png):
    """One finished pass was logged: update the ledger and raise the
    state-change console alerts. decoded_png is the pass's image file
    name (None = dark pass)."""
    invalidate()
    s = ledger(sat_name)
    if s is None:
        return
    dark = s["dark_streak"] >= ALERT_DARK_STREAK
    prev = _alert_state.get(sat_name)
    if dark and prev != "dark":
        state.log_console(
            f"\u26a0\ufe0f {sat_name}: transmitter dark for"
            f" {s['dark_streak']} passes in a row (last image"
            f" {_ago(s['last_heard_ts'])}) \u2014 tracking continues,"
            f" but the pass list will say so", "warn")
        _alert_state[sat_name] = "dark"
    elif not dark and decoded_png and prev == "dark":
        state.log_console(
            f"\U0001f4e1 {sat_name}: transmitter is BACK \u2014 image"
            f" decoded after a {ALERT_DARK_STREAK}+ pass dark streak")
        _alert_state[sat_name] = "lit"
    elif decoded_png and prev is None:
        _alert_state[sat_name] = "lit"


def _ago(ts):
    """Human 'last heard' age for the console line."""
    if not ts:
        return "never (no image decoded so far)"
    hrs = max(0.0, (time.time() - ts) / 3600.0)
    if hrs < 1:
        return f"{int(hrs * 60)} min ago"
    if hrs < 48:
        return f"{hrs:.0f} h ago"
    return f"{hrs / 24:.0f} days ago"


def tuned_freq(catnr, sat_name):
    """(freq_hz, note) for a tracked satellite — the configured
    frequency with the SatNOGS dead-transmitter fallback applied."""
    configured = TRACKED_SATS.get(catnr, (None, 0))[1]
    if not configured:
        return 0, None
    return effective_freq(catnr, sat_name, configured)
