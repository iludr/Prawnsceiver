"""Tests for noaa_receiver.decoding.aprs: Bell 202 AFSK demodulation,
AX.25 framing, APRS parsing, packet plumbing.

Run: python3 tests/test_aprs.py (needs numpy). Asserts:

- build_frame -> frame_bits -> ax25_frames round-trip: the payload
  comes back byte-identical from the raw NRZI data bits (flags,
  stuffing and the X.25 FCS shared with the AIS chain)
- parse_ax25: src/dst callsigns + SSIDs, digipeater chain, info text;
  non-UI frames are rejected (control/PID)
- parse_position: uncompressed !/= beacons, N/S and E/W signs, symbol
- an end-to-end synthetic RF chain: AX.25 frames -> Bell 202 AFSK on
  FM (phase-continuous tone changes) -> noise -> the production
  demodulator fed in many IQ batches (cross-batch FIR tail and 48 kHz
  decimation phase continuity) -> energy-gated burst assembly -> the
  original frames back, position included
- a corrupted frame is rejected by the FCS without losing the second
  frame in the same burst
- handle_frames: station registry, packet ring, waterfall marks and
  the persistent aprs_messages log (isolated temp database)
- the all-time station registry (aprs_stations table): newest data
  merged over old, position kept only from frames that carry one,
  last-10 frame history, save/load round-trip through the database
- telemetry: T# beacons decoded, channels scaled by the
  PARM/UNIT/EQNS definition messages (value = a*raw^2 + b*raw + c),
  digital bits named, plain messages ignored, persistence through
  the registry round-trip
- telemetry backfill: the rolling aprs_messages log replays
  through the telemetry parsers at registry load - a wiped
  registry table rebuilds the decoded channels from frames
- Mic-E: position/speed/course decoded from the destination
  field and packed info bytes (test encoder covers N/S, E/W,
  the message bits and the minutes-60 wrap), end-to-end
  through handle_frames with the decoded comment as text
- APRS text messages: parse_message, the messages_view filter
  (telemetry definitions out, acks flagged)

Memory-light by design: the test signal is ~3 s of 2.4 MHz IQ (~30 MB
of complex64) and everything else is bounded - it runs comfortably
under a 1.5 GB ulimit.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                      os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.decoding import aprs
from noaa_receiver import state
import threading  # noqa: E402
from collections import deque  # noqa: E402

# --- 1. build_frame -> frame_bits -> ax25_frames round-trip ---
p1 = aprs.build_frame("DL1ABC", "APRS", "!4903.78N/01206.43E&CrabArk beacon",
                      "WIDE1-1", src_ssid=7)
bits = aprs.frame_bits(p1)
got = aprs.ax25_frames(bits)
assert len(got) == 1 and got[0] == p1, (len(got),)
print("1. frame build -> air bits -> ax25_frames: ok")

# --- 2. parse_ax25: addresses, digis, info ---
f = aprs.parse_ax25(p1)
assert f is not None
assert f["src"] == "DL1ABC-7" and f["dst"] == "APRS", f
assert f["digis"] == ["WIDE1-1"], f
assert f["info"].startswith("!4903.78N/01206.43E&"), f
f2 = aprs.parse_ax25(aprs.build_frame("OE9XYZ", "APRS", ">hello world"))
assert f2["src"] == "OE9XYZ" and f2["info"] == ">hello world", f2
# non-UI frame rejected: the control byte is not 0x03
def build_non_ui():
    p = aprs.build_frame("DL1ABC", "APRS", "test")
    ctrl_off = 14 * 8           # control byte follows dst+src (14 bytes)
    p[ctrl_off] = 0x13
    return p
assert aprs.parse_ax25(build_non_ui()) is None
print("2. parse_ax25 (callsigns, SSIDs, digis, UI-only): ok")

# --- 3. parse_position ---
lat, lon, sym = aprs.parse_position("!4903.78N/01206.43E&")
assert abs(lat - (49 + 3.78 / 60)) < 1e-9 and abs(lon - (12 + 6.43 / 60)) < 1e-9
assert sym == "/&", sym
lat, lon, _ = aprs.parse_position("=3412.34S/05623.45W>")
assert abs(lat - -(34 + 12.34 / 60)) < 1e-9 and abs(lon - -(56 + 23.45 / 60)) < 1e-9
assert aprs.parse_position(">no position here") is None
print("3. parse_position (N/S E/W, symbol): ok")

# --- 4. end-to-end AFSK-on-FM chain through the production demod ---
info = "!4903.78N/01206.43E&CrabArk test beacon"
p2 = aprs.build_frame("DL1ABC", "APRS", info, "WIDE1-1", src_ssid=7)
p3 = aprs.build_frame("OE9XYZ", "APRS", ">second frame in the same burst")
# one burst: frame + frame back-to-back, ~1.3 s of RF
sig1 = aprs.afsk_modulate(aprs.frame_bits(p2) + [0, 1] * 40
                          + aprs.frame_bits(p3))
rng = np.random.default_rng(11)
quiet = 0.12 * (rng.standard_normal(240000)
                + 1j * rng.standard_normal(240000)).astype(np.complex64)
sig = np.concatenate([quiet, sig1, quiet])
sig = sig + 0.05 * (rng.standard_normal(len(sig))
                    + 1j * rng.standard_normal(len(sig))).astype(np.complex64)
st = aprs.new_channel_state("test:0")
CH = 240000          # 100 ms of RF per batch: many batches per burst
frames = []
for i in range(0, len(sig), CH):
    # the synthetic signal is already at DC - the live thread's
    # frequency_shift (offset-tune rotation) has nothing to undo here
    freq48, energy = aprs.demod_channel_batch(sig[i:i + CH], st)
    if len(freq48) == 0:
        continue
    for burst in aprs.collect_bursts(freq48, energy, st):
        frames += aprs.decode_burst(burst)
assert len(frames) == 2, (len(frames),)
g2, g3 = frames
assert aprs.parse_ax25(g2)["info"] == info, aprs.parse_ax25(g2)
assert aprs.parse_ax25(g3)["src"] == "OE9XYZ"
pos = aprs.parse_position(aprs.parse_ax25(g2)["info"])
assert pos is not None and abs(pos[0] - (49 + 3.78 / 60)) < 1e-6, pos
print("4. AFSK-on-FM RF chain: 2 frames across %d batches: ok"
      % ((len(sig) + CH - 1) // CH))

# --- 5. corrupted frame rejected, its neighbor survives ---
bad_bits = aprs.frame_bits(p2)
mid = len(bad_bits) // 2
bad_bits[mid] ^= 1
sig2 = aprs.afsk_modulate(aprs.frame_bits(p3) + [0, 1] * 40 + bad_bits)
sig2 = sig2 + 0.05 * (rng.standard_normal(len(sig2))
                      + 1j * rng.standard_normal(len(sig2))).astype(np.complex64)
sig2 = np.concatenate([quiet, sig2, quiet])
st2 = aprs.new_channel_state("test:0")
frames2 = []
for i in range(0, len(sig2), CH):
    freq48, energy = aprs.demod_channel_batch(sig2[i:i + CH], st2)
    if len(freq48) == 0:
        continue
    for burst in aprs.collect_bursts(freq48, energy, st2):
        frames2 += aprs.decode_burst(burst)
assert len(frames2) == 1, (len(frames2),)
assert aprs.parse_ax25(frames2[0])["src"] == "OE9XYZ"
print("5. FCS rejects a corrupted frame, neighbor decodes: ok")

# --- 6. decoded frames -> registry / log / waterfall marks ---
state.sdrs["test:0"] = {"lock": threading.Lock(), "wf_marks": deque(maxlen=64)}
st3 = aprs.new_channel_state("test:0")
aprs.handle_frames([g2, g3], st3, now=1234.5)
with state.aprs_lock:
    stations = dict(state.aprs_stations)
    packets = list(state.aprs_packets)
assert set(stations) == {"DL1ABC-7", "OE9XYZ"}, stations
assert stations["DL1ABC-7"]["lat"] is not None and stations["DL1ABC-7"]["msgs"] == 1
assert len(packets) == 2 and packets[0]["call"] == "DL1ABC-7"
log = aprs.aprs_log(5)
assert len(log) == 2 and log[0]["call"] == "OE9XYZ" \
       and log[1]["call"] == "DL1ABC-7", log   # newest-first
assert list(state.sdrs["test:0"]["wf_marks"]) == [0.0, 0.0]
snap = aprs.aprs_status()
assert snap["enabled"] is False and len(snap["stations"]) == 2 and len(snap["packets"]) == 2
aprs.handle_frames([g2], st3, now=1236.0)
with state.aprs_lock:
    assert state.aprs_stations["DL1ABC-7"]["msgs"] == 2
print("6. registry / aprs_messages log / waterfall marks / status: ok")

# --- 7. all-time station registry (aprs_stations table) ---
with state.aprs_lock:
    state.aprs_stations_all.clear()
aprs._stations_save_at = 0.0
aprs.handle_frames([g2, g3], st3, now=1238.0)   # fresh registry
aprs.handle_frames([g2], st3, now=1240.0)      # same station again
reg = aprs.stations_registry()
assert [s["call"] for s in reg] == ["DL1ABC-7", "OE9XYZ"], reg   # newest first
d7, oe = reg[0], reg[1]
assert d7["msgs"] == 2 and len(d7["recent"]) == 2, d7
assert d7["lat"] is not None and d7["first_seen"] < d7["last_seen"], d7
assert oe["msgs"] == 1 and oe["lat"] is None and len(oe["recent"]) == 1, oe
aprs.save_stations()
aprs.load_stations()                            # restart round-trip
reg2 = aprs.stations_registry()
assert [s["call"] for s in reg2] == ["DL1ABC-7", "OE9XYZ"], reg2
d7b = reg2[0]
assert d7b["msgs"] == 2 and len(d7b["recent"]) == 2 and d7b["lat"] is not None, d7b
print("7. all-time station registry (aprs_stations table): ok")

# --- 8. telemetry: T# beacons + PARM/UNIT/EQNS/BITS messages ---
# raw forms copied from the DB0BMW digi the station actually hears
T_RAW = "T#099,188,128,000,000,000,00000000"
tm = aprs.parse_telemetry(T_RAW)
assert tm == {"seq": 99, "analog": [188, 128, 0, 0, 0], "bits": "00000000"}, tm
assert aprs.parse_telemetry(">status text is not telemetry") is None
assert aprs.parse_telemetry("T#99,xxx") is None            # bad analog
d = aprs.parse_telemetry_definition(":DB0BMW   :PARM.Batt.,Temp")
assert d == ("DB0BMW", "PARM", "Batt.,Temp"), d
assert aprs.parse_telemetry_definition(":DB0BMW   :hello there") is None
assert aprs.parse_telemetry_definition("!4858.68N/01202.98E#") is None
p_t = aprs.build_frame("DB0BMW", "APMI0A", T_RAW)
aprs.handle_frames([p_t], st3, now=1250.0)
with state.aprs_lock:
    tlm = dict(state.aprs_stations_all["DB0BMW"]["telemetry"])
assert tlm["seq"] == 99 and tlm["channels"][0]["raw"] == 188
assert tlm["channels"][0]["value"] == 188      # no EQNS yet: identity
p_defs = [
    aprs.build_frame("DB0BMW", "APMI0A", ":DB0BMW   :PARM.Batt.,Temp,A3,A4,A5,O1,O2,O3,O4,I1,I2,I3,I4"),
    aprs.build_frame("DB0BMW", "APMI0A", ":DB0BMW   :UNIT.Volt,C,None,None,None,On,On,On,On,Hi,Hi,Hi,Hi"),
    aprs.build_frame("DB0BMW", "APMI0A", ":DB0BMW   :EQNS.0,0.075,0,0,0,0,0,0,0,0,0,0,0,0,0"),
    aprs.build_frame("DB0BMW", "APMI0A", ":DB0BMW   :BITS.11111111,Telemetry DB0BMW"),
    aprs.build_frame("DL1ABC", "APRS", ":DB0BMW   :hello there"),   # plain message
]
aprs.handle_frames(p_defs, st3, now=1251.0)
with state.aprs_lock:
    tlm = dict(state.aprs_stations_all["DB0BMW"]["telemetry"])
c0 = tlm["channels"][0]
assert c0 == {"name": "Batt.", "raw": 188, "value": 14.1, "unit": "Volt"}, c0
assert tlm["channels"][1]["value"] == 0.0, tlm["channels"][1]   # EQNS all zero
assert tlm["channels"][2]["name"] == "A3"
assert [b["name"] for b in tlm["bits"]][:2] == ["O1", "O2"], tlm["bits"]
assert all(b["value"] == 0 for b in tlm["bits"])
assert tlm["label"] == "Telemetry DB0BMW"
aprs.save_stations()
aprs.load_stations()                            # restart round-trip
with state.aprs_lock:
    tlm2 = dict(state.aprs_stations_all["DB0BMW"]["telemetry"])
assert tlm2["channels"][0]["value"] == 14.1 and tlm2["label"] == "Telemetry DB0BMW"
print("8. telemetry decode (T#, PARM/UNIT/EQNS/BITS, persistence): ok")

# --- 9. telemetry backfill from the persisted frame log ---
# wipe the registry table but KEEP aprs_messages: load_stations
# must rebuild the decoded telemetry from the logged frames
with aprs.db.write() as cur:
    cur.execute("DELETE FROM aprs_stations")
with state.aprs_lock:
    state.aprs_stations_all.clear()
aprs.load_stations()
with state.aprs_lock:
    tlm3 = dict(state.aprs_stations_all["DB0BMW"]["telemetry"])
c0 = tlm3["channels"][0]
assert c0 == {"name": "Batt.", "raw": 188, "value": 14.1, "unit": "Volt"}, c0
assert tlm3["seq"] == 99 and tlm3["label"] == "Telemetry DB0BMW"
assert [b["name"] for b in tlm3["bits"]][:2] == ["O1", "O2"]
print("9. telemetry backfill from the aprs_messages log: ok")

# --- 10. Mic-E: mobile positions from the destination field ---
# Test encoders (inverse of the decoder tables): the dest letter
# forms carry N/S, +100-deg and E/W; minutes < 10 ride the +60
# wrap; the low speed digit hides in the course byte.
def mice_dst(lat, lon):
    d = int(abs(lat))
    m = round((abs(lat) - d) * 60, 4)
    mt, mh = int(m), int(round((m - int(m)) * 100))
    return (str(d // 10) + str(d % 10) + str(mt // 10)
            + (chr(80 + mt % 10) if lat >= 0 else str(mt % 10))
            + (chr(80 + mh // 10) if abs(lon) >= 100 else str(mh // 10))
            + (chr(80 + mh % 10) if lon < 0 else str(mh % 10)))
def mice_info(lon, sym, speed_kn, course, comment):
    lo = int(abs(lon))
    lm = round((abs(lon) - lo) * 60, 4)
    lmt = int(lm)
    if lmt < 10:
        lmt += 60                      # the [&-a] regex range
    lht = int(round((lm - int(lm)) * 100))
    b = [lo % 100 + 28, lmt + 28, lht + 28,
         speed_kn // 10 + 28,
         (speed_kn % 10) * 10 + course // 100 + 28,
         course % 100 + 28]
    return "".join(chr(x) for x in b) + sym + comment

info_m = mice_info(12.05, ">/", 42, 123, "CrabArk mobile")
assert mice_dst(49.05, 12.05) == "490S00", mice_dst(49.05, 12.05)
m1 = aprs.parse_mice("490S00", info_m)
assert abs(m1["lat"] - 49.05) < 1e-6 and abs(m1["lon"] - 12.05) < 1e-6, m1
assert m1["sym"] == "/>" and m1["cse"] == 123, m1
assert abs(m1["spd"] - 77.8) < 0.05, m1          # 42 kn -> km/h
assert m1["msg"] is None and m1["comment"] == "CrabArk mobile", m1
# south + west: digit-form N/S, letter-form E/W
assert mice_dst(-49.05, -12.05) == "49030P", mice_dst(-49.05, -12.05)
m2 = aprs.parse_mice("49030P", info_m)
assert abs(m2["lat"] + 49.05) < 1e-6 and abs(m2["lon"] + 12.05) < 1e-6, m2
# A-J message bits -> the custom table; degree digit still decodes
m3 = aprs.parse_mice("E90S00", info_m)
assert abs(m3["lat"] - 49.05) < 1e-6 and m3["msg"] == "C3: Custom-3", m3
# a pure-digit destination is NOT the 000/Emergency message row
assert aprs.parse_mice("490000", mice_info(12.05, ">/", 42, 123, ""))["msg"] is None
# guards: tocalls and status text are not Mic-E
assert aprs.parse_mice("APMI0A", info_m) is None
assert aprs.parse_mice("490S00", ">status text here") is None
# end-to-end: Mic-E fallback when no uncompressed position, comment
# as text, speed/course through the registry merge
p_m = aprs.build_frame("DL1MOB", mice_dst(49.05, 12.05), info_m,
                      "WIDE1-1", src_ssid=9)
aprs.handle_frames([p_m], st3, now=1260.0)
with state.aprs_lock:
    mob = dict(state.aprs_stations_all["DL1MOB-9"])   # keyed with SSID
assert abs(mob["lat"] - 49.05) < 1e-6 and abs(mob["lon"] - 12.05) < 1e-6, mob
assert mob["spd"] == 77.8 and mob["cse"] == 123 and mob["sym"] == "/>", mob
assert mob["text"] == "CrabArk mobile", mob
print("10. Mic-E positions (N/S, E/W, message bits, comment): ok")

# --- 11. APRS text messages: parse + messages_view filter ---
assert aprs.parse_message(":DL1DEF   :hi") == {"to": "DL1DEF", "text": "hi"}
assert aprs.parse_message("!4858.68N/01202.98E#") is None
p_c1 = aprs.build_frame("DL1ABC", "APRS", ":DL1DEF   :hi there, testing 1-2-3")
p_c2 = aprs.build_frame("DL1DEF", "APRS", ":DL1ABC   :ack3")
aprs.handle_frames([p_c1, p_c2], st3, now=1261.0)
mv = aprs.messages_view(500)
texts = [m["text"] for m in mv]
assert "hi there, testing 1-2-3" in texts, texts
assert not any(t.startswith(("PARM.", "UNIT.", "EQNS.", "BITS."))
               for t in texts), texts
chat = [m for m in mv if m["text"].startswith("hi there")][0]
assert chat["from"] == "DL1ABC" and chat["to"] == "DL1DEF" and chat["ack"] is None
ack = [m for m in mv if m["ack"]][0]
assert ack["from"] == "DL1DEF" and ack["to"] == "DL1ABC" and ack["ack"] == "ack", ack
print("11. APRS text messages (filter + ack flagging): ok")

# --- 12. GFSK 9600 chain (LASARsat digipeater mode): modulate ->
#     channel FIR -> discriminator -> burst gate -> slicer phase trial
#     -> G3RUH descramble -> FCS -> AX.25 ---
info_l = "!4903.78N/01206.43E&LASARsat relayed beacon"
pl1 = aprs.build_frame("OK0LSR", "APRS", info_l)
pl2 = aprs.build_frame("DL1ABC", "APRS", ">hello via LASARsat", "OK0LSR*")
# two frames back-to-back in one burst; ~0.36 s of RF at 9600 baud
sig_l = aprs.gfsk96_modulate(aprs.frame_bits(pl1) + [0, 1] * 100
                             + aprs.frame_bits(pl2))
rng_l = np.random.default_rng(21)
quiet = 0.12 * (rng_l.standard_normal(240000)
                + 1j * rng_l.standard_normal(240000)).astype(np.complex64)
sig_l = np.concatenate([quiet, sig_l, quiet])
sig_l = sig_l + 0.05 * (rng_l.standard_normal(len(sig_l))
                        + 1j * rng_l.standard_normal(len(sig_l))).astype(np.complex64)
stl = aprs.new_channel_state("test:0", lasar=True)
CH = 24000          # 10 ms of RF per batch - the production cadence
frames_l = []
for i in range(0, len(sig_l), CH):
    freq48, energy = aprs.demod_channel_batch(sig_l[i:i + CH], stl)
    if len(freq48) == 0:
        continue
    for burst in aprs.collect_bursts(freq48, energy, stl):
        frames_l += aprs.decode_burst_gfsk96(burst)
assert len(frames_l) == 2, (len(frames_l),)
gl1, gl2 = frames_l
assert aprs.parse_ax25(gl1)["info"] == info_l, aprs.parse_ax25(gl1)
d2 = aprs.parse_ax25(gl2)
assert d2["src"] == "DL1ABC" and d2["digis"] == ["OK0LSR"], d2   # 7-char "OK0LSR*" truncates to the 6-char callsign
assert aprs.parse_ax25(gl1)["src"] == "OK0LSR"
print("12. GFSK 9600 (G3RUH) RF chain: 2 frames, digipeater chain kept: ok")

# --- 13. GFSK 9600 with +/-6 kHz carrier offset (Doppler): the
#      median slicer must absorb it (LASARsat at 436.925 MHz swings
#      ~ +/-9 kHz over a pass) ---
sig_d = aprs.gfsk96_modulate(aprs.frame_bits(pl1), offset_hz=6000.0)
# quiet lead-in first: the burst gate's floor EMA must establish the
# noise floor before the signal arrives (a gate that meets the signal
# first mistakes it for silence)
quiet_d = 0.12 * (rng_l.standard_normal(240000)
                  + 1j * rng_l.standard_normal(240000)).astype(np.complex64)
sig_d = np.concatenate([quiet_d, sig_d, quiet_d])
sig_d = sig_d + 0.05 * (rng_l.standard_normal(len(sig_d))
                        + 1j * rng_l.standard_normal(len(sig_d))).astype(np.complex64)
std = aprs.new_channel_state("test:0", lasar=True)
frames_d = []
for i in range(0, len(sig_d), CH):
    freq48, energy = aprs.demod_channel_batch(sig_d[i:i + CH], std)
    if len(freq48) == 0:
        continue
    for burst in aprs.collect_bursts(freq48, energy, std):
        frames_d += aprs.decode_burst_gfsk96(burst)
assert len(frames_d) == 1, (len(frames_d),)
assert aprs.parse_ax25(frames_d[0])["info"] == info_l
print("13. GFSK 9600 with +6 kHz Doppler offset: decoded: ok")

# --- 14. scrambler round-trip: descramble(scramble(x)) == x ---
bits_x = [int(b) for b in rng_l.integers(0, 2, 512)]
assert aprs.descramble_g3ruh(aprs.scramble_g3ruh(bits_x)) == bits_x
print("14. G3RUH scrambler/descrambler round-trip: ok")

print("ALL APRS TESTS PASSED")
