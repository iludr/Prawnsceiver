"""Tests for noaa_receiver.decoding.adsb: Mode S CRC, ADS-B payload
parsing, CPR position decoding and the envelope demodulator.

Run: python3 tests/test_adsb.py (needs numpy). Asserts:

- the Mode S CRC-24 (generator 0x1FFF409) against the worked examples
  of "The 1090 MHz Riddle": remainder of a payload, zero remainder of
  a complete message, a corrupted message, and ICAO recovery from
  Address Parity
- identification, airborne position (Q-bit altitude + global and
  local CPR), airborne velocity and surface position parses against
  the book's example messages — each one a real over-the-air frame
- an end-to-end synthetic RF chain: hex frame -> PPM envelope with
  noise -> the production demodulator (threshold, preamble check,
  3-phase bit slicing, CRC dispatch) -> same frame back, including
  frames straddling block boundaries and DF11 short replies
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.decoding import adsb
from noaa_receiver.decoding.adsb import (bits_to_hex, crc24,
                                         decode_batch, decode_cpr_global,
                                         decode_cpr_local, hex_to_bits,
                                         new_demod_state, parse_frame,
                                         parse_adsb, ppm_modulate,
                                         _movement_speed)
from noaa_receiver import state
from noaa_receiver.config import SDR_RATE

# --- 1. CRC-24 against the book's error-control chapter ---
payload = hex_to_bits("8D406B902015A678D4D220") + [0] * 24
assert crc24(payload) == 0xAA4BDA, hex(crc24(payload))
assert crc24(hex_to_bits("8D406B902015A678D4D220AA4BDA")) == 0
assert crc24(hex_to_bits("8D4CA251204994B1C36E60A5343D")) == 16
print("1. Mode S CRC-24 (generator 0x1FFF409): ok (AA4BDA / 0 / 16)")

# --- 2. ICAO address recovery via Address Parity ---
# book example: payload CRC CE2CA7, transmitted parity F24177 -> 3C6DD0
p = "A0001838CA380031440000"
assert crc24(hex_to_bits(p) + [0] * 24) == 0xCE2CA7
assert crc24(hex_to_bits(p) + [0] * 24) ^ 0xF24177 == 0x3C6DD0
assert bits_to_hex(hex_to_bits("8D4840D6202CC371C32CE0576098")) == \
    "8D4840D6202CC371C32CE0576098"
print("2. AP ICAO recovery + hex round-trip: ok (3C6DD0)")

# --- 3. identification: the book's KLM frame ---
d = parse_frame(hex_to_bits("8D4840D6202CC371C32CE0576098"))
assert d is not None and d["df"] == 17 and d["icao"] == "4840D6", d
assert d["tc"] == 4 and d["callsign"] == "KLM1023", d
print("3. identification:", d["icao"], d["callsign"])

# --- 4. airborne position: altitude + globally unambiguous CPR pair ---
EVEN = "8D40621D58C382D690C8AC2863A7"   # most recent
ODD = "8D40621D58C386435CC412692AD6"
de = parse_frame(hex_to_bits(EVEN))
do = parse_frame(hex_to_bits(ODD))
assert de["alt_ft"] == 38000, de
latE, lonE = de["cpr"][0] / 131072.0, de["cpr"][1] / 131072.0
latO, lonO = do["cpr"][0] / 131072.0, do["cpr"][1] / 131072.0
pos = decode_cpr_global(latE, lonE, latO, lonO, odd_newest=False)
assert abs(pos[0] - 52.2572021484375) < 1e-6, pos
assert abs(pos[1] - 3.91937255859375) < 1e-6, pos
print("4. airborne position (Q-bit 38000 ft, global CPR):", pos)

# --- 5. locally unambiguous CPR (reference position) ---
pos = decode_cpr_local(latE, lonE, False, 52.258, 3.918)
assert abs(pos[0] - 52.2572021484375) < 1e-6, pos
assert abs(pos[1] - 3.91937255859375) < 1e-6, pos
print("5. airborne position (local CPR with reference):", pos)

# --- 6. airborne velocity: ground speed and airspeed examples ---
v = parse_frame(hex_to_bits("8D485020994409940838175B284F"))
assert v["vel_st"] == 1 and abs(v["speed_kt"] - 159.2) < 0.05, v
assert abs(v["track_deg"] - 182.88) < 0.05 and v["vs_fpm"] == -832, v
v = parse_frame(hex_to_bits("8DA05F219B06B6AF189400CBC33F"))
assert v["vel_st"] == 3 and v["speed_kt"] == 375 and not v["ias"], v
assert abs(v["track_deg"] - 243.98) < 0.05 and v["vs_fpm"] == -2304, v
print("6. velocity: 159 kt 182.9° -832 fpm (GS) / 375 kt 244.0° -2304 (TAS)")

# --- 7. surface position: global pair + local decode + movement ---
SE, SO = "8C4841753AAB238733C8CD4020B1", "8C4841753A8A35323FAEBDAC702D"
se, so = parse_frame(hex_to_bits(SE)), parse_frame(hex_to_bits(SO))
assert se["onground"] and so["onground"]
pos = decode_cpr_global(se["cpr"][0] / 131072.0, se["cpr"][1] / 131072.0,
                        so["cpr"][0] / 131072.0, so["cpr"][1] / 131072.0,
                        odd_newest=True, surface=True,
                        lat_ref=51.990, lon_ref=4.375)
assert abs(pos[0] - 52.320607) < 1e-5 and abs(pos[1] - 4.734735) < 1e-5, pos
sl = parse_frame(hex_to_bits("8C4841753A9A153237AEF0F275BE"))
pos = decode_cpr_local(sl["cpr"][0] / 131072.0, sl["cpr"][1] / 131072.0,
                       True, 51.990, 4.375, surface=True)
assert abs(pos[0] - 52.320561) < 1e-5 and abs(pos[1] - 4.735735) < 1e-5, pos
assert _movement_speed(41) == 17 and _movement_speed(1) == 0.0
assert sl["track_deg"] == 92.8, sl
print("7. surface position (global + local CPR, movement 17 kt):", pos)

# --- 8. DF11 all-call reply (short frame, address parity) ---
# built from scratch: DF=11, CA=5, AA=4CA251, AP = CRC ^ AA
payload = [0, 1, 0, 1, 1] + [1, 0, 1] + \
    [(0x4CA251 >> i) & 1 for i in range(23, -1, -1)]
pi = crc24(payload + [0] * 24) ^ 0x4CA251
frame = payload + [(pi >> i) & 1 for i in range(23, -1, -1)]
DF11_HEX = bits_to_hex(frame)
d = parse_frame(frame)
assert d is not None and d["df"] == 11 and d["icao"] == "4CA251", d
assert len(DF11_HEX) == 14, DF11_HEX
# a corrupted AP must not pass
bad = list(frame)
bad[-1] ^= 1
assert parse_frame(bad) is None
print("8. DF11 all-call reply (AP syndrome == AA):", d["icao"], DF11_HEX)

# --- 9. end-to-end demod: PPM envelope + noise -> production decoder ---
rng = np.random.RandomState(42)
frames = ["8D4840D6202CC371C32CE0576098",          # identification
          EVEN,                                     # airborne position
          "8D485020994409940838175B284F",           # velocity
          DF11_HEX]                                 # DF11 all-call
batch = 0.05 + 0.02 * rng.rand(SDR_RATE)            # noise floor ~0.05
offsets = [2000, 9000, 24000, 37000]
for hexstr, off in zip(frames, offsets):
    bits = hex_to_bits(hexstr)
    env = ppm_modulate(bits)
    batch[off:off + len(env)] += env
st = new_demod_state()
out = decode_batch(batch, st)
got = {d["hex"]: d for d in out}
for hexstr in frames:
    assert hexstr in got, (hexstr, list(got))
assert got[frames[0]]["callsign"] == "KLM1023"
assert got[frames[1]]["alt_ft"] == 38000
assert got[frames[3]]["df"] == 11
assert st["bad"] == 0 and st["frames"] == len(frames), st
print(f"9. envelope demod: {len(out)} frames, 0 bad, KLM1023 + alt"
      f" 38000 ft + DF11 recovered")

# --- 10. demod edge cases: block straddle, re-decode, noise, phase ---
# frame split across two decode_batch calls (the tail carries it, the
# absolute counter must keep it from decoding twice)
env = ppm_modulate(hex_to_bits(frames[0]))
cut = 40
b1 = 0.05 + 0.02 * rng.rand(SDR_RATE)
b2 = 0.05 + 0.02 * rng.rand(SDR_RATE)
b1[len(b1) - cut:] += env[:cut]   # fragment rides the block tail
b2[:len(env) - cut] += env[cut:]
st = new_demod_state()
out = decode_batch(b1, st) + decode_batch(b2, st)
assert len(out) == 1 and out[0]["hex"] == frames[0], out
# the same frame fully inside one batch is NOT decoded again by the next
b3 = 0.05 + 0.02 * rng.rand(SDR_RATE)
b3[5000:5000 + len(env)] += env
st = new_demod_state()
assert len(decode_batch(b3, st)) == 1
assert decode_batch(0.05 + 0.02 * rng.rand(SDR_RATE), st) == []
# sub-sample phase error: pulses shifted 0.3 us against the grid
b4 = 0.05 + 0.02 * rng.rand(SDR_RATE)
env = ppm_modulate(hex_to_bits(frames[1]), offset_us=0.3)
b4[1000:1000 + len(env)] += env
st = new_demod_state()
out = decode_batch(b4, st)
assert len(out) == 1 and out[0]["hex"] == frames[1], out
print("10. demod edge cases: straddle + no re-decode + noise + phase: ok")

# --- helpers for the synthetic-aircraft sections (11, 12, 20) ---
def encode_cpr(lat, lon, odd):
    """lat/lon -> (latcpr, loncpr) airborne CPR (test encoder, the
    inverse of decode_cpr_local's zone arithmetic)."""
    dlat = 360.0 / (4 * 15 - (1 if odd else 0))
    latcpr = int((lat / dlat - np.floor(lat / dlat)) * 131072)
    nl = adsb._nl(lat)
    dlon = 360.0 / max(nl - (1 if odd else 0), 1)
    loncpr = int((lon / dlon - np.floor(lon / dlon)) * 131072)
    return latcpr, loncpr


def build_df17(icao, tc, alt_ft, latcpr, loncpr, odd):
    """A valid-CRC DF17 airborne-position frame (hex) for a synthetic
    aircraft — the full parse_frame -> handle_message path with a
    position the live-table gate accepts."""
    me = [0] * 56
    for i in range(5):
        me[i] = (tc >> (4 - i)) & 1
    n = int(round((alt_ft + 1000) / 25.0))    # Q-bit altitude encoding
    alt12 = ((n >> 4) << 5) | 0x10 | (n & 0xF)
    for i in range(12):
        me[8 + i] = (alt12 >> (11 - i)) & 1
    me[21] = 1 if odd else 0
    for i in range(17):
        me[22 + i] = (latcpr >> (16 - i)) & 1
        me[39 + i] = (loncpr >> (16 - i)) & 1
    payload = ([1, 0, 0, 0, 1] + [1, 0, 1]          # DF17, CA=5
               + [(icao >> i) & 1 for i in range(23, -1, -1)] + me)
    pi = crc24(payload + [0] * 24)
    return bits_to_hex(payload + [(pi >> i) & 1 for i in range(23, -1, -1)])


# --- 11. live table updates: pair -> position, callsign merge ---
# A synthetic pair at a receivable distance (~45 km) as real valid-CRC
# frames through the full parse -> merge path. (The book's example
# aircraft of the old version sits ~680 km out — beyond the 600 km
# live-table gate of section 20; its frames stay in sections 4/5 for
# the CPR math and in section 12 for the gate.)
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
for odd in (False, True):
    latcpr, loncpr = encode_cpr(48.80, 11.90, odd)
    hx = build_df17(0x40621D, 11, 38000, latcpr, loncpr, odd)
    d = parse_frame(hex_to_bits(hx))
    d["hex"] = hx
    adsb.handle_message(d, None)
ac = state.adsb_aircrafts["40621D"]
assert ac["alt_ft"] == 38000 and abs(ac["lat"] - 48.80) < 1e-3 \
    and abs(ac["lon"] - 11.90) < 1e-3, ac
assert len(state.adsb_aircrafts_all["40621D"]["recent"]) == 2
print("11. aircraft table: pair decoded to position", (ac["lat"], ac["lon"]))

# --- 12. first-fix seeding from the RECEIVER reference ---
# The book's example aircraft (52.257N 3.919E) sits ~680 km away, past
# the local-decode validity window: a single squitter can only seed a
# zone phantom INSIDE the 180 NM window (a single message cannot do
# better). Its pair decodes globally to the true fix — but 680 km is
# beyond ADSB_COVERAGE_MAX_KM (600), so under the live-table
# plausibility gate the far pair is a bad decode, not a fix: never the
# far position, never anything beyond the gate. A pair that decodes
# INSIDE the gate still wins over a seeded phantom.
from noaa_receiver.config import LAT as REFLAT, LON as REFLON
from noaa_receiver.config import ADSB_COVERAGE_MAX_KM
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
d = parse_frame(hex_to_bits(ODD))
d["hex"] = ODD
adsb.handle_message(d, None)
ac = state.adsb_aircrafts["40621D"]
assert ac.get("lat") is not None, "the seed must land inside the window"
assert adsb._dist_km(ac["lat"], ac["lon"], REFLAT, REFLON) <= 333.0, ac
d = parse_frame(hex_to_bits(EVEN))
d["hex"] = EVEN
adsb.handle_message(d, None)
assert abs(ac.get("lat", 0) - 52.257202) > 1.0, ac
if ac.get("lat") is not None:
    assert adsb._haversine_km(REFLAT, REFLON, ac["lat"], ac["lon"]) \
        <= ADSB_COVERAGE_MAX_KM, ac

# A pair that decodes INSIDE the gate still wins over a seed: an
# aircraft at ~455 km (52.50N 15.50E) — past the 180 NM seed window,
# so its first squitter seeds a zone phantom — is corrected by its
# pair.
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
for odd in (False, True):
    latcpr, loncpr = encode_cpr(52.50, 15.50, odd)
    adsb.handle_message({"icao": "0A00A1", "df": 17, "tc": 11,
                         "alt_ft": 38000,
                         "cpr": (latcpr, loncpr, odd, False)}, None)
ac = state.adsb_aircrafts["0A00A1"]
assert abs(ac["lat"] - 52.50) < 1e-3 and abs(ac["lon"] - 15.50) < 1e-3, ac

# A synthetic aircraft right over Bavaria (48.50N 11.50E, ~78 km): its
# FIRST position squitter must seed the correct fix directly
TEST_ICAO = "0A0001"
latcpr, loncpr = encode_cpr(48.50, 11.50, False)
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
adsb.handle_message({"icao": TEST_ICAO, "df": 17, "tc": 11, "alt_ft": 12500,
                     "cpr": (latcpr, loncpr, False, False)}, None)
ac = state.adsb_aircrafts[TEST_ICAO]
assert ac.get("lat") is not None, "receiver-reference decode must fire"
assert abs(ac["lat"] - 48.50) < 0.01 and abs(ac["lon"] - 11.50) < 0.01, ac
print("12. receiver-reference first fix (48.50, 11.50), mid-range pair"
      " corrects a seed, far pair gated: ok")

# --- 13. path history from the persistent log (/adsb_tracks.json) ---
tracks = adsb.aircraft_tracks([TEST_ICAO, "40621D"], 24)
assert len(tracks[TEST_ICAO]) == 1, tracks
assert abs(tracks[TEST_ICAO][0]["lat"] - 48.50) < 0.01
assert tracks[TEST_ICAO][0]["alt_ft"] == 12500
assert len(tracks["40621D"]) >= 2   # section 11 logged the pair's fixes
assert any(abs(p["lat"] - 48.80) < 1e-3 for p in tracks["40621D"]), tracks
assert adsb.aircraft_tracks([], 24) == {}
print("13. path history from the message log:", len(tracks[TEST_ICAO]),
      "point for", TEST_ICAO)

# --- 14. registry window: only aircraft heard in the last 24 h ---
# (the adsb_aircrafts table keeps every aircraft ever; the page shows
# the ADSB_REGISTRY_WINDOW_SECS tail of it)
import time  # noqa: E402
state.adsb_aircrafts_all.clear()
state.adsb_aircrafts_all["0A00F1"] = {"icao": "0A00F1", "msgs": 3,
                                     "last_seen": time.time() - 25 * 3600}
state.adsb_aircrafts_all["0A00F2"] = {"icao": "0A00F2", "msgs": 5,
                                     "last_seen": time.time() - 3600}
state.adsb_aircrafts_all["0A00F3"] = {"icao": "0A00F3", "msgs": 1,
                                     "last_seen": time.time()}
reg = adsb.aircrafts_registry()
assert [a["icao"] for a in reg] == ["0A00F3", "0A00F2"], reg
print("14. registry window: stale aircraft filtered, recent kept: ok")

# --- 15. stream-rate watchdog: wrong-rate streams are flagged ---
# nominal 2.4 Msps passes; the wedged-dongle rates measured in the
# field (0.82 / 1.44 Msps, 2026-10-19) are rejected
ok, msps = adsb._rate_check(2 * SDR_RATE * 6, 6.0)
assert ok and abs(msps - SDR_RATE / 1e6) < 0.01, (ok, msps)
ok, msps = adsb._rate_check(2 * 0.8202e6 * 6, 6.0)
assert not ok and abs(msps - 0.8202) < 0.01, (ok, msps)
ok, msps = adsb._rate_check(2 * 1.4432e6 * 6, 6.0)
assert not ok and abs(msps - 1.4432) < 0.01, (ok, msps)
ok, msps = adsb._rate_check(2 * SDR_RATE * 6, 6.0 * 1.1)   # 10% slow: ok
assert ok, (ok, msps)
ok, msps = adsb._rate_check(2 * SDR_RATE * 6, 6.0 * 2.0)  # half rate: bad
assert not ok, (ok, msps)
print("15. stream-rate watchdog: nominal ok, 0.82/1.44 Msps flagged: ok")
# a flaky-but-decoding dongle is kept connected; a wrong-rate silent one
# is reconnected
assert adsb._rate_action(False, True) == "keep"
assert adsb._rate_action(False, False) == "reconnect"
assert adsb._rate_action(True, False) == "keep"
print("    rate action: keep while decoding, reconnect when silent: ok")

# --- 16. field preamble: P2 nulled by tuner ripple + inter-pulse ring ---
# Measured on the v5 + ADS-B antenna (2026-10-19): the 2.5 us pulse
# rides a filter-ripple null down to dust while P0/P1/P3 stay strong,
# and the positions between pulses ring to ~half pulse level. The
# old per-pulse absolute pattern rejected every such frame.
HX = "8D4840D6202CC371C32CE0576098"
env = ppm_modulate(hex_to_bits(HX))
env = env.copy()
env[6:8] *= 0.15          # P2 (2.5 us) -> filter-ripple null
env[4] = max(env[4], 0.5 * env[2])    # ring between P1 and P2
env[10] = max(env[10], 0.4 * env[9])  # ring after P3
b = 0.05 + 0.02 * rng.rand(SDR_RATE)
b[3000:3000 + len(env)] += env
st = new_demod_state()
out = decode_batch(b, st)
assert len(out) == 1 and out[0]["hex"] == HX, out
assert st["bad"] == 0, st
print("16. field preamble (weak P2 + ringed gaps) still decodes: ok")

# --- 17. bare frames update presence but are not logged ---
# DF0/4/5 surveillance replies and DF11 all-calls carry no callsign,
# altitude, position, direction or velocity: they keep the live table
# (presence, counters) but stay out of the message log, the raw feed
# and the registry's recent list.
state.adsb_raw.clear()
bare11 = parse_frame(hex_to_bits(DF11_HEX))
bare11["hex"] = DF11_HEX
adsb.handle_message(bare11, demod_st=None)
assert bare11["icao"] in state.adsb_aircrafts, "presence must update"
assert state.adsb_aircrafts[bare11["icao"]]["msgs"] == 1
assert DF11_HEX not in state.adsb_raw, "bare frame must not hit the raw feed"
assert not state.adsb_aircrafts_all[bare11["icao"]]["recent"], \
    state.adsb_aircrafts_all[bare11["icao"]]
cs = parse_frame(hex_to_bits(frames[0]))       # identification: callsign
cs["hex"] = frames[0]
adsb.handle_message(cs, demod_st=None)
assert frames[0] in state.adsb_raw
assert state.adsb_aircrafts_all[cs["icao"]]["recent"], "payload frame logged"
assert state.adsb_aircrafts_all[cs["icao"]]["recent"][-1]["callsign"] == "KLM1023"
print("17. bare frames (no payload) not logged, presence kept: ok")

# --- 18. coverage hull: all-time registry basis + bogus-position gate ---
# The hull must come from the adsb_aircrafts registry (every aircraft
# ever), not the rolling adsb_messages log, and must reject positions
# beyond ADSB_COVERAGE_MAX_KM of the station (bad global CPR decodes
# measured live at 67.5N/9.6W and 31N/31.8E, 2026-10-20).
import json  # noqa: E402
import time  # noqa: E402
from noaa_receiver import db  # noqa: E402

now = time.time()
registry = {
    "3C1234": {"icao": "3C1234", "lat": 48.50, "lon": 11.60, "last_seen": now},
    "4CA2B0": {"icao": "4CA2B0", "lat": 49.80, "lon": 13.60, "last_seen": now},
    "3C6666": {"icao": "3C6666", "lat": 49.20, "lon": 12.60, "last_seen": now},
    "4CA111": {"icao": "4CA111", "lat": None, "lon": None, "last_seen": now},
    "DEAD01": {"icao": "DEAD01", "lat": 67.55, "lon": -9.56, "last_seen": now},
    "DEAD02": {"icao": "DEAD02", "lat": 31.13, "lon": 31.81, "last_seen": now},
}
with db.write() as cur:
    for a in registry.values():
        cur.execute("INSERT OR REPLACE INTO adsb_aircrafts VALUES (?,?,?)",
                    (a["icao"], json.dumps(a), now))
hull = adsb.coverage_hull()
assert hull["points"] == 3, hull            # only the 3 plausible positions
assert len(hull["hull"]) == 3, hull        # triangle around the station
for la, lo in hull["hull"]:
    assert 48 < la < 50 and 11 < lo < 14, hull
print("18. coverage hull: registry-based, bogus positions gated: ok")

# --- 19. frame skips: a real frame right behind a short reply decodes ---
# The skip after an accepted short reply (DF11, 64 us) must be the
# SHORT span (~156 samples), not the long one (~290): with the long
# skip every frame starting within 57 us after an all-call was
# silently dropped (all-calls are most of the ~35 frames/s).
b = 0.05 + 0.02 * rng.rand(SDR_RATE)
e11 = ppm_modulate(hex_to_bits(DF11_HEX))
ecs = ppm_modulate(hex_to_bits(frames[0]))
b[1000:1000 + len(e11)] += e11
off2 = 1000 + int(64 * SDR_RATE / 1e6) + 60    # 60 samples behind the DF11
b[off2:off2 + len(ecs)] += ecs
st = new_demod_state()
out = decode_batch(b, st)
got = {d["hex"] for d in out}
assert DF11_HEX in got and frames[0] in got, sorted(got)
# a rejected (noise) candidate skips only the short span: a real
# frame ~160 samples behind it still decodes (the old long skip ate
# everything within 290 samples)
b = 0.05 + 0.02 * rng.rand(SDR_RATE)
b[500] += 0.9                    # interference burst: pattern-passing
b[508] += 0.9                    # candidate (onset + dip + "P3"), but
b[509] += 0.9                    # the slice fails CRC at every phase
b[660:660 + len(ecs)] += ecs
st = new_demod_state()
out = decode_batch(b, st)
assert frames[0] in {d["hex"] for d in out}, out
assert st["bad"] >= 1, st
print("19. frame skips: DF11 + following DF17 both decode; noise-"
      "candidate window halved: ok")

# --- 20. live-table plausibility gate: garbage CPR pairs are bad decodes ---
# A garbage global pair decodes anywhere on the globe (measured live
# 2026-10-20: 67.5N/9.6W and 31N/31.8E, ~2400 km out) — beyond
# ADSB_COVERAGE_MAX_KM of the station it is a bad decode, not a fix,
# and must not enter the live table (review 2026-10-21; the coverage
# hull already gated it).
from noaa_receiver.config import LAT, LON, ADSB_COVERAGE_MAX_KM  # noqa: E402
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
GARBAGE = "0BAD01"
for odd in (False, True):
    latcpr, loncpr = encode_cpr(67.55, -9.56, odd)
    adsb.handle_message({"icao": GARBAGE, "df": 17, "tc": 11,
                         "alt_ft": 38000,
                         "cpr": (latcpr, loncpr, odd, False)}, None)
ac = state.adsb_aircrafts[GARBAGE]
assert ac.get("lat") is None or abs(ac["lat"] - 67.55) > 1.0, ac
if ac.get("lat") is not None:
    d = adsb._haversine_km(LAT, LON, ac["lat"], ac["lon"])
    assert d <= ADSB_COVERAGE_MAX_KM, (ac, d)
assert ac["msgs"] == 2, ac       # presence + counters still merge
# a plausible pair still lands
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
NEAR = "0A0002"
for odd in (False, True):
    latcpr, loncpr = encode_cpr(48.50, 11.50, odd)
    adsb.handle_message({"icao": NEAR, "df": 17, "tc": 11,
                         "alt_ft": 12500,
                         "cpr": (latcpr, loncpr, odd, False)}, None)
ac = state.adsb_aircrafts[NEAR]
assert abs(ac["lat"] - 48.50) < 0.01 and abs(ac["lon"] - 11.50) < 0.01, ac
print("20. live-table CPR gate: garbage pair gated at"
      f" {ADSB_COVERAGE_MAX_KM} km, plausible pair lands: ok")

# --- 21. registry load tolerates a corrupt row; a failed load never
# wipes the table (review 2026-10-21) ---
now = time.time()
with db.write() as cur:
    cur.execute("INSERT OR REPLACE INTO adsb_aircrafts VALUES (?,?,?)",
                ("3C1234", json.dumps({"icao": "3C1234", "lat": 48.5,
                                      "lon": 11.6, "last_seen": now}), now))
    cur.execute("INSERT OR REPLACE INTO adsb_aircrafts VALUES (?,?,?)",
                ("BADCOR", "{not json", now))
adsb.load_aircrafts()
assert "3C1234" in state.adsb_aircrafts_all, \
    "a corrupt sibling row must not abort the load"
# a TOTAL load failure leaves the wipe guard off: the next save must
# not delete every aircraft ever
real_query = db.query
def _db_down(*a, **k):
    raise RuntimeError("db down")
db.query = _db_down
adsb.load_aircrafts()
db.query = real_query
assert state.adsb_aircrafts_all == {}
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
d = parse_frame(hex_to_bits(frames[0]))
d["hex"] = frames[0]
adsb.handle_message(d, None)          # a new aircraft arrives
adsb.save_aircrafts()
rows = {r["icao"] for r in db.query("SELECT icao FROM adsb_aircrafts")}
assert "3C1234" in rows, "a failed load must not wipe the registry table"
assert "4840D6" in rows, rows
print("21. registry load per-row tolerance + wipe guard: ok")

print("\nAll ADS-B tests passed.")
