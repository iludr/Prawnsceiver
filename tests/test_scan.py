"""Tests for noaa_receiver.scan: peak/floor measurement, width fit, scan sweep.

Run: python3 tests/test_scan.py (needs numpy). Asserts:

- _measure finds the true peak bin and ratio, ignoring the DC-spike
  window at +SDR_OFFSET_HZ (a spike there must never win the peak search)
- _signal_width_hz measures a synthetic hump's occupied width and
  refuses to measure noise
- fit_bw_hz maps width -> demod cutoff (APT-like signal -> ~22 kHz) and
  clamps to the 1-120 kHz range
- the scan thread end-to-end against a fake dongle entry whose feeder
  is TUNE-AWARE (signals sit at absolute frequencies and land at 0 Hz
  once the scan re-centers on them; spurs follow the tune):
  nothing found (override restored), found (parked on the re-centered
  peak, bandwidth fitted), stop request (parked at the current
  frequency), saturating impulse noise ignored, carrier found through
  impulse noise, and a tune-relative spur (the live phantom-finder
  bug class) rejected by the re-center confirm
"""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.sdr import scan
from noaa_receiver import state
from noaa_receiver.config import FFT_SIZE, SDR_OFFSET_HZ, SDR_RATE

BIN_HZ = SDR_RATE / FFT_SIZE

# --- 1. _measure: peak bin, ratio, DC-spike masking ---
def noise_row(seed):
    rng = np.random.RandomState(seed)
    return rng.uniform(90.0, 110.0, FFT_SIZE).astype(np.float32)

def signal_row(seed, offset_hz, amp=500.0, width_hz=18750.0):
    m = noise_row(seed).astype(np.float64)
    idx = np.arange(FFT_SIZE)
    c = FFT_SIZE // 2 + offset_hz / BIN_HZ
    m += amp * np.exp(-0.5 * ((idx - c) / (width_hz / BIN_HZ / 2.35)) ** 2)
    return m.astype(np.float32)

row = signal_row(1, -50000)
idx, ratio, floor = scan._measure(row)
assert abs((idx - FFT_SIZE // 2) * BIN_HZ - (-50000)) < 4 * BIN_HZ, "peak bin offset wrong"
assert 4.0 < ratio < 6.5, f"peak/floor ratio off: {ratio}"
assert 90 < floor < 110, "floor should track the noise median"

# A 4x-floor spike INSIDE the +SDR_OFFSET_HZ window must not win:
spiked = signal_row(2, -50000)
c = int(round(FFT_SIZE // 2 + SDR_OFFSET_HZ / BIN_HZ))
spiked[c] = 450.0
idx2, ratio2, _ = scan._measure(spiked)
assert abs((idx2 - FFT_SIZE // 2) * BIN_HZ - (-50000)) < 4 * BIN_HZ, \
    "DC spike inside the guard window won the peak search"
assert ratio2 < 6.5, "spike contaminated the ratio"

# Pure noise: ratio stays near 1
_, rnoise, _ = scan._measure(noise_row(3))
assert rnoise < 1.3, f"noise measured as a signal: {rnoise}"
print("1. _measure peak/floor + DC-spike masking: ok")

# --- 2. _signal_width_hz ---
w = scan._signal_width_hz(signal_row(4, -50000))
assert 15000 < w < 40000, f"hump width off: {w} Hz"
assert scan._signal_width_hz(noise_row(5)) == 0.0, "noise must measure zero width"
print(f"2. _signal_width_hz: hump {w/1000:.1f} kHz, noise refused: ok")

# --- 3. fit_bw_hz ---
apt = scan.fit_bw_hz(34000)          # APT-like: ~34 kHz occupied
assert 21000 <= apt <= 24000, apt     # ... maps to the ~22 kHz demod default
assert scan.fit_bw_hz(200) == 1000    # clamp at the bottom
assert scan.fit_bw_hz(400000) == 120000  # clamp at the top (full band)
print(f"3. fit_bw_hz: 34 kHz signal -> {apt/1000:g} kHz cutoff, clamps hold: ok")

# --- 4. scan thread end-to-end (fake dongle entry + tune-aware feeder) ---
# Speed the dwell windows up: wrap _sample with a short-but-sufficient
# dwell (>= 5 rows are averaged per window by design — the feeder
# publishes a fresh row every 4 ms, so 0.15 s gathers ~30 rows)
_orig_sample = scan._sample
def _fast_sample(entry, sc, settle=None, dwell=None):
    return _orig_sample(entry, sc, settle=0.0, dwell=0.15)
scan._sample = _fast_sample
scan.SCAN_SAMPLE_EVERY = 0.002

SERIAL = "SCANTEST"
saved = (dict(state.manual_dongle_freq), dict(state.manual_dongle_bw), dict(state.scans))

def make_entry():
    return {"lock": threading.Lock(), "last_mag": None, "primary": False}

def feed(entry, rowfn, every=0.004):
    """Publish fresh magnitude rows until stopped. rowfn(tune_hz, k)
    builds the row, so the fake spectrum can depend on where the dongle
    is tuned: signals sit at ABSOLUTE frequencies (they land at 0 Hz
    once the scan re-centers on them), spurs follow the tune."""
    stop = threading.Event()
    def run():
        k = 0
        while not stop.is_set():
            with state.status_lock:
                tune = state.manual_dongle_freq.get(SERIAL)
            with entry["lock"]:
                entry["last_mag"] = rowfn(tune, k)
            k += 1
            time.sleep(every)
    threading.Thread(target=run, daemon=True).start()
    return stop

def wait_done(sc, timeout=15):
    deadline = time.time() + timeout
    while sc.get("active") and time.time() < deadline:
        time.sleep(0.02)
    assert not sc.get("active"), "scan thread did not finish"

def add_hump(m, offset_hz, amp=500.0, width_hz=18750.0):
    idx = np.arange(FFT_SIZE)
    c = FFT_SIZE // 2 + offset_hz / BIN_HZ
    m += amp * np.exp(-0.5 * ((idx - c) / (width_hz / BIN_HZ / 2.35)) ** 2)
    return m

# The fake carrier's absolute frequency (below the 100 MHz scan start)
SIG_ABS = 99_950_000

def carrier_rowfn(impulses=False):
    def rowfn(tune, k):
        m = noise_row(20 + (k % 2)).astype(np.float64)
        off = (SIG_ABS - tune) if tune is not None else -50000.0
        m = add_hump(m, off)
        if impulses:
            m[int(np.random.RandomState(k).randint(0, FFT_SIZE))] = 700.0
        return m.astype(np.float32)
    return rowfn

# 4a. nothing found: noise only -> override cleared, result 'nothing'
state.sdrs[SERIAL] = entry = make_entry()
stop = feed(entry, lambda tune, k: noise_row(10 + (k % 2)))
sc = scan.start_scan(SERIAL, 100.0, 100.6, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "nothing", sc["result"]
assert SERIAL not in state.manual_dongle_freq, "override must be cleared when nothing is found"
snap = scan.scan_snapshot(SERIAL)
assert snap["result"] == "nothing" and snap["found_mhz"] is None
print("4a. noise sweep -> 'nothing', override restored: ok")

# 4b. found: a carrier at 99.95 MHz shows up at -50 kHz on the first
# step (tuned 100.0) -> the scan re-centers on it, confirms (the peak
# lands at ~0 Hz there), parks and fits the recorded BW
state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
stop = feed(entry, carrier_rowfn())
sc = scan.start_scan(SERIAL, 100.0, 100.6, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "found", sc["result"]
assert abs(sc["found_hz"] - SIG_ABS) <= 2000, sc["found_hz"]   # re-centered on the peak (bin accuracy)
assert abs(state.manual_dongle_freq[SERIAL] - SIG_ABS) <= 2000, "dongle not parked on the find"
assert sc["found_ratio"] >= 3.0
bw = state.manual_dongle_bw.get(SERIAL)
# one BIN_HZ of slack: _signal_width_hz counts whole bins, so the fitted
# BW quantizes a bin high when the bins get coarse (2.4 MHz capture)
assert bw and 14000 <= bw <= 24000 + BIN_HZ, f"fitted bandwidth off: {bw}"
snap = scan.scan_snapshot(SERIAL)
assert abs(snap["found_mhz"] - 99.95) < 0.005 and snap["bw_khz"] == round(bw / 1000, 1)
assert snap["active"] is False
print(f"4b. found + re-centered + parked at 99.95 MHz, BW fitted to {bw/1000:g} kHz: ok")

# 4c. stop request: parks at the current frequency
state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
state.manual_dongle_bw.clear()
stop = feed(entry, lambda tune, k: noise_row(30 + (k % 2)))
sc = scan.start_scan(SERIAL, 100.0, 102.0, 100)
time.sleep(0.15)   # let it make a few steps
assert scan.stop_scan(SERIAL) is True
wait_done(sc)
stop.set()
assert sc["result"] == "stopped", sc["result"]
assert state.manual_dongle_freq.get(SERIAL) == sc["cur_hz"], "must stay parked after Stop"
print("4c. stop request -> parked at the current frequency: ok")

# 4d. impulse noise regression (measured live 2026-10-08: this site shows
# one saturating bin per FFT row at a wandering offset): must NOT be
# detected as a signal, however hot the impulses are
def impulse_rowfn(amp=700.0):
    def rowfn(tune, k):
        m = noise_row(40 + (k % 4)).astype(np.float64)
        m[int(np.random.RandomState(1000 + k).randint(0, FFT_SIZE))] = amp
        return m.astype(np.float32)
    return rowfn

state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
stop = feed(entry, impulse_rowfn())
sc = scan.start_scan(SERIAL, 100.0, 100.4, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "nothing", f"impulse noise falsely detected: {sc}"
print("4d. saturating impulse noise ignored (clip + time-average): ok")

# 4e. a real carrier must still be found THROUGH that impulse noise
state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
stop = feed(entry, carrier_rowfn(impulses=True))
sc = scan.start_scan(SERIAL, 100.0, 100.4, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "found", sc["result"]
assert abs(sc["found_hz"] - SIG_ABS) <= 2000, sc["found_hz"]
print("4e. carrier found through impulse noise, parked at 99.95 MHz: ok")

# 4f. tune-relative spur regression (the live phantom-finder bug: finds
# at +40.8 kHz from EVERY step, parked on nothing): the re-center
# confirm must reject it — the spur moves away from 0 Hz after the
# retune — and the sweep finishes with 'nothing'
def spur_rowfn(tune, k):
    m = noise_row(60 + (k % 2)).astype(np.float64)
    m = add_hump(m, 40800.0)   # just outside the DC-spike mask, like the live one
    return m.astype(np.float32)

state.sdrs[SERIAL] = entry = make_entry()
state.manual_dongle_freq.clear()
stop = feed(entry, spur_rowfn)
sc = scan.start_scan(SERIAL, 100.0, 100.4, 200)
wait_done(sc)
stop.set()
assert sc["result"] == "nothing", f"tune-relative spur falsely parked: {sc}"
assert SERIAL not in state.manual_dongle_freq, "spur must not leave the dongle parked"
print("4f. tune-relative spur rejected by the re-center confirm: ok")

# restore global state
state.manual_dongle_freq.clear(); state.manual_dongle_bw.clear(); state.scans.clear()
state.manual_dongle_freq.update(saved[0]); state.manual_dongle_bw.update(saved[1])
state.scans.update(saved[2])
del state.sdrs[SERIAL]
scan._sample = _orig_sample
print("all scan tests passed")
