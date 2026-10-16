"""ADS-B receiver: aircraft squitters on 1090 MHz (Mode S extended squitter).

The dongle switched to the ADS-B role (dashboard role dropdown, like
AIS/APRS) tunes to ADSB_CENTER_HZ and decodes Mode S downlink frames
straight from the 2.4 MHz capture:

Radio layer (ICAO Annex 10 Vol IV; conventions cross-checked against
Junzi Sun's "The 1090 MHz Riddle" worked examples — see
tests/test_adsb.py for the full vector set):

- PPM at 1 Mbit/s: each bit is 1 us; logic 1 = 0.5 us pulse in the
  FIRST half of the bit, logic 0 = pulse in the second half. A 4-pulse
  preamble (pulses starting at 0, 1.0, 2.5, 3.5 us) precedes the 56
  (short) or 112 (long) data bits. At 2.4 Msps each half-bit holds 1-2
  samples — enough to slice (dump1090 runs the same rate).
- CRC-24, generator 1111111111111010000001001 (0x1FFF409 incl. the
  leading x^24 bit), long division over the payload; the 24-bit
  remainder rides in the frame tail. Extended squitters (DF17/18)
  transmit it plain ("PI"); every other downlink format overlays it
  with the transponder ICAO address ("AP"). DF11 all-call replies
  carry the address in their AA field, so the check is syndrome == AA.
  Short surveillance replies (DF0/4/5) have no address in the message:
  the syndrome IS the address — accepted only for addresses already
  known from ADS-B, so noise cannot plant registry entries.
- ADS-B payloads (DF17/18 ME field by type code): identification
  (TC 1-4, 6-bit chars), surface position (TC 5-8, movement + track,
  local CPR only), airborne position (TC 9-18 baro / 20-22 GNSS,
  Q-bit 25 ft altitude) and airborne velocity (TC 19, EW/NS components
  or airspeed).

Compact Position Reporting is decoded globally (odd+even pair within
CPR_MAX_PAIR_SECS) and locally (one message + a reference within
180 NM airborne / 45 NM surface), per the book's equations. Surface
CPR uses four-times smaller zones (90/60 vs 360/60 degrees) and is
inherently ambiguous at 17 bits — the reference picks the
hemisphere/quadrant.

Demodulator: envelope (|IQ|, no frequency rotation needed — PPM is
amplitude keyed and the squitters sit 60 kHz off the dongle's DC
spike) -> noise-floor EMA -> preamble candidate scan -> bit slicing
with max() per half-bit at 3 sample-grid phases (the ADC phase vs the
preamble start is unknown within one sample; the CRC arbitrates) ->
CRC/DF dispatch. Frames do not overlap, so an accepted frame skips
the scan past its end.

The aircraft table, every-aircraft-ever registry and rolling message
log follow the AIS chain (ships): live table in
state.adsb_aircrafts, persistent registry in the adsb_aircrafts table,
rolling per-frame log in adsb_messages.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

from .. import db
from .. import state
from ..calibration import SDR_DONGLE_GAIN, correction_info, tuning_correction
from ..config import (ADSB_AIRCRAFTS_SAVE_SECS, ADSB_AIRCRAFT_TTL_SECS,
                      ADSB_CENTER_HZ, ADSB_LOG_KEEP_HOURS, ADSB_RECENT_MSGS,
                      FFT_SIZE, IQ_BLOCK, LAT, LON, SDR_GAIN, SDR_OFFSET_HZ,
                      SDR_RATE)

# ---------- Mode S framing / CRC primitives ----------

BITS_LONG = 112                     # extended squitter / long replies
BITS_SHORT = 56                     # all-call + short surveillance replies
_CRC_GEN = 0x1FFF409                # 25-bit generator (leading x^24 bit)
CPR_MAX_PAIR_SECS = 10.0            # odd/even pair validity window
_ADSB_DFS = (17, 18)                # extended squitter

assert SDR_RATE >= 2_000_000, "ADS-B PPM demod needs >= 2 samples per us"
SPS = SDR_RATE / 1e6                # samples per microsecond (2.4 at 2.4 Msps)


def crc24(bits):
    """Mode S CRC-24 remainder over MSB-first bits (poly 0x1FFF409).

    Over payload+parity of a valid frame the remainder is 0; over the
    payload alone it is the transmitted parity.
    """
    reg = 0
    for b in bits:
        reg = (reg << 1) | int(b)
        if reg & (1 << 24):
            reg ^= _CRC_GEN
    return reg & 0xFFFFFF


def u(bits, off, n):
    """Unsigned field, MSB first."""
    v = 0
    for b in bits[off:off + n]:
        v = v << 1 | int(b)
    return v


def hex_to_bits(hexstr):
    """A 14/28-char ADS-B hex string -> MSB-first bit list."""
    bs = []
    for ch in hexstr:
        v = int(ch, 16)
        bs.extend((v >> i) & 1 for i in (3, 2, 1, 0))
    return bs


def bits_to_hex(bits):
    """MSB-first bit list -> hex string (zero-padded to whole bytes)."""
    pad = (-len(bits)) % 4
    v = 0
    for b in list(bits) + [0] * pad:
        v = v << 1 | int(b)
    return "%X" % v


def parse_frame(bits):
    """One CRC-valid Mode S frame -> dict (df, icao, ...fields) or None.

    DF17/18 carry full ADS-B payloads (identification / position /
    velocity); DF11 all-call replies contribute the ICAO address and
    capability. DF0/4/5 short replies recover their unknown address
    from the AP syndrome and are flagged known_only — they update
    known aircraft but never create registry entries. Everything else
    fails the CRC dispatch and is dropped.
    """
    df = u(bits, 0, 5)
    if len(bits) == BITS_LONG and df in _ADSB_DFS:
        syndrome = crc24(bits)
        icao = u(bits, 8, 24)
        if df == 17 and syndrome != 0:
            return None
        if df == 18 and syndrome not in (0, icao):
            # DF18 variants transmit either plain PI (syndrome 0) or AP
            # with the address (ADS-B ancillary devices)
            return None
        return parse_adsb(df, icao, bits[32:88])
    if len(bits) == BITS_SHORT and df in (0, 4, 5, 11):
        syndrome = crc24(bits)
        if df == 11:
            aa = u(bits, 8, 24)
            if syndrome != aa:
                return None
            return {"df": 11, "icao": "%06X" % aa, "ca": u(bits, 5, 3)}
        return {"df": df, "icao": "%06X" % syndrome, "known_only": True}
    return None


def parse_adsb(df, icao, me):
    """DF17/18 payload (ME field) -> dict of the fields the aircraft
    table keeps. Returns None for type codes the station does not use."""
    d = {"df": df, "icao": "%06X" % icao}
    tc = u(me, 0, 5)
    d["tc"] = tc
    if 1 <= tc <= 4:                      # identification + category
        d["cat"] = tc * 10 + u(me, 5, 3)   # conventional combined code
        d["callsign"] = _chars(me, 8, 8)
    elif 5 <= tc <= 8:                     # surface position
        d["onground"] = True
        mov = u(me, 5, 7)
        d["speed_kt"] = _movement_speed(mov)
        if me[12]:                        # ground track status
            d["track_deg"] = round(360.0 * u(me, 13, 7) / 128.0, 1)
        d["cpr"] = (u(me, 22, 17), u(me, 39, 17), me[21], True)
    elif 9 <= tc <= 18 or 20 <= tc <= 22:  # airborne position
        d["onground"] = False
        alt = u(me, 8, 12)
        if 20 <= tc <= 22:
            if alt:
                d["alt_m"] = alt           # GNSS height, meters
        else:
            d["alt_ft"] = _altitude_ft(alt)
        d["cpr"] = (u(me, 22, 17), u(me, 39, 17), me[21], False)
    elif tc == 19:                        # airborne velocity
        _velocity(d, me)
    else:
        return None
    return d


# ---------- payload field decoders ----------

_CHARS = {v: chr(64 + v) for v in range(1, 27)}       # 1-26 -> A-Z
_CHARS[32] = " "
for _v in range(48, 58):
    _CHARS[_v] = chr(_v)                              # 48-57 -> 0-9


def _chars(me, off, n):
    """6-bit ADS-B character set -> callsign (padding stripped)."""
    out = "".join(_CHARS.get(u(me, off + 6 * i, 6), "")
                  for i in range(n))
    return out.strip()


def _altitude_ft(alt12):
    """12-bit altitude field -> feet. Q=1: 25 ft increments (N*25-1000);
    Q=0 is the legacy 100 ft Gillham code (rare today) — None."""
    if alt12 == 0:
        return None
    if (alt12 >> 4) & 1:                  # Q bit, 5th LSB
        n = ((alt12 >> 5) << 4) | (alt12 & 0xF)
        return n * 25 - 1000
    return None


def _velocity(d, me):
    """TC 19: ground speed (sub 1/2) or airspeed (sub 3/4)."""
    st = u(me, 5, 3)
    d["vel_st"] = st
    if st in (1, 2):
        scale = 1 if st == 1 else 4
        vew, vns = u(me, 14, 10), u(me, 25, 10)
        if vew and vns:
            vx = (vew - 1) * (-1 if me[13] else 1) * scale
            vy = (vns - 1) * (-1 if me[24] else 1) * scale
            d["speed_kt"] = round(float(np.hypot(vx, vy)), 1)
            d["track_deg"] = round(float(np.degrees(np.arctan2(vx, vy)))
                                   % 360.0, 1)
    elif st in (3, 4):
        if me[13]:                        # magnetic heading status
            d["track_deg"] = round(360.0 * u(me, 14, 10) / 1024.0, 1)
        v = u(me, 25, 10)
        if v:
            d["speed_kt"] = (v - 1) * (1 if st == 3 else 4)
            d["ias"] = not me[24]         # 0 = IAS, 1 = TAS
    vr = u(me, 37, 9)
    if vr:
        d["vs_fpm"] = 64 * (vr - 1) * (-1 if me[36] else 1)
    d["vs_src"] = "baro" if me[35] else "gnss"


def _movement_speed(mov):
    """Surface movement code -> knots (0 = none, 1 = stopped,
    124 = >=175 kt; the table's quantized midpoints)."""
    if mov == 0 or mov >= 125:
        return None
    if mov == 1:
        return 0.0
    if mov == 124:
        return 175.0
    for base, n, step, start in ((2, 7, 0.125, 0.125), (9, 4, 0.25, 1.0),
                                (13, 26, 0.5, 2.0), (39, 55, 1.0, 15.0),
                                (94, 15, 2.0, 70.0), (109, 15, 5.0, 100.0)):
        if base <= mov < base + n:
            return round(start + (mov - base) * step, 3)
    return None


# ---------- compact position reporting ----------

NZ = 15


def _nl(lat):
    """Number of longitude zones at this latitude (1-59)."""
    a = abs(lat)
    if a > 87:
        return 1
    c = max(np.cos(np.radians(a)), 1e-9)
    v = 2 * np.pi / np.arccos(1 - (1 - np.cos(np.pi / (2 * NZ))) / c ** 2)
    return int(min(59, max(1, np.floor(v))))


def _wrap_lon(lon):
    if lon >= 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0
    return lon


def _quadrant(lon, lon_ref):
    """Surface CPR: the 17-bit longitude is ambiguous across four 90
    degree quadrants — pick the candidate nearest the reference."""
    return min((_wrap_lon(lon + k * 90.0) for k in range(4)),
               key=lambda x: abs(x - lon_ref))


def decode_cpr_global(latE, lonE, latO, lonO, odd_newest,
                      surface=False, lat_ref=None, lon_ref=None):
    """Odd+even CPR pair -> (lat, lon), or None (zone mismatch).

    Airborne: unambiguous on its own. Surface: 17-bit truncation leaves
    a hemisphere/quadrant ambiguity — lat_ref/lon_ref pick the solution
    nearest the receiver (or airfield)."""
    dl0 = (90.0 if surface else 360.0) / (4 * NZ)
    dl1 = (90.0 if surface else 360.0) / (4 * NZ - 1)
    j = np.floor(59.0 * latE - 60.0 * latO + 0.5)
    lat_e = dl0 * (j % 60 + latE)
    lat_o = dl1 * (j % 59 + latO)
    if not surface:
        if lat_e >= 270.0:
            lat_e -= 360.0
        if lat_o >= 270.0:
            lat_o -= 360.0
    nl_e, nl_o = _nl(lat_e), _nl(lat_o)
    if nl_e != nl_o:
        return None
    lat = lat_o if odd_newest else lat_e
    nlv = nl_o if odd_newest else nl_e
    m = np.floor(lonE * (nlv - 1) - lonO * nlv + 0.5)
    n_e, n_o = max(nlv, 1), max(nlv - 1, 1)
    span = 90.0 if surface else 360.0
    lon = span / n_o * (m % n_o + lonO) if odd_newest \
        else span / n_e * (m % n_e + lonE)
    if surface:
        if lat_ref is None or lon_ref is None:
            return None
        # latitude: the equations yield the northern solution
        if abs(lat - 90.0 - lat_ref) < abs(lat - lat_ref):
            lat -= 90.0
        lon = _quadrant(lon, lon_ref)
    else:
        lon = _wrap_lon(lon)
    return round(float(lat), 6), round(float(lon), 6)


def decode_cpr_local(latcpr, loncpr, odd, lat_ref, lon_ref, surface=False):
    """One CPR message + a reference within 180 NM (45 NM surface) ->
    (lat, lon). The reference is the aircraft's previous fix or, for
    surface traffic, the receiver itself."""
    i = 1 if odd else 0
    dlat = (90.0 if surface else 360.0) / (4 * NZ - i)
    j = np.floor(lat_ref / dlat) + np.floor(
        np.mod(lat_ref, dlat) / dlat - latcpr + 0.5)
    lat = dlat * (j + latcpr)
    if surface and abs(lat - 90.0 - lat_ref) < abs(lat - lat_ref):
        lat -= 90.0
    dlon = (90.0 if surface else 360.0) / max(_nl(lat) - i, 1)
    m = np.floor(lon_ref / dlon) + np.floor(
        np.mod(lon_ref, dlon) / dlon - loncpr + 0.5)
    lon = dlon * (m + loncpr)
    if surface:
        lon = _quadrant(lon, lon_ref)
    else:
        lon = _wrap_lon(lon)
    return round(float(lat), 6), round(float(lon), 6)


# ---------- aircraft table ----------

def _dist_km(lat1, lon1, lat2, lon2):
    """Equirectangular distance in km (zone-error checks; at the
    latitudes the station decodes the approximation error is small)."""
    mx = np.radians((lat1 + lat2) / 2.0)
    dx = np.radians(lon2 - lon1) * np.cos(mx)
    dy = np.radians(lat2 - lat1)
    return float(np.hypot(dx, dy) * 6371.0)


def handle_message(d, demod_st=None):
    """Merge one decoded frame into the live aircraft table, the
    persistent every-aircraft registry and the message log.

    Called from the capture thread only. known_only frames (DF0/4/5,
    address recovered from the AP syndrome) update the counters but
    touch the tables only for addresses that already exist.
    """
    now = time.time()
    icao = d["icao"]
    hexstr = d.get("hex")
    known_only = d.get("known_only")
    pos = None
    with state.adsb_lock:
        ac = state.adsb_aircrafts.get(icao)
        if known_only and ac is None:
            # surveillance reply for an address never seen via ADS-B:
            # count it, trust nothing else about it
            if demod_st is not None:
                demod_st["frames"] += 1
                demod_st["last_frame"] = now
            return
        if ac is None:
            ac = state.adsb_aircrafts[icao] = {
                "icao": icao, "first_seen": now, "msgs": 0}
        if "cpr" in d:
            latcpr, loncpr, odd, surface = d["cpr"]
            latcpr /= 131072.0
            loncpr /= 131072.0
            cpr = ac.setdefault("cpr", {})
            # Position sources, in order of trust:
            # 1) global decode of an odd+even pair within
            #    CPR_MAX_PAIR_SECS — globally unambiguous, and it
            #    overrides any seeded position below (the pair wins
            #    over everything: a seed can be a zone phantom)
            # 2) local decode from the aircraft's previous fix (the
            #    freshest reference, works from a single message)
            # 3) local decode from the RECEIVER position: the seed for
            #    a first fix. The zone arithmetic is only guaranteed
            #    within 180 NM (45 NM surface) of the reference, so
            #    results farther out are discarded; phantoms inside
            #    that window are unavoidable single-message risks —
            #    the next pair corrects them (measured: the book's
            #    680 km example seeded a phantom, the pair fixed it)
            other = cpr.get(1 - odd)
            if other is not None \
                    and now - other[2] <= CPR_MAX_PAIR_SECS \
                    and other[3] == surface:
                olat, olon = other[0] / 131072.0, other[1] / 131072.0
                pos = decode_cpr_global(
                    olat, olon, latcpr, loncpr, True, surface,
                    LAT, LON) if odd else decode_cpr_global(
                    latcpr, loncpr, olat, olon, False, surface,
                    LAT, LON)
            if pos is None and ac.get("lat") is not None \
                    and now - ac.get("last_pos_seen", 0) \
                    <= ADSB_AIRCRAFT_TTL_SECS:
                pos = decode_cpr_local(latcpr, loncpr, odd, ac["lat"],
                                       ac["lon"], surface)
            if pos is None:
                pos = decode_cpr_local(latcpr, loncpr, odd,
                                       LAT, LON, surface)
                if pos is not None and _dist_km(pos[0], pos[1],
                                                LAT, LON) \
                        > (80.0 if surface else 333.0):
                    pos = None   # outside the local-decode validity window
            cpr[odd] = (d["cpr"][0], d["cpr"][1], now, surface)
        for k, v in d.items():
            if k in ("cpr", "df", "tc", "hex", "allcall", "known_only"):
                continue
            if v is not None:
                ac[k] = v
        if pos:
            ac["lat"], ac["lon"] = pos
            ac["last_pos_seen"] = now
        ac["msgs"] += 1
        ac["last_seen"] = now
        if hexstr:
            state.adsb_raw.append(hexstr)
        # persistent registry: every aircraft ever received, newest data
        # merged over the old, plus the last ADSB_RECENT_MSGS frames
        if not known_only:
            reg = state.adsb_aircrafts_all.setdefault(
                icao, {"icao": icao, "first_seen": now, "msgs": 0,
                       "recent": []})
            for k, v in ac.items():
                if k not in ("cpr", "recent"):
                    reg[k] = v
            reg["recent"].append(_log_entry(d, pos, hexstr, now))
            if len(reg["recent"]) > ADSB_RECENT_MSGS:
                del reg["recent"][:len(reg["recent"]) - ADSB_RECENT_MSGS]
    if demod_st is not None:
        demod_st["frames"] += 1
        demod_st["last_frame"] = now
    log_message(_log_entry(d, pos, hexstr, now))
    _maybe_save_aircrafts()


def _log_entry(d, pos, hexstr, now=None):
    """The jsonl line content for one decoded frame."""
    lat, lon = pos if pos else (None, None)
    e = {
        "ts": round(now if now is not None else time.time(), 2),
        "time": datetime.now().strftime("%H:%M:%S"),
        "df": d.get("df"),
        "tc": d.get("tc"),
        "icao": d["icao"],
        "callsign": d.get("callsign"),
        "lat": lat,
        "lon": lon,
        "alt_ft": d.get("alt_ft"),
        "alt_m": d.get("alt_m"),
        "speed_kt": d.get("speed_kt"),
        "track_deg": d.get("track_deg"),
        "vs_fpm": d.get("vs_fpm"),
        "onground": d.get("onground"),
    }
    if hexstr:
        e["hex"] = hexstr
    return e


# ---------- persistent message log (adsb_messages table) ----------

_log_lock = threading.Lock()
_log_appends = 0


def log_message(entry):
    """Append one decoded frame to the station database; trimmed to the
    last ADSB_LOG_KEEP_HOURS. Database failures never take the capture
    thread down."""
    global _log_appends
    with _log_lock:
        try:
            with db.write() as cur:
                cur.execute("INSERT INTO adsb_messages (ts, entry)"
                            " VALUES (?,?)",
                            (entry.get("ts", time.time()),
                             json.dumps(entry, separators=(",", ":"))))
                _log_appends += 1
                if _log_appends % 256 == 0:
                    cur.execute("DELETE FROM adsb_messages WHERE ts < ?",
                                (time.time() - ADSB_LOG_KEEP_HOURS * 3600,))
        except Exception as e:
            state.log_console(f"ADS-B log write failed: {e}", "warn")


def adsb_log(count=200):
    """Newest-first tail of the persistent message log for /adsblog.json."""
    try:
        count = max(1, min(int(count), 2000))
    except ValueError:
        count = 200
    out = []
    for r in db.query("SELECT entry FROM adsb_messages"
                      " ORDER BY id DESC LIMIT ?", (count,)):
        try:
            out.append(json.loads(r["entry"]))
        except ValueError:
            continue
    return out


def aircraft_tracks(icaos, hours=24):
    """Position track (oldest first) per ICAO, from the persistent
    message log — the rolling frame history is the station's position
    archive, like the AIS ship tracks. Only frames that actually
    decoded a position become track points (with the altitude at that
    moment); ICAOs without any are returned as empty lists."""
    wanted = {str(m).strip().upper() for m in icaos if str(m).strip()}
    if not wanted:
        return {}
    try:
        hours = max(1, min(float(hours), 48))
    except (TypeError, ValueError):
        hours = 24.0
    cutoff = time.time() - hours * 3600
    tracks = {m: [] for m in wanted}
    for r in db.query("SELECT entry FROM adsb_messages WHERE ts >= ?"
                      " ORDER BY id", (cutoff,)):
        try:
            e = json.loads(r["entry"])
        except ValueError:
            continue
        m = e.get("icao")
        if m in tracks and e.get("lat") is not None and e.get("lon") \
                is not None:
            tracks[m].append({"ts": e.get("ts", 0), "lat": e["lat"],
                              "lon": e["lon"], "alt_ft": e.get("alt_ft")})
    return tracks


# ---------- persistent aircraft registry (every aircraft ever seen) ------

_aircrafts_file_lock = threading.Lock()
_aircrafts_save_at = 0.0


def load_aircrafts():
    """Startup: restore the every-aircraft-ever registry."""
    try:
        rows = db.query("SELECT data FROM adsb_aircrafts")
        with state.adsb_lock:
            state.adsb_aircrafts_all = {}
            for r in rows:
                a = json.loads(r["data"])
                if isinstance(a, dict) and "icao" in a:
                    state.adsb_aircrafts_all[a["icao"]] = a
        state.log_console(f"✈️ ADS-B aircraft registry: "
                          f"{len(state.adsb_aircrafts_all)} aircraft ever "
                          f"received, restored from the database")
    except Exception as e:
        state.log_console(f"ADS-B aircraft registry could not be loaded: "
                          f"{e}", "warn")
        with state.adsb_lock:
            state.adsb_aircrafts_all = {}


def save_aircrafts():
    """Write the whole registry to the station database (never fatal)."""
    with state.adsb_lock:
        snapshot = [dict(a, recent=list(a.get("recent", [])))
                    for a in state.adsb_aircrafts_all.values()]
    with _aircrafts_file_lock:
        try:
            with db.write() as cur:
                for a in snapshot:
                    cur.execute("INSERT OR REPLACE INTO adsb_aircrafts"
                                " VALUES (?,?,?)",
                                (a["icao"],
                                 json.dumps(a, separators=(",", ":")),
                                 time.time()))
                icaos = [a["icao"] for a in snapshot]
                if icaos:
                    cur.execute("DELETE FROM adsb_aircrafts WHERE icao"
                                " NOT IN (%s)" % ",".join("?" * len(icaos)),
                                icaos)
        except Exception as e:
            state.log_console(f"ADS-B aircraft registry save failed: {e}",
                              "warn")


def _maybe_save_aircrafts():
    """Throttled save from the capture path (at most one write per
    ADSB_AIRCRAFTS_SAVE_SECS, mirroring the AIS ship registry)."""
    global _aircrafts_save_at
    now = time.time()
    if now < _aircrafts_save_at:
        return
    _aircrafts_save_at = now + ADSB_AIRCRAFTS_SAVE_SECS
    save_aircrafts()


def aircrafts_registry():
    """Snapshot for /adsb_aircrafts.json: every aircraft ever received,
    newest activity first."""
    with state.adsb_lock:
        return sorted(state.adsb_aircrafts_all.values(),
                      key=lambda a: -a.get("last_seen", 0))


def prune_aircrafts():
    """Drop aircraft not heard for ADSB_AIRCRAFT_TTL_SECS."""
    now = time.time()
    with state.adsb_lock:
        for icao in [k for k, a in state.adsb_aircrafts.items()
                     if now - a.get("last_seen", 0)
                     > ADSB_AIRCRAFT_TTL_SECS]:
            del state.adsb_aircrafts[icao]


def adsb_status():
    """Snapshot for /adsb.json: aircraft table, demod stats, raw frames."""
    prune_aircrafts()
    with state.adsb_lock:
        aircraft = sorted(state.adsb_aircrafts.values(),
                          key=lambda a: -a.get("last_seen", 0))
        raw = list(state.adsb_raw)
        stats = dict(state.adsb_stats)
        enabled = state.adsb_enabled
        serial = state.adsb_dongle
    return {"enabled": enabled, "dongle": serial,
            "freq_mhz": round(ADSB_CENTER_HZ / 1e6, 1),
            "stats": stats, "aircraft": aircraft, "raw": raw}


# ---------- envelope demodulator ----------

# Preamble pulses start at 0, 1.0, 2.5, 3.5 us, each 0.5 us wide. At
# SPS samples/us the pulse peaks land inside [off, off+2) for offsets
# (0, 2, 6, 8) — tolerant to +/-0.5 sample of frame-start error. The
# gap samples 4 and 10 are low for every shift in that range (the
# other gaps have no integer sample that is safely outside the pulses).
_PULSE_OFF = (0, 2, 6, 8)
_GAP_OFF = (4, 10)
_SAMPLES_LONG = int(np.ceil((8 + BITS_LONG) * SPS)) + 2
_TAIL = _SAMPLES_LONG   # carry across blocks: a frame can straddle two


def _bit_windows(ref_shift):
    """Per-bit half-window sample index tables, relative to the frame
    start sample, for one candidate grid phase. Bit i's first half
    (logic 1 pulse) covers [(8+i), (8.5+i)) us, second half the rest.
    A sample belongs to a half when its CENTER ((j+0.5)/SPS us) falls
    inside — the boundary-start convention would drop the sample that
    carries most of the pulse."""
    bounds = []
    for i in range(BITS_LONG):
        t0 = (8 + i) * SPS + ref_shift
        t1 = t0 + 0.5 * SPS
        t2 = t0 + SPS
        lo_a = int(np.ceil(t0 - 0.5 - 1e-9))
        hi_a = int(np.ceil(t1 - 0.5 - 1e-9))
        hi_b = int(np.ceil(t2 - 0.5 - 1e-9))
        bounds.append((lo_a, hi_a, hi_b))
    max_a = max(h - l for l, h, _ in bounds)
    max_b = max(h - a for _, a, h in bounds)
    a = np.zeros((BITS_LONG, max_a), dtype=np.int32)
    b = np.zeros((BITS_LONG, max_b), dtype=np.int32)
    ma = np.zeros((BITS_LONG, max_a), dtype=bool)
    mb = np.zeros((BITS_LONG, max_b), dtype=bool)
    for i, (lo_a, hi_a, hi_b) in enumerate(bounds):
        a[i, :hi_a - lo_a] = np.arange(lo_a, hi_a)
        b[i, :hi_b - hi_a] = np.arange(hi_a, hi_b)
        ma[i, :hi_a - lo_a] = True
        mb[i, :hi_b - hi_a] = True
    return a, b, ma, mb


# the candidate (first threshold crossing) may lead or lag the true
# preamble start by up to a sample and a quarter — the CRC
# arbitrates between the quarter-sample grid phases
_BIT_WIN = {s: _bit_windows(s) for s in
            (-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75)}


def new_demod_state():
    """Fresh per-dongle demodulator state."""
    return {
        "tail": np.zeros(0, dtype=np.float64),
        "floor": None,
        "consumed": 0,     # absolute samples fed so far
        "last_end_abs": -1 << 30,   # end of the last processed candidate
        "frames": 0,
        "bad": 0,
        "last_frame": 0.0,
    }


def _preamble_ok(x, p, hi, lo):
    """4-pulse pattern check at frame-start candidate p."""
    if p + _GAP_OFF[-1] + 1 >= len(x):
        return False
    for off in _PULSE_OFF:
        if x[p + off] <= hi and x[p + off + 1] <= hi:
            return False
    for off in _GAP_OFF:
        if x[p + off] >= lo:
            return False
    return True


def _slice_bits(x, p, tables):
    """Envelope window -> 112 data bits (max per half-bit window)."""
    a, b, ma, mb = tables
    wa = np.where(ma, x[p + a], -np.inf)
    wb = np.where(mb, x[p + b], -np.inf)
    return wa.max(axis=1) > wb.max(axis=1)


def decode_batch(mag, st):
    """One capture batch of |IQ| samples -> list of decoded frame dicts.

    Envelope -> noise-floor EMA -> preamble scan (first threshold
    crossing, tried at three half-sample grid phases; the CRC
    arbitrates) -> bit slicing -> CRC/DF dispatch (long frame first,
    short-reply CRC on the same 112-bit slice as fallback). Frames do
    not overlap: an accepted frame skips the scan past its end, and
    the absolute sample counter keeps the block tail (a frame can
    straddle two batches) from being decoded twice.
    """
    mag = np.asarray(mag, dtype=np.float64)
    prev_tail = st["tail"]
    if len(prev_tail):
        x = np.concatenate([prev_tail, mag])
    else:
        x = mag
    base = st["consumed"] - len(prev_tail)   # absolute index of x[0]
    st["consumed"] += len(mag)
    if len(x) <= _TAIL:
        st["tail"] = x
        return []
    st["tail"] = x[-_TAIL:]
    # Noise-PEAK level: the magnitude of |IQ| is Rayleigh-distributed,
    # so a low percentile sits deep inside the noise (a third of all
    # samples crossed the first 2.2 x p20 threshold in the field,
    # 444k bogus candidates in 6 s). The 90th percentile anchors the
    # threshold above the noise peaks: 2 x p90 (~4.3 sigma) is crossed
    # by ~1e-4 of noise samples, and the 4-pulse pattern (4 crossings at
    # exact offsets) makes false preambles vanishingly rare.
    floor = float(np.percentile(x, 90))
    st["floor"] = floor if st["floor"] is None \
        else 0.85 * st["floor"] + 0.15 * floor
    hi = 2.0 * max(st["floor"], 1e-9)
    lo = hi
    out = []
    last_end = st["last_end_abs"]           # absolute skip mark
    for q in np.flatnonzero(x > hi):
        q = int(q)
        q_abs = q + base
        if q_abs <= last_end:
            continue
        if q + _SAMPLES_LONG >= len(x):
            break
        hit = False
        for shift in _BIT_WIN:
            if not _preamble_ok(x, q, hi, lo):
                break     # pattern check is shift-independent: give up
            bits = _slice_bits(x, q, _BIT_WIN[shift])
            d = parse_frame(bits.tolist())
            hexbits = bits
            if d is None:
                # short replies (56 bits) ride the same preamble — try
                # the short CRC on the same slice. known_only replies
                # (DF0/4/5) carry no CRC, so a wrong grid phase or noise
                # slice always "decodes" — only accept them when the
                # ICAO is already in the live table, otherwise the
                # fallback masks the correct phase.
                sd = parse_frame(bits[:BITS_SHORT].tolist())
                if (sd is not None and not sd.get("known_only")) \
                        or (sd is not None
                            and sd["icao"] in state.adsb_aircrafts):
                    d = sd
                    hexbits = bits[:BITS_SHORT]
            if d is not None:
                d["hex"] = bits_to_hex(hexbits.tolist())
                handle_message(d, st)
                out.append(d)
                hit = True
                last_end = q_abs + _SAMPLES_LONG
                break
        if not hit:
            st["bad"] += 1
            last_end = q_abs + 4   # skip a little past the noise blip
    st["last_end_abs"] = last_end
    return out


# ---------- capture thread ----------

def adsb_capture_thread(did):
    """rtl_tcp/rtl_sdr connection + envelope decoding for a dongle
    switched to the ADS-B role. Mirrors the POCSAG capture thread's
    connection lifecycle (handshake, gain, retune on connect, reconnect
    with backoff, role handover)."""
    from ..sdr.dsp import iq_to_complex
    from ..sdr.radio import (_IQReader, _PipeRecv, push_wf_row,
                             _kill_rtl_sdr, _rtl_tcp_connect, _spawn_rtl_sdr,
                             CAPTURE_BATCH,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]

    def reception_off():
        others = [d for d, e in state.sdrs.items()
                  if d != did and e.get('adsb')
                  and not e.get('closed') and not e.get('paused')]
        with state.adsb_lock:
            if others:
                state.adsb_enabled = True
                state.adsb_dongle = others[0]
                return
            state.adsb_enabled = False
            state.adsb_dongle = None

    with state.adsb_lock:
        state.adsb_enabled = True
        state.adsb_dongle = did
        state.adsb_stats = {}
    st = new_demod_state()
    state.log_console(f"✈️ ADS-B receiver on dongle {did}: "
                      f"{ADSB_CENTER_HZ / 1e6:.1f} MHz — decoding Mode S "
                      f"extended squitters")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("adsb") or entry.get("paused"):
            reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            correction = tuning_correction(ADSB_CENTER_HZ, did)
            tune = ADSB_CENTER_HZ + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = \
                correction_info(ADSB_CENTER_HZ, did)
            gain_db = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
            if entry.get("usb_idx") is not None:
                proc = _spawn_rtl_sdr(entry, tune, gain_db)
                if proc is None:
                    raise RuntimeError("rtl_sdr not available on this host")
                entry["proc"] = proc
                entry["connected"] = True
                entry["tuner"] = "USB (rtl_sdr)"
                entry["last_data"] = time.time()
                state.log_console(f"Connected to ADS-B USB dongle {did} "
                                  f"(device {entry['usb_idx']}), tuned "
                                  f"{tune}Hz")
                reader = _IQReader(_PipeRecv(proc.stdout), IQ_BLOCK)
            else:
                sock, tuner_type, _gain_count = _rtl_tcp_connect(
                    entry["host"], entry["port"], gain_db)
                _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, tune)
                entry["sock"] = sock
                entry["connected"] = True
                entry["tuner"] = TUNER_NAMES.get(
                    tuner_type, f"rtl_tcp type {tuner_type}")
                entry["last_data"] = time.time()
                state.log_console(f"Connected to ADS-B rtl_tcp {did}, "
                                  f"tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, "
                                  f"correction {correction:+d}Hz)")
                reader = _IQReader(sock, IQ_BLOCK)
            sig_bins = max(1, round(39375 / (SDR_RATE / FFT_SIZE)))
            while True:
                if entry.get("closed") or not entry.get("adsb"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (ADS-B dongle "
                                      f"{did}), reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                c = iq_to_complex(raw)
                # waterfall + signal for the dongle card (see push_wf_row);
                # every 4th row keeps the 1090 card light
                for k in range(0, len(c) // FFT_SIZE, 4):
                    try:
                        push_wf_row(entry, c, k, sig_bins)
                    except Exception:
                        pass
                # envelope: PPM is amplitude-keyed, no rotation needed —
                # the squitters sit 60 kHz off the dongle's DC spike
                decode_batch(np.abs(c), st)
                with state.adsb_lock:
                    state.adsb_stats = {"frames": st["frames"],
                                        "bad": st["bad"],
                                        "last_frame": st["last_frame"],
                                        "floor": round(st["floor"] or 0.0, 2)}
        except Exception as e:
            if not entry.get("closed") and entry.get("adsb") \
                    and not entry.get("paused"):
                state.log_console(f"ADS-B thread error (dongle {did}): {e}",
                                  "error")
        entry["connected"] = False
        entry["sock"] = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill_rtl_sdr(entry.pop("proc", None))
        if entry.get("closed") or not entry.get("adsb") or entry.get("paused"):
            reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)


# ---------- test/simulation helpers (used by tests/test_adsb.py) ----------

def ppm_modulate(bits, fs=SDR_RATE, amp=0.9, sigma_us=0.15, offset_us=0.0):
    """Test signal generator: Mode S PPM envelope (preamble + data) as
    a real magnitude array at fs samples/s. Pulses are gaussian-shaped
    (sigma_us) — sharper than any real dongle's channel filter.
    offset_us shifts every pulse in time (sub-sample grid phase)."""
    n = int(np.ceil((8 + len(bits)) / 1e6 * fs)) + 8
    env = np.zeros(n)
    step_us = 1e6 / fs                      # one sample per step
    on = np.arange(-3 * sigma_us, 3 * sigma_us + step_us, step_us)
    pulse = np.exp(-0.5 * (on / sigma_us) ** 2)

    def add_pulse(t_us):
        p0 = int(round((t_us + offset_us) * fs / 1e6))
        env[p0:p0 + len(pulse)] += pulse

    for t in (0.0, 1.0, 2.5, 3.5):
        add_pulse(t)
    for i, b in enumerate(bits):
        add_pulse(8.0 + i + (0.0 if b else 0.5))
    return amp * env
