"""Tests for noaa_receiver.decoding.orbcomm: SDPSK demod, Fletcher-16
framing, Ephemeris parsing, band channel detection, packet plumbing.

Run: python3 tests/test_orbcomm.py (needs numpy). Asserts:

- Fletcher-16 (mod-256 variant): hand-computed vector, and the tail
  property fletcher16(data + fletcher_tail(data)) == (0, 0)
- the four bit-stream orientations (MSB/LSB-first, polarity) all
  recover the 0x65 header byte; packet_bits is the inverse of
  _bytes_from_bits for the live-satellite orientation
- the header scan anchors a Sync packet at its exact bit position
  inside a noisy stream
- build_ephemeris <-> parse_ephemeris round-trip: position (ECEF
  20-bit quantization) and GPS week/time survive the packet encoding
- an end-to-end synthetic RF chain: packets -> SDPSK at 2.4 MHz ->
  noise -> the production demodulator fed in several IQ batches
  (cross-batch FIR tail / decimator phase / bit continuity) -> the
  original packets back, including grid loss and re-anchor on a
  corrupted slot
- _band_bin_range / _detect_channels: a carrier cluster above the
  tracked noise floor is found at its expected frequency
- handle_packets: live channel registry, packet ring, waterfall marks
  and the persistent orbcomm_messages log (isolated temp database)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.decoding import orbcomm
from noaa_receiver import state
from noaa_receiver.config import ORBCOMM_CENTER_HZ, SDR_RATE, FFT_SIZE
import threading  # noqa: E402

# --- 1. Fletcher-16 (mod 256): hand-computed vector + tail property ---
assert orbcomm.fletcher16(b"ab") == (97 + 98, (97 + 97 + 98) % 256)
for data in (b"", b"\x65", bytes(range(12)), b"orbcomm-downlink"):
    s1, s2 = orbcomm.fletcher16(data + orbcomm.fletcher_tail(data))
    assert (s1, s2) == (0, 0), (data, s1, s2)
print("1. Fletcher-16 mod-256 + tail property: ok")

# --- 2. bit-stream orientations all recover the 0x65 header byte ---
_H = (0, 1, 1, 0, 0, 1, 0, 1)
for rev, pol in ((False, False), (True, False), (False, True), (True, True)):
    base = tuple(1 - b for b in _H) if pol else _H
    bits = tuple(reversed(base)) if rev else base
    b = int(orbcomm._bytes_from_bits(bits, rev, pol)[0])
    assert b == 0x65, (rev, pol, hex(b))
print("2. MSB/LSB-first x polarity orientations: ok (0x65 in all four)")

# --- 3. build_packet -> packet_bits -> _bytes_from_bits round-trip ---
sync = orbcomm.build_packet(0x65, bytes(range(1, 9)), sat_id=0x34)
assert len(sync) == 12 and orbcomm.fletcher16(sync) == (0, 0)
bits = orbcomm.packet_bits(sync)          # LSB-first per byte: live case
back = orbcomm._bytes_from_bits(bits, True, False)
assert bytes(back) == sync, (back.hex(), sync.hex())
msg = orbcomm.build_packet(0x1A, bytes(range(10, 19)))
assert len(msg) == 12 and orbcomm.fletcher16(msg) == (0, 0)
print("3. packet build -> air bits -> bytes: ok (%s)" % sync.hex())

# --- 4. header scan anchors the first Sync packet exactly ---
rng = np.random.default_rng(7)
stream = list(rng.integers(0, 2, 150)) + orbcomm.packet_bits(sync) \
    + list(rng.integers(0, 2, 150))
anchor = orbcomm._find_anchor(np.array(stream, dtype=np.int8))
assert anchor is not None
ix, data, orient = anchor
assert ix == 150 and bytes(data) == sync and orient == (True, False), (ix, data.hex(), orient)
print("4. header anchor at bit %d, orientation %s: ok" % (ix, orient))

# --- 5. Ephemeris build <-> parse round-trip ---
x, y, z = 3.0e6, 4.0e6, 5.0e6
eph = orbcomm.build_ephemeris(0x34, x, y, z, week=1300, tow=250000)
assert len(eph) == 24 and orbcomm.fletcher16(eph) == (0, 0)
pos = orbcomm.parse_ephemeris(eph)
assert pos is not None
lat0, lon0, alt0 = orbcomm._ecef_to_lla(x, y, z)
# 20-bit ECEF fields: ~16 m per step, so ~100 m agreement is generous
assert abs(pos["lat"] - lat0) < 0.001 and abs(pos["lon"] - lon0) < 0.001, pos
assert abs(pos["alt"] - alt0) < 200, (pos["alt"], alt0)
assert pos["gps_week"] == 1300
from datetime import datetime, timedelta
want_gps = (datetime(1980, 1, 6)
            + timedelta(weeks=1300, seconds=250000)).strftime("%Y-%m-%d %H:%M:%S")
assert pos["gps_time"] == want_gps, (pos["gps_time"], want_gps)
print("5. Ephemeris round-trip: ok (%.4f, %.4f, %.0f m, week %d)"
      % (pos["lat"], pos["lon"], pos["alt"], pos["gps_week"]))

# --- 6. end-to-end SDPSK chain: packets -> RF -> production demod ---
fill = orbcomm.build_packet(0x1E, b"\x00" * 9)
fill2 = orbcomm.build_packet(0x1E, b"\x11" * 9)
sync2 = orbcomm.build_packet(0x65, bytes(range(3, 11)), sat_id=0x35)
msg2 = orbcomm.build_packet(0x1A, bytes(range(20, 29)))
expected = [sync, msg, fill, eph, sync2, msg2, fill2]
# packets back-to-back on the 96-bit grid (Ephemeris = 2 slots), a
# corrupted slot of all-ones bits between the two Sync packets, and
# an alternating idle preamble for the FIR to settle into
tx_bits = ([i % 2 for i in range(200)]
           + orbcomm.packet_bits(sync) + orbcomm.packet_bits(msg)
           + orbcomm.packet_bits(fill) + orbcomm.packet_bits(eph)
           + [1] * 96
           + orbcomm.packet_bits(sync2) + orbcomm.packet_bits(msg2)
           + orbcomm.packet_bits(fill2)
           + [i % 2 for i in range(96)])   # tail: the group delay shifts
# the stream one symbol, the last packet needs bits past its own end
sig = orbcomm.sdpsk_modulate(tx_bits)
rng = np.random.default_rng(2024)
sig = sig + 0.05 * (rng.standard_normal(len(sig))
                    + 1j * rng.standard_normal(len(sig)))
st = orbcomm.new_channel_state("test:1", "test:1:250",
                               ORBCOMM_CENTER_HZ + 250e3)
got = []
CH = 120000                       # ~240 bits per batch: many batches
for i in range(0, len(sig), CH):
    got += orbcomm.demod_batch(sig[i:i + CH], st)
got_hex = [bytes(p).hex() for p in got]
want_hex = [bytes(p).hex() for p in expected]
assert got_hex == want_hex, (got_hex, want_hex)
assert st["bad"] == 1, st["bad"]   # the all-ones slot: bad, then re-anchor
assert st["packets"] == 0 and st["phase"] is not None and st["grid"] is not None
print("6. SDPSK RF chain: %d packets across %d batches, 1 bad slot, "
      "grid re-anchored: ok" % (len(got), (len(sig) + CH - 1) // CH))

# --- 7. band scan: carrier cluster found at its frequency ---
k_lo, k_hi, bin_hz = orbcomm._band_bin_range(0)
assert 0 < k_lo < k_hi <= FFT_SIZE and 149 <= k_lo <= 151, (k_lo, k_hi)
det = {"floor": None, "channels": {}}
rows = [np.full(FFT_SIZE, 100.0) + rng.normal(0, 2.0, FFT_SIZE)
        for _ in range(60)]
for r in rows:
    orbcomm._detect_channels(r, det, k_lo, k_hi, bin_hz, 0)
assert det["channels"] == {} or all(
    not orbcomm._detect_channels(r, det, k_lo, k_hi, bin_hz, 0) for r in rows[-5:])
B = 220                           # display bin of the fake carrier
for _ in range(6):
    r = rows[0].copy()
    r[B - 1:B + 3] = 600.0        # ~4-bin cluster well above the floor
    found = orbcomm._detect_channels(r, det, k_lo, k_hi, bin_hz, 0)
    if found:
        break
assert found, "carrier cluster not detected"
freq, snr = found[0]
want = ORBCOMM_CENTER_HZ + (B + 0.5 - 256) * bin_hz
assert abs(freq - want) < 5000, (freq, want)
assert snr > 2.0, snr
assert orbcomm._quantize(freq) == round(freq / 500.0) * 500
print("7. band scan: carrier at %.1f kHz (bin %d), SNR %.1f: ok"
      % (freq / 1e3, B, snr))

# --- 8. decoded packets -> live registry / log / waterfall marks ---
state.sdrs["test:1"] = {"lock": threading.Lock()}
st2 = orbcomm.new_channel_state("test:1", "test:1:250",
                                ORBCOMM_CENTER_HZ + 250e3)
orbcomm.handle_packets(st2, [sync, eph], now=1000.0)
with state.orbcomm_lock:
    ch = state.orbcomm_channels["test:1:250"]
    pkts = list(state.orbcomm_packets)
assert ch["sat_id"] == bytes(sync[6:8]).hex(), ch
assert ch["lat"] == pos["lat"] and ch["lon"] == pos["lon"], ch
assert len(pkts) == 2 and pkts[0]["type"] == "Sync" and pkts[1]["type"] == "Ephemeris"
assert pkts[1]["freq_khz"] == 137750.0
log = orbcomm.orbcomm_log(5)
assert len(log) == 2 and log[0]["hex"] == eph.hex() and log[1]["hex"] == sync.hex()
marks = list(state.sdrs["test:1"]["wf_marks"])
assert marks == [250.0, 250.0], marks
snap = orbcomm.orbcomm_status()
assert snap["channels"][0]["key"] == "test:1:250" and len(snap["packets"]) == 2
print("8. registry / orbcomm_messages log / waterfall marks / status: ok")

print("ALL ORBCOMM TESTS PASSED")
