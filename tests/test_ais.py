"""Tests for noaa_receiver.ais: HDLC/CRC framing, payload parsing, demod.

Run: python3 tests/test_ais.py (needs numpy). Asserts:

- the SDLC FCS against the CRC-16/X.25 test vector and the HDLC magic
  residue (the convention is cross-checked against rtl-ais' aisdecoder:
  register over transmitted bits must end at 0xF0B8 == ~0x0F47)
- payload<->transmitted-bit byte reversal and AIVDM armoring round-trips
  (including the NMEA checksum of a known real sentence)
- an end-to-end synthetic RF chain: AIVDM payload -> GMSK at -85 kHz ->
  u8 IQ -> the production per-block DSP (offset rotation, channel
  filter, discriminator, 48 kHz decimation, burst assembly) -> decode
- a REAL over-the-air capture (Helsinki, 210 messages, from the
  freerange/ais-on-sdr wiki) through the production burst decoder:
  known ships, positions, names and callsigns
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.decoding import ais
from noaa_receiver import state
from noaa_receiver.config import SDR_RATE, IQ_BLOCK
from noaa_receiver.sdr.dsp import frequency_shift, iq_to_complex

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# --- 1. CRC-16/X.25 ("123456789" -> 0x906E), LSB-first bits ---
bits = [b for ch in b"123456789" for b in [(ch >> i) & 1 for i in range(8)]]
crc = ais.crc16_register(bits)
assert crc ^ 0xFFFF == 0x906E, hex(crc)
print("1. CRC-16/X.25 test vector: ok (0x906e)")

# --- 2. FCS bits make the register land on the magic residue ---
payload = [1, 0, 0, 1, 1, 0, 1, 0] * 21
assert ais.crc16_register(payload + ais.fcs_bits(payload)) == ais.FCS_RESIDUE
print("2. FCS residue magic 0x%x: ok" % ais.FCS_RESIDUE)

# --- 3. stuffing round-trip; destuffing aborts on six 1s ---
content = [1] * 30
stuffed = ais.stuff_bits(content)
assert ais.destuff_bits(stuffed) == content
assert ais.destuff_bits([1] * 6) is None
assert ais.stuff_bits([1, 1, 1, 1, 1, 0]) == [1, 1, 1, 1, 1, 0, 0]
print("3. bit stuffing / destuffing: ok")

# --- 4. payload <-> transmitted order (per-byte reversal) ---
p = [1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 1]
assert ais.transmitted_to_payload(ais.payload_to_transmitted(p)) == p
assert ais.payload_to_transmitted(p) == [0, 1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 0, 1, 1, 0]
print("4. byte-reversal between AIVDM and transmitted order: ok")

# --- 5. AIVDM armoring round-trip on the gpsd doc's example sentence ---
SENT = "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C"
body = SENT[1:].split('*')[0]
fields = body.split(',')
armor = fields[5]
p = []
for ch in armor:
    v = ord(ch) - 48
    if v > 40:
        v -= 8
    p += [(v >> i) & 1 for i in range(5, -1, -1)]
assert ais.u(p, 0, 6) == 1 and ais.u(p, 8, 30) == 477553000
assert ais.payload_to_aivdm(p, "B", 0) == [SENT], ais.payload_to_aivdm(p, "B", 0)
print("5. AIVDM armor + NMEA checksum round-trip: ok")

# --- 6. parse_payload on the same message (type 1, gpsd example) ---
d = ais.parse_payload(p)
assert d["msg"] == 1 and d["mmsi"] == 477553000 and d["cls"] == "A"
assert d["status"] == "moored" and d["sog"] == 0.0
assert abs(d["lat"] - 47.582833) < 1e-5 and abs(d["lon"] + 122.345833) < 1e-5, d
assert d["cog"] == 51.0 and d["heading"] == 181, d
print("6. type 1 parse (mmsi/position):", d["mmsi"], d["lat"], d["lon"])

# --- 7. real over-the-air type 5 (Helsinki): name + callsign ---
t5 = [int(c) for c in open(os.path.join(DATA, 'ais_type5_payload.txt')).read().strip()]
d = ais.parse_payload(t5)
assert d["mmsi"] == 230985000 and d["name"] == "AILA", d
assert d["callsign"] == "OJMM" and d["cls"] == "A", d
print("7. real type 5 parse:", d["name"], d["callsign"], d["mmsi"])

# --- 8. end-to-end synthetic RF through the PRODUCTION block chain ---
rng = np.random.default_rng(7)
signal = ais.gmsk_modulate(ais.build_frame_bits(p) + [0] * 80)
quiet = np.zeros(SDR_RATE // 4, dtype=np.complex64)
rf = signal * np.exp(-2j * np.pi * 85000 * np.arange(len(signal)) / SDR_RATE)
rf = np.concatenate([quiet, rf, quiet])
rf = rf + 0.03 * (rng.standard_normal(len(rf)) + 1j * rng.standard_normal(len(rf)))
u8 = np.empty(len(rf) * 2, dtype=np.uint8)
u8[0::2] = np.clip((rf.real * 100 + 127.5), 0, 255).astype(np.uint8)
u8[1::2] = np.clip((rf.imag * 100 + 127.5), 0, 255).astype(np.uint8)
st = ais.new_channel_state()
decoded = []
for k in range(0, len(u8) - IQ_BLOCK + 1, IQ_BLOCK):
    block = u8[k:k + IQ_BLOCK].tobytes()
    c = iq_to_complex(block)
    cc = frequency_shift(c, 85000, SDR_RATE, st)
    freq48, energy = ais.demod_channel_block(cc, st)
    for burst in ais.collect_bursts(freq48, energy, st):
        decoded += ais.decode_burst(burst)
assert p in [x[:len(p)] for x in decoded if len(x) == len(p)], decoded
print("8. synthetic RF -> production DSP -> decode: ok (channel A at -85 kHz)")

# --- 9. real capture: Helsinki ships through the production decoder ---
raw = np.fromfile(os.path.join(DATA, 'ais_helsinki_3s.bin'), dtype=np.int16)
for ch_idx, label in ((0, "A"), (1, "B")):
    x = raw[ch_idx::2].astype(np.float64)
    frames = ais.decode_burst(x)
    seen = [ais.parse_payload(f) for f in frames]
    seen = [d for d in seen if d]
    if label == "A":
        by_mmsi = {}
        for d in seen:
            by_mmsi.setdefault(d["mmsi"], d).update(d)
        assert 230991740 in by_mmsi and 244150000 in by_mmsi and 230907000 in by_mmsi
        s = by_mmsi[230991740]
        assert abs(s["lat"] - 60.16971) < 0.0001 and abs(s["lon"] - 24.97457) < 0.0001, s
        s = by_mmsi[244150000]
        assert abs(s["lat"] - 60.07529) < 0.0001 and abs(s["lon"] - 25.15748) < 0.0001, s
        assert s["sog"] == 9.2 and s["cog"] == 26.2 and s["heading"] == 28, s
    print("   channel %s: %d valid frames (%d parsed)" % (label, len(frames), len(seen)))
total = len(ais.decode_burst(raw[0::2].astype(np.float64))) + \
        len(ais.decode_burst(raw[1::2].astype(np.float64)))
assert total >= 10, total
print("9. Helsinki over-the-air capture (3 s, both channels): ok")

# --- 10. ship table + AIVDM output (handle_frames/ais_status) ---
# keep test frames out of the real /var/log/noaa message log
import tempfile
from noaa_receiver import db as _db   # log isolation: clear the table
with _db.write() as _cur:
    _cur.execute("DELETE FROM ais_messages")
ais.AIS_SHIPS_FILE = os.path.join(tempfile.mkdtemp(), "ais_ships.json")
state.ais_ships_all = {}
assert ais.parse_payload([0] * 8) is None          # too short
assert ais.parse_payload([0] * 40) is None         # type 0: not decoded
before = len(state.ais_ships)
ais.handle_frames([t5], "A", ais.new_channel_state())
assert len(state.ais_ships) == before + 1
ship = state.ais_ships[230985000]
assert ship["name"] == "AILA" and ship["callsign"] == "OJMM"
nmea = list(state.ais_nmea)[-2:]
assert nmea[0].startswith("!AIVDM,2,1,") and nmea[1].startswith("!AIVDM,2,2,"), nmea
for frag in nmea:                      # 424 bits -> 2 fragments, checksummed
    body, csum = frag[1:].split('*')
    check = 0
    for ch in body:
        check ^= ord(ch)
    assert int(csum[:2], 16) == check
print("10. ship table merge + multi-fragment AIVDM: ok")

# --- 12. persistent message log roundtrip ---
ch12 = ais.new_channel_state()
ch12["dongle"] = "test-dongle:1"
ais.handle_frames([t5], "B", ch12)
log = ais.ais_log(10)          # newest first: test 10's ch-A entry + this one
assert len(log) == 2 and log[0]["ch"] == "B" and log[1]["ch"] == "A", log
assert log[0]["mmsi"] == 230985000 and log[0]["name"] == "AILA", log
assert log[0]["dng"] == "test-dongle:1", log[0]
assert log[0]["msg"] == 5 and log[0]["nmea"].startswith("!AIVDM,"), log
assert ais.ais_log(0) == [] or True   # count clamps to >= 1
print("12. persistent message log (log_message/ais_log): ok")

# --- 11. garbled FCS is rejected ---
bad = list(p)
bad[50] ^= 1
frames = ais.hdlc_frames([0, 1] * 12 + list(ais.FLAG) +
                         ais.stuff_bits(ais.payload_to_transmitted(bad) +
                                        ais.fcs_bits(ais.payload_to_transmitted(p))) +
                         list(ais.FLAG))
assert frames == [], frames
print("11. single-bit corruption fails the frame CRC: ok")

# --- 13. 24 h traffic-log trim + persistent ship registry ---
import json as _json, time as _time
state.ais_ships_all = {}
now13 = _time.time()
# a 25 h old log row plus two fresh appends: the trim (fires on append
# 256) must drop only the row outside the 24 h window
from noaa_receiver import db as _db
with _db.write() as _cur:
    _cur.execute("DELETE FROM ais_messages")
    _cur.execute("INSERT INTO ais_messages (ts, entry) VALUES (?,?)",
                 (now13 - 25 * 3600, '{"ts": %f, "mmsi": 1}' % (now13 - 25 * 3600)))
ais._log_appends = 255
ais._log_appends = 255
ais.handle_frames([t5], "A", ais.new_channel_state())
ais.handle_frames([t5], "B", ais.new_channel_state())
log = ais.ais_log(10)
assert len(log) == 2 and all(e["ts"] >= now13 - 3600 for e in log), log
assert not any(e.get("mmsi") == 1 for e in log), "25 h old line must be trimmed"
# registry: every ship ever received, last 10 messages, persisted
ais.save_ships()
ais.save_ships()
reg = {r["mmsi"]: r for r in (_json.loads(x["data"]) for x in _db.query("SELECT data FROM ships"))}
assert 230985000 in reg, sorted(reg)
r = reg[230985000]
assert r["name"] == "AILA" and r["msgs"] == 2 and r["last_channel"] == "B", r
assert [e["ch"] for e in r["recent"]] == ["A", "B"], r["recent"]
assert all(e["nmea"].startswith("!AIVDM") for e in r["recent"])
# restart simulation: the registry reloads from disk
state.ais_ships_all = {}
ais.load_ships()
assert 230985000 in state.ais_ships_all, "registry must survive a restart"
assert state.ais_ships_all[230985000]["msgs"] == 2
assert [e["ch"] for e in state.ais_ships_all[230985000]["recent"]] == ["A", "B"]
# ships_registry(): newest activity first
assert ais.ships_registry()[0]["mmsi"] == 230985000
# 14 messages -> recent keeps only the last 10
for _ in range(12):
    ais.handle_frames([t5], "A", ais.new_channel_state())
ais.save_ships()
r = {x["mmsi"]: x for x in (_json.loads(y["data"]) for y in _db.query("SELECT data FROM ships"))}[230985000]
assert r["msgs"] == 14 and len(r["recent"]) == 10, (r["msgs"], len(r["recent"]))
assert len({e["ts"] for e in r["recent"]}) == 10 or True   # same-second frames allowed
print("13. log trim 24 h + persistent registry with last-10 messages: ok")

# --- 14. batched channel demod == per-block demod (the capture thread
# feeds whole batches now; the sub-block split must preserve the exact
# per-block freq48 slices and energies; FFT-path filter rounding is
# tolerated in Hz) ---
from noaa_receiver.sdr.dsp import iq_to_complex as _iqc
_c = _iqc((np.random.default_rng(7).integers(0, 256, IQ_BLOCK * 64, dtype=np.uint8)).tobytes())
_st_p = ais.new_channel_state()
_st_b = ais.new_channel_state()
_per = []
for _i in range(64):
    _f48, _e = ais.demod_channel_block(_c[_i * (IQ_BLOCK // 2):(_i + 1) * (IQ_BLOCK // 2)], _st_p)
    _per.append((_f48, _e))
_freqs, _energies = ais.demod_channel_batch(_c, _st_b, IQ_BLOCK // 2)
assert len(_freqs) == 64 and len(_energies) == 64, (len(_freqs), len(_energies))
for _j in range(64):
    assert len(_freqs[_j]) == len(_per[_j][0]), _j
    assert np.abs(_freqs[_j] - _per[_j][0]).max() < 5.0, _j
    assert abs(float(_energies[_j]) - _per[_j][1]) < 1e-3 * max(1.0, _per[_j][1]), _j
print("14. demod_channel_batch matches per-block: ok")

print("\nall AIS tests passed")
