"""Transmitter activity ledger + SatNOGS dead-transmitter frequency fallback.

Run: python -u tests/test_txstatus.py
"""
import os
import sys
import tempfile
import time

ROOT = tempfile.mkdtemp()
os.environ["PRAWN_DB_FILE"] = os.path.join(ROOT, "station.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from noaa_receiver import db, state
db.DB_FILE = os.environ["PRAWN_DB_FILE"]
db.close()

from noaa_receiver.tracking import plan, txstatus
import noaa_receiver.config as config

state.console_buffer.clear()

def _log_pass(sat, ts, png):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO passes (sat_name, rise_ts, rise_local, set_local,"
            " max_alt, duration_min, frequency_hz, signal_peak, quality,"
            " decoded, wav, png, satnogs, logged_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sat, ts, "", "", 30.0, 8.0, 137912500, 100.0, "medium",
             1 if png else 0, "x.wav" if png else None, png, None, ts))


def _console_texts():
    with state.console_lock:
        return [l["msg"] for l in state.console_buffer]


# --- 1. ledger: dark streak + last heard from the pass history ---
t0 = 1_700_000_000
_log_pass("Meteor-M 2-3", t0, "a.png")           # lit
_log_pass("Meteor-M 2-3", t0 + 3600, None)       # dark
_log_pass("Meteor-M 2-3", t0 + 7200, None)      # dark
_log_pass("Meteor-M 2-3", t0 + 10800, None)      # dark
_log_pass("Meteor-M 2-4", t0 + 5400, "b.png")    # lit
txstatus.invalidate()
led = txstatus.ledger("Meteor-M 2-3")
assert led["dark_streak"] == 3, led
assert led["passes"] == 4 and led["last_heard_ts"] == t0, led
led4 = txstatus.ledger("Meteor-M 2-4")
assert led4["dark_streak"] == 0 and led4["last_heard_ts"] == t0 + 5400, led4
assert txstatus.ledger("ISS (Zarya)") is None
# hot-path read: present while cached, no rebuild under the lock
assert txstatus.cached_ledger("Meteor-M 2-3")["dark_streak"] == 3
txstatus.invalidate()
# stale entries stay readable (no flicker); the rebuild brings the
# fresh numbers
assert txstatus.cached_ledger("Meteor-M 2-3")["dark_streak"] == 3
txstatus.ledger("Meteor-M 2-3")
print("1. ledger (dark streak, last heard, cached hot path): ok")

# --- 2. note_pass_result: dark warning raised ONCE at the threshold ---
txstatus.invalidate()
before = len(_console_texts())
txstatus.note_pass_result("Meteor-M 2-3", None)     # streak 3 -> warning
warn = _console_texts()[-1] if _console_texts() else ""
assert "transmitter dark for 3 passes" in warn, _console_texts()[-3:]
n_after_first = len(_console_texts())
txstatus.note_pass_result("Meteor-M 2-3", None)       # still dark: silent
assert len(_console_texts()) == n_after_first, "warning repeated"
print("2. dark-transmitter warning raised once, not per pass: ok")

# --- 3. note_pass_result: transmitter is BACK ---
_log_pass("Meteor-M 2-3", t0 + 14400, "c.png")       # lit again
txstatus.note_pass_result("Meteor-M 2-3", "c.png")
back = _console_texts()[-1]
assert "BACK" in back, back
assert txstatus.ledger("Meteor-M 2-3")["dark_streak"] == 0
print("3. transmitter-is-BACK alert + streak reset: ok")

# --- 4. effective_freq: dead SatNOGS tx + compatible alive alternative ---
CAT = 57166
plan._tx_cache[CAT] = {"description": "LRPT Downlink", "mode": "LRPT",
                       "alive": False, "downlink_hz": 137912500}
plan._tx_all[CAT] = [
    {"description": "LRPT Downlink", "mode": "LRPT", "alive": False,
     "downlink_hz": 137912500},
    # alive but L-band: receivable band excludes it (no dish)
    {"description": "HRPT Downlink", "mode": "BPSK", "alive": True,
     "downlink_hz": 1_700_000_000},
    # alive but wrong mode for the Meteor LRPT receive plan
    {"description": "Telemetry", "mode": "GMSK", "alive": True,
     "downlink_hz": 137_100_000},
    # the compatible alternate: alive, VHF, LRPT
    {"description": "LRPT Downlink 2", "mode": "LRPT", "alive": True,
     "downlink_hz": 137_100_000},
]
freq, note = txstatus.effective_freq(CAT, "Meteor-M 2-3", 137912500)
assert freq == 137_100_000, (freq, note)
assert note and "inactive" in note and "137.1000" in note, note
assert 137_100_000 in config.IQ_RECORD_FREQS, "fallback freq must record IQ"
print("4. frequency fallback (dead LRPT -> alive LRPT, IQ registered): ok")

# --- 5. effective_freq: no compatible alternative -> keep configured ---
plan._tx_all[CAT] = [plan._tx_all[CAT][0],   # the dead one
                     plan._tx_all[CAT][1],   # L-band HRPT only
                     plan._tx_all[CAT][2]]   # GMSK telemetry
freq, note = txstatus.effective_freq(CAT, "Meteor-M 2-3", 137912500)
assert freq == 137912500 and note is None, (freq, note)
# unknown/alive SatNOGS status: never switch
plan._tx_cache[CAT] = {"alive": True, "mode": "LRPT",
                       "downlink_hz": 137912500}
assert txstatus.effective_freq(CAT, "Meteor-M 2-3", 137912500)[1] is None
plan._tx_cache[CAT] = {"alive": None, "mode": "LRPT",
                       "downlink_hz": 137912500}
assert txstatus.effective_freq(CAT, "Meteor-M 2-3", 137912500)[1] is None
plan._tx_cache.pop(CAT, None)
assert txstatus.effective_freq(CAT, "Meteor-M 2-3", 137912500)[1] is None
print("5. no fallback without a compatible ALIVE alternative: ok")

# --- 6. ISS keyword: only SSTV-compatible alternates for the ISS ---
CAT_ISS = 25544
plan._tx_cache[CAT_ISS] = {"description": "SSTV", "mode": "SSTV",
                           "alive": False, "downlink_hz": 437550000}
plan._tx_all[CAT_ISS] = [
    {"description": "SSTV", "mode": "SSTV", "alive": False,
     "downlink_hz": 437550000},
    {"description": "APRS digipeater", "mode": "AFSK", "alive": True,
     "downlink_hz": 145825000},          # alive but not SSTV
    {"description": "SSTV backup", "mode": "SSTV/PD120", "alive": True,
     "downlink_hz": 437800000},
]
freq, note = txstatus.effective_freq(CAT_ISS, "ISS (Zarya)", 437550000)
assert freq == 437800000, (freq, note)
assert note and "SSTV" in note, note
print("6. ISS fallback stays mode-compatible (SSTV only): ok")

# --- 7. tuned_freq: TRACKED_SATS lookup with the fallback applied ---
plan._tx_cache[57166] = {"alive": False, "mode": "LRPT",
                         "downlink_hz": 137912500}
plan._tx_all[57166] = [
    {"mode": "LRPT", "alive": False, "downlink_hz": 137912500},
    {"mode": "LRPT", "alive": True, "downlink_hz": 137100000},
]
f, n = txstatus.tuned_freq(57166, "Meteor-M 2-3")
assert f == 137100000 and n, (f, n)
assert txstatus.tuned_freq(99999, "Unknown") == (0, None)
print("7. tuned_freq (config freq + fallback, unknown sat): ok")

print("ALL TXSTATUS TESTS PASSED")
import os as _os
_os._exit(0)
