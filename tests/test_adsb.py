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

# --- 11. live table updates: pair -> position, callsign merge ---
state.adsb_aircrafts.clear()
state.adsb_aircrafts_all.clear()
for hexstr in (ODD, EVEN):
    d = parse_frame(hex_to_bits(hexstr))
    d["hex"] = hexstr
    adsb.handle_message(d, None)
ac = state.adsb_aircrafts["40621D"]
assert ac["alt_ft"] == 38000 and abs(ac["lat"] - 52.257202) < 1e-5 \
    and abs(ac["lon"] - 3.919373) < 1e-5, ac
assert len(state.adsb_aircrafts_all["40621D"]["recent"]) == 2
print("11. aircraft table: pair decoded to position", (ac["lat"], ac["lon"]))

print("\nAll ADS-B tests passed.")
