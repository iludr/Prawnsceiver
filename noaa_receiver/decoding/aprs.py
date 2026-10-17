"""APRS receiver: amateur packet radio on the 144.800 MHz European
channel.

Bell 202 AFSK (1200 Hz mark / 2200 Hz space) at 1200 baud on narrowband
FM — NRZI data bits, HDLC framing with the same X.25 FCS as AIS (the
flag 0x7E, bit stuffing and CRC helpers are shared with ais.py), AX.25
UI frames carrying APRS text.

The chain mirrors the AIS role (dedicated dongle, offset-tuned capture,
FM discriminator at 48 kHz, energy-gated bursts) with two differences:

- the tone pair is separated by two complex-exponential correlate-and-
  dump filters (one per tone, evaluated over one-bit windows at every
  symbol phase - 40 samples/bit at 48 kHz, exactly the AIS phase-trial
  idea; the FCS picks the right phase) instead of frequency slicing -
  the discriminator output IS the audio sine, not the tone value
- the channel FIR is evaluated polyphase (see orbcomm._demod_channel_
  batch): only the 48 kHz output positions are computed, ~1 ms per
  13.7 ms batch instead of a full-rate convolution

Payload: destination/source callsigns + SSIDs, digipeater chain,
control/PID (UI frames only) and the info text. Positions are parsed
from uncompressed beacons (`!`/`=` data types); Mic-E frames (mobile
radios packing lat/lon/speed/course into the destination field and the
first info bytes) are decoded as the
position fallback - tables ported from aprslib (aprs-python),
including position ambiguity and the A-J message bits. Text
messages (`:ADDRESSEE:text`, the same format as the telemetry
definitions) feed the /aprsmsgs.json message view.

Telemetry (APRS spec): T# beacons (sequence + 5 analog channels +
8 digital bits) and the PARM/UNIT/EQNS/BITS definition messages are
decoded into the all-time registry entry - channels scaled by the
EQNS quadratic (value = a*raw^2 + b*raw + c), names/units from
PARM/UNIT - so DB0BMW-style "T#099,188,..." frames read as
"Batt. 14.1 Volt". The definitions are rare (sent every ~30 min),
so they persist with the registry.

Live data: state.aprs_stations (callsign -> newest info), the newest
packets in state.aprs_packets, every frame appended to the aprs_messages
table (trimmed to APRS_LOG_KEEP_HOURS), and the persistent every-
station-ever registry in state.aprs_stations_all (aprs_stations table,
mirroring the AIS ship registry - see load_stations/save_stations).
"""
import json
import re
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

from .. import db
from .. import state
from ..sdr.dsp import frequency_shift, sinc_taps
from ..calibration import SDR_DONGLE_GAIN, correction_info, tuning_correction
from ..config import (APRS_FREQ_HZ, APRS_LOG_KEEP_HOURS, APRS_RECENT_MSGS,
                      APRS_STATIONS_SAVE_SECS, ISS_APRS_FREQ_HZ,
                      LASAR_FREQ_HZ,
                      FFT_SIZE, IQ_BLOCK, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE)
from .ais import (FLAG, FCS_RESIDUE, _find_flags, crc16_register,
                  destuff_bits, fcs_bits, payload_to_transmitted,
                  stuff_bits)

APRS_BITRATE = 1200                    # Bell 202 symbols/s
APRS_MARK_HZ, APRS_SPACE_HZ = 1200.0, 2200.0
APRS_DEC = SDR_RATE // 48000           # 2.4 MHz -> 48 kHz
APRS_FS = SDR_RATE // APRS_DEC
SYM_SAMP = APRS_FS // APRS_BITRATE    # 40 samples per symbol, exact
assert SDR_RATE % 48000 == 0 and APRS_FS % APRS_BITRATE == 0

# AX.25 frame bounds (bits between flags, stuffed): up to 8 addresses
# x 7 + control + PID + 256 info bytes + 16 FCS, plus worst-case ~25%
# bit stuffing. ais.hdlc_frames caps at 1200 (AIS-sized) - too small.
_AX25_MAX_BITS = 3400

_BLK_S = (IQ_BLOCK // 2) / SDR_RATE   # 512-sample block, seconds
# Burst assembly runs at capture-batch granularity (13.65 ms of RF per
# batch - a burst shorter than three batches could not hold the ~18
# byte minimum AX.25 frame at 1200 baud anyway)
APRS_MIN_BURST_BATCHES = 3            # >= ~40 ms
APRS_MAX_BURST_BATCHES = 220          # 3 s cap: longest legal frame is ~2.2 s
APRS_WF_BLOCKS = max(8, round(4.27e-3 / _BLK_S))   # one waterfall row per ~4 ms
_SIG_BINS = max(1, round(39375 / (SDR_RATE / FFT_SIZE)))  # +/-39 kHz band center

# ---------- AX.25 payload -> frame -> text ----------

_POS_RE = re.compile(r"^[!=]([0-9]{4}\.[0-9]{2})([NS])(.)([0-9]{5}\.[0-9]{2})([EW])(.)")


def _payload_bytes(p):
    """Payload vector (MSB-first per byte, hdlc_frames order) -> bytes."""
    b = np.asarray(p[:len(p) // 8 * 8], dtype=np.int64).reshape(-1, 8)
    return (b * (1 << np.arange(7, -1, -1))).sum(axis=1).astype(np.uint8).tobytes()


def _callsign(raw6):
    """Six AX.25 address bytes (chars <<1) -> callsign, or None."""
    s = bytes((c >> 1) & 0x7F for c in raw6).decode("ascii", "replace").strip()
    if not s or any(not (32 <= ord(ch) < 127) for ch in s):
        return None
    return s


def _ssid(byte):
    """Callsign-SSID byte -> 'CALL-7' style text (SSID 0 = bare call)."""
    return (byte >> 1) & 0x0F


def parse_ax25(p):
    """Payload vector -> AX.25 UI frame dict, or None.

    APRS rides unnumbered information frames only (control 0x03, PID
    0xF0); the FCS has already validated the bits in hdlc_frames, so
    the checks here just separate APRS from other AX.25 traffic."""
    b = _payload_bytes(p)
    if len(b) < 16:
        return None
    # address field: 7 bytes per address, the last one has bit 0 set
    n_addr = 0
    for j in range(6, len(b), 7):
        n_addr += 1
        if b[j] & 1:
            break
        if n_addr >= 8:
            return None
    if 7 * n_addr + 2 > len(b):
        return None
    dst, src = _callsign(b[0:6]), _callsign(b[7:13])
    if not dst or not src:
        return None
    def _fmt(call, ssid):
        return f"{call}-{ssid}" if ssid else call
    digis = []
    for i in range(2, n_addr):
        call = _callsign(b[7 * i:7 * i + 6])
        if call:
            digis.append(_fmt(call, _ssid(b[7 * i + 6])))
    if b[7 * n_addr] != 0x03 or b[7 * n_addr + 1] != 0xF0:
        return None     # not a UI frame - no APRS in it
    info = b[7 * n_addr + 2:].decode("ascii", "replace")
    if not info:
        return None
    return {"src": _fmt(src, _ssid(b[13])), "dst": _fmt(dst, _ssid(b[6])),
            "digis": digis, "info": info}


def parse_position(info):
    """Uncompressed APRS position (`!`/`=` data types) ->
    (lat, lon, symbol), or None."""
    m = _POS_RE.match(info)
    if m is None:
        return None
    lat = int(m.group(1)[:2]) + float(m.group(1)[2:]) / 60.0
    lon = int(m.group(4)[:3]) + float(m.group(4)[3:]) / 60.0
    if m.group(2) == "S":
        lat = -lat
    if m.group(5) == "W":
        lon = -lon
    return lat, lon, m.group(3) + m.group(6)


# ---------- telemetry (APRS spec, chapter 13) ----------

def parse_telemetry(info):
    """`T#sss,aaa,bbb,ccc,ddd,eee,dddddddd` -> {seq, analog, bits},
    or None if the info field is not a telemetry data frame:
    sequence number, five analog channels (raw 0-255), eight
    digital bits (0/1 each)."""
    if not info.startswith("T#"):
        return None
    parts = info[2:].split(",")
    if len(parts) < 2:
        return None
    try:
        seq = int(parts[0])
        analog = [int(v) for v in parts[1:6]]
    except ValueError:
        return None
    bits = (parts[6] if len(parts) > 6 else "").ljust(8, "0")[:8]
    return {"seq": seq, "analog": analog, "bits": bits}

_TELEM_DEF_KINDS = ("PARM", "UNIT", "EQNS", "BITS")

def parse_telemetry_definition(info):
    """Telemetry definition message (an AX.25 message addressed to
    the station whose telemetry it describes):
    `:CALLSIGN  :PARM.Batt,Temp,...` / `UNIT.` / `EQNS.a,b,c,...` /
    `BITS.11111111,label` -> (call, kind, payload), or None for any
    other message. The addressee is exactly 9 chars, padded."""
    if not info.startswith(":") or len(info) < 11 or info[10] != ":":
        return None
    addressee = info[1:10].strip()
    if not addressee:
        return None
    text = info[11:]
    for kind in _TELEM_DEF_KINDS:
        if text.startswith(kind + "."):
            return addressee, kind, text[len(kind) + 1:]
    return None

def parse_message(info):
    """APRS text message `:ADDRESSEE:text` -> {to, text}, or None.
    The addressee is exactly 9 chars, padded (the same layout as the
    telemetry definition messages)."""
    if not info.startswith(":") or len(info) < 11 or info[10] != ":":
        return None
    return {"to": info[1:10].strip(), "text": info[11:]}


# ---------- Mic-E (position packed into the destination field) ----
# Tables ported from aprslib (aprs-python, aprslib/parsing/mice.py):
# the destination carries latitude + N/S / +100-deg / E-W flags in
# dual-use char forms (digits = plain, P-Y = digits + flag, A-J =
# digits + message bits, K/L/Z = position-ambiguity space), the
# info field starts with 6 packed bytes (longitude, speed, course),
# then symbol code + symbol table + free-text comment.

_MICE_DST_RE = re.compile(r"^[0-9A-Z]{3}[0-9L-Z]{3}$")
_MICE_BODY_RE = re.compile(
    r"^[&-\x7f][&-a][\x1c-\x7f]{2}[\x1c-\x7d][\x1c-\x7f][\x21-\x7e]"
    r"[/\\0-9A-Z]")
_MTYPE_STD = {
    "111": "M0: Off Duty", "110": "M1: En Route",
    "101": "M2: In Service", "100": "M3: Returning",
    "011": "M4: Committed", "010": "M5: Special",
    "001": "M6: Priority", "000": "Emergency",
}
_MTYPE_CUSTOM = {
    "111": "C0: Custom-0", "110": "C1: Custom-1",
    "101": "C2: Custom-2", "100": "C3: Custom-3",
    "011": "C4: Custom-4", "010": "C5: Custom-5",
    "001": "C6: Custom-6", "000": "Emergency",
}

def parse_mice(dst, info):
    """Mic-E destination field + info text -> {lat, lon, sym,
    spd (km/h), cse, msg, comment}, or None if not Mic-E. Tried
    only when no uncompressed position was found, so the strict
    format regexes keep regular beacons and tocalls out."""
    if len(dst) != 6 or len(info) < 8:
        return None
    if not _MICE_DST_RE.match(dst) or not _MICE_BODY_RE.match(info):
        return None
    # latitude digits: K/L/Z -> space (position ambiguity),
    # P-Y -> digits, A-J -> digits
    t = ""
    for ch in dst:
        if ch in "KLZ":
            t += " "
        elif ord(ch) > 76:
            t += chr(ord(ch) - 32)
        elif ord(ch) > 57:
            t += chr(ord(ch) - 17)
        else:
            t += ch
    m = re.match(r"^(\d+)( *)$", t)
    if m is None or not t[0:2].isdigit():
        return None
    posamb = len(m.group(2))
    tl = list(t)
    if posamb >= 4:
        tl[2] = "3"             # center of the ambiguity box
    elif posamb > 0:
        tl[6 - posamb] = "5"
    t = "".join(tl)
    lat = int(t[0:2]) + float((t[2:4] + "." + t[4:6]).replace(" ", "0")) / 60.0
    if ord(dst[3]) <= 0x4C:    # dest byte 4 carries N/S
        lat = -lat
    # plain digits mean "no message" - an all-digit destination
    # must NOT read as the 000/Emergency row)
    msg = None
    if any(c > "9" for c in dst[0:3]):
        mb = re.sub(r"[0-9L]", "0", dst[0:3])
        mb = re.sub(r"[P-Z]", "1", mb)
        mb = re.sub(r"[A-K]", "2", mb)
        if re.match(r"^[012]+$", mb):
            if "2" in mb:
                msg = _MTYPE_CUSTOM[mb.replace("2", "1")]
            else:
                msg = _MTYPE_STD[mb]
    # longitude: degrees + 100 offset flag, minutes with the
    # >= 60 wrap, hundredths
    lon = ord(info[0]) - 28
    if ord(dst[4]) >= 0x50:    # dest byte 5 carries +100 deg
        lon += 100
    if 180 <= lon <= 189:
        lon -= 80
    elif 190 <= lon <= 199:
        lon -= 190
    lm = ord(info[1]) - 28.0
    if lm >= 60:
        lm -= 60
    lm += (ord(info[2]) - 28.0) / 100.0
    if posamb == 4:
        lm = 30.0
    elif posamb == 3:
        lm = (int(lm / 10) + 0.5) * 10
    elif posamb == 2:
        lm = int(lm) + 0.5
    elif posamb == 1:
        lm = (int(lm * 10) + 0.5) / 10.0
    lon += lm / 60.0
    if ord(dst[5]) >= 0x50:    # dest byte 6 carries E/W
        lon = -lon
    # speed (knots, +800 wrap) and course (+400 wrap): the low
    # speed digit rides in the course byte
    spd = (ord(info[3]) - 28) * 10
    cse = ord(info[4]) - 28
    q = int(cse / 10.0)
    cse = (cse - q * 10) * 100 + ord(info[5]) - 28
    spd += q
    if spd >= 800:
        spd -= 800
    if cse >= 400:
        cse -= 400
    return {"lat": lat, "lon": lon, "sym": info[7] + info[6],
            "spd": round(spd * 1.852, 1), "cse": cse, "msg": msg,
            "comment": info[8:].strip(" ") or None}


# ---------- AFSK demodulation ----------

# 610 taps at 2.4 MHz: -3 dB ~12 kHz, stopband by ~20 kHz. Bell 202 on
# FM occupies 2*(deviation + tone) ~ 2*(3 + 2.2) = 10.4 kHz (Carson),
# so 12 kHz keeps the whole signal while staying inside the 25 kHz
# channel raster; the offset DC spike (60 kHz) is far outside.
_APRS_TAPS = sinc_taps(12000, 610)
_APRS_TAPS_C = _APRS_TAPS.astype(np.complex64)   # polyphase matmul form

def new_channel_state(did, lasar=False):
    """Fresh DSP state for the one APRS channel of one dongle. lasar
    selects the 18 kHz GFSK-9600 channel filter (LASARsat digipeater)
    over the 12 kHz Bell 202 one."""
    taps = _LASAR_TAPS if lasar else _APRS_TAPS
    return {
        "dongle": did, "lasar": bool(lasar),
        "rot": 0,                        # offset rotator counter
        "fir_tail": np.zeros(len(taps) - 1, dtype=np.complex64),
        "last_c": None,                  # discriminator previous sample
        "dec_pos": 0,                    # 48 kHz decimation phase
        "floor": None,                   # noise floor EMA
        "burst": [], "burst_open": False,
        "frames": 0, "bad": 0, "last_frame": 0.0,
    }

def demod_channel_batch(c, st):
    """One offset-rotated IQ batch -> (48 kHz FM audio, batch RMS).

    Channel filter + discriminator, evaluated polyphase (see
    orbcomm._demod_channel_batch): the FIR is computed only at the
    decimated output positions, keeping one channel around ~1 ms per
    13.7 ms batch (the full-rate FFT convolution costs ~13 ms and
    would not leave room for anything else on this CPU)."""
    taps_c = _LASAR_TAPS_C if st.get("lasar") else _APRS_TAPS_C
    ntap = taps_c.shape[0]
    x = np.concatenate([st["fir_tail"], c])
    st["fir_tail"] = x[-(ntap - 1):].copy()
    m = len(x) - ntap + 1
    if m <= 0:
        return np.empty(0, dtype=np.float32), 0.0
    start = (-st["dec_pos"]) % APRS_DEC
    st["dec_pos"] = (st["dec_pos"] + m) % APRS_DEC
    f = (np.lib.stride_tricks.sliding_window_view(x, ntap)
         [start::APRS_DEC] @ taps_c)
    energy = float(np.sqrt(np.mean(np.abs(f) ** 2))) if len(f) else 0.0
    prev = st["last_c"]
    if prev is None:
        prev = f[0]
    dd = np.empty(len(f), dtype=np.complex64)
    dd[0] = f[0] * np.conj(prev)
    np.multiply(f[1:], np.conj(f[:-1]), out=dd[1:])
    st["last_c"] = f[-1].copy()
    freq = np.arctan2(dd.imag, dd.real) * (APRS_FS / (2 * np.pi))
    return freq, energy

def collect_bursts(freq48, energy, st,
                    max_batches=APRS_MAX_BURST_BATCHES,
                    min_batches=None):
    """Noise-floor tracking + burst assembly at batch granularity
    (see ais.collect_bursts - the same EMA floor, the same one-quiet-
    block close, with batch counts instead of 512-sample blocks). The
    minimum defaults by mode: 3 batches (~40 ms) for 1200-baud Bell 202
    bursts, 2 for the 9600-baud GFSK ones (a single LASARsat frame can
    be over in ~30 ms)."""
    if min_batches is None:
        min_batches = 2 if st.get("lasar") else APRS_MIN_BURST_BATCHES
    if st["floor"] is None:
        st["floor"] = energy
    active = energy > 1.5 * st["floor"]
    if not active:   # EMA tracks the quiet floor, never the bursts
        st["floor"] += 0.05 * (energy - st["floor"])
        active = energy > 1.5 * st["floor"]
    closed = []
    if active:
        st["burst"].append(freq48)
        st["burst_open"] = True
        if len(st["burst"]) > max_batches:   # noise stuck high: cut it
            closed.append(np.concatenate(st["burst"]))
            st["burst"] = []
    elif st["burst_open"]:
        if len(st["burst"]) >= min_batches:
            closed.append(np.concatenate(st["burst"]))
        st["burst"] = []
        st["burst_open"] = False
    return closed

def ax25_frames(data_bits):
    """NRZI-decoded data bits -> valid AX.25 payload vectors.

    Same contract as ais.hdlc_frames (flag-delimited, destuffed,
    X.25-FCS-validated), with the AX.25 length bound instead of the
    AIS one (see _AX25_MAX_BITS)."""
    frames, seen = [], set()
    flags = _find_flags(data_bits)
    for a, b in zip(flags, flags[1:]):
        raw = data_bits[a + 8:b]
        if not 40 <= len(raw) <= _AX25_MAX_BITS:
            continue
        content = destuff_bits(raw)
        if content is None or len(content) < 40 or len(content) % 8:
            continue
        if crc16_register(content) != FCS_RESIDUE:
            continue
        payload = tuple(payload_to_transmitted(content[:-16]))
        if payload not in seen:
            seen.add(payload)
            frames.append(list(payload))
    return frames


# Complex exponentials of the two tones over one symbol window: the
# correlate-and-dump tone filters. Hann-weighted (adjacent-channel
# and DC rejection; the flat window leaks DC into the 2200 Hz arm
# because 2200/48000 * 40 is not an integer number of cycles).
_TONE_NCO = (np.exp(-2j * np.pi
                    * np.outer(np.arange(SYM_SAMP) / APRS_FS,
                               np.array([APRS_MARK_HZ, APRS_SPACE_HZ])))
             * np.hanning(SYM_SAMP)[:, None]).astype(np.complex64)

def decode_burst(freq48):
    """48 kHz FM-audio samples of one burst -> valid AX.25 payload
    vectors (ax25_frames order). One correlate-and-dump per tone at
    every symbol phase; the X.25 FCS picks the correct phase - no
    clock recovery state machine, exactly the AIS trial scheme."""
    x = np.asarray(freq48, dtype=np.float64)
    if len(x) < 3 * SYM_SAMP:
        return []
    x = x - np.median(x)
    w = np.lib.stride_tricks.sliding_window_view(x, SYM_SAMP)
    out, seen = [], set()
    for ph in range(SYM_SAMP):
        corr = np.abs(w[ph::SYM_SAMP] @ _TONE_NCO)   # (K, 2) energies
        if len(corr) < 2:
            continue
        space = corr[:, 1] > corr[:, 0]              # True = 2200 Hz
        data = np.zeros(len(space) - 1, dtype=np.int8)
        data[space[1:] == space[:-1]] = 1           # NRZI: 1 = unchanged
        for p in ax25_frames(data.tolist()):
            key = tuple(p)
            if key not in seen:
                seen.add(key)
                out.append(list(p))
    return out

# ---------- GFSK 9600 (LASARsat digipeater) ----------

# LASARsat relays APRS on 436.925 MHz as GFSK 9600 bps AX.25 with G3RUH
# scrambling (the standard 9k6 satellite framing: the scrambler keeps
# the NRZ bit stream transition-dense, so 9600 baud fits an FM channel).
# The discriminator output IS the NRZ data (no tone pair like Bell 202):
# 1 = frequency above the burst median (Doppler and detector offsets
# shift the mean; the median removes it), 5 samples/symbol at 48 kHz.
LASAR_BITRATE = 9600
LASAR_SAMP = APRS_FS // LASAR_BITRATE   # 5 samples per symbol, exact
assert APRS_FS % LASAR_BITRATE == 0

# 9600 GFSK at +/-4.8 kHz deviation occupies ~2*(4.8+4.8) kHz (Carson);
# an 18 kHz low-pass keeps it inside the +/-24 kHz Nyquist pair of the
# 48 kHz decimated channel (the 12 kHz AFSK taps would clip it).
_LASAR_TAPS = sinc_taps(18000, 610)
_LASAR_TAPS_C = _LASAR_TAPS.astype(np.complex64)

def scramble_g3ruh(bits):
    """G3RUH transmit-side scrambler: out[i] = in[i] ^ out[i-17] ^
    out[i-12] (x^17 + x^12 + 1). Used by the test chain (the station
    only receives); the demodulator runs the inverse."""
    out = []
    for bit in bits:
        v = bit & 1
        if len(out) >= 17:
            v ^= out[-17]
        if len(out) >= 12:
            v ^= out[-12]
        out.append(v)
    return out

def descramble_g3ruh(bits):
    """G3RUH receive-side descrambler: data[i] = rx[i] ^ rx[i-17] ^
    rx[i-12] over the RECEIVED stream - self-synchronizing (the state
    is just the last 17 received bits), so it works from any point in
    the burst. Vectorized: one XOR per tap over the whole burst."""
    b = np.asarray(bits, dtype=np.int8)
    out = b.copy()
    for t in (17, 12):
        if len(b) > t:
            out[t:] ^= b[:-t]
    return out.tolist()

def decode_burst_gfsk96(freq48):
    """48 kHz FM-audio samples of one burst -> valid AX.25 payload
    vectors, GFSK 9600 G3RUH. One slicer trial per symbol phase (5):
    slice, descramble, let the X.25 FCS pick the phase - the same trial
    scheme as the AFSK correlate-and-dump, minus the tone filters
    (the discriminator output is already the NRZ data)."""
    x = np.asarray(freq48, dtype=np.float64)
    if len(x) < 3 * LASAR_SAMP:
        return []
    x = x - np.median(x)
    out, seen = [], set()
    for ph in range(LASAR_SAMP):
        bits = (x[ph::LASAR_SAMP] > 0).astype(np.int8).tolist()
        for p in ax25_frames(descramble_g3ruh(bits)):
            key = tuple(p)
            if key not in seen:
                seen.add(key)
                out.append(list(p))
    return out

# ---------- decoded frames -> registry, log, marks ----------

def _new_station_reg(call, now):
    """Fresh all-time-registry entry for a callsign."""
    return {"call": call, "first_seen": round(now, 2), "msgs": 0,
            "lat": None, "lon": None, "sym": None, "recent": []}

def _retel_channels(t):
    """Recompute the display-ready telemetry channels: five analog
    channels scaled by the EQNS quadratic (value = a*raw^2 + b*raw
    + c; defaults a=0, b=1, c=0 = raw) with names/units from
    PARM/UNIT, plus the eight digital bits named by PARM items 6+."""
    parm = t.get("parm") or []
    unit = t.get("unit") or []
    eqns = t.get("eqns") or [(0.0, 1.0, 0.0)] * 5
    raw = t.get("raw") or []
    chans = []
    for i in range(5):
        name = parm[i] if i < len(parm) and parm[i] else "A%d" % (i + 1)
        u = unit[i] if i < len(unit) else ""
        r = raw[i] if i < len(raw) else None
        if r is None:
            chans.append({"name": name, "raw": None, "value": None,
                           "unit": u})
            continue
        a, b, c = eqns[i] if i < len(eqns) else (0.0, 1.0, 0.0)
        value = a * r * r + b * r + c
        chans.append({"name": name, "raw": r, "value": round(value, 3),
                       "unit": u})
    t["channels"] = chans
    bits = []
    bits_raw = t.get("bits_raw") or ""
    for i in range(8):
        name = (parm[5 + i] if 5 + i < len(parm) and parm[5 + i]
                else "B%d" % (i + 1))
        value = int(bits_raw[i]) if i < len(bits_raw) and bits_raw[i] in "01" else None
        bits.append({"name": name, "value": value})
    t["bits"] = bits

def _apply_telemetry(reg, tlm, entry):
    """Merge one decoded T# frame into the station's registry entry,
    recomputing the scaled channels from the current definitions."""
    t = reg.setdefault("telemetry", {})
    t["seq"] = tlm["seq"]
    t["ts"] = entry["ts"]
    t["time"] = entry["time"]
    t["raw"] = tlm["analog"]
    t["bits_raw"] = tlm["bits"]
    _retel_channels(t)

def _apply_telemetry_definition(reg, tdef):
    """Merge one PARM/UNIT/EQNS/BITS message into the described
    station's registry entry (created as a stub if the station
    itself has not been heard yet)."""
    kind, payload = tdef[1], tdef[2]
    t = reg.setdefault("telemetry", {})
    if kind == "PARM":
        t["parm"] = [p.strip() for p in payload.split(",")]
    elif kind == "UNIT":
        t["unit"] = [u.strip() for u in payload.split(",")]
    elif kind == "EQNS":
        try:
            nums = [float(x) for x in payload.split(",")]
        except ValueError:
            return
        eqns = []
        for i in range(5):
            j = 3 * i
            eqns.append(tuple(nums[j:j + 3]) if len(nums) >= j + 3
                         else (0.0, 1.0, 0.0))
        t["eqns"] = eqns
    elif kind == "BITS":
        parts = payload.split(",", 1)
        t["bits_sense"] = parts[0][:8]
        if len(parts) > 1:
            t["label"] = parts[1][:40]
    _retel_channels(t)

def _display_entry(d, now):
    text = d.get("comment")          # Mic-E: decoded comment,
    if text is None:                 # not the packed data bytes
        text = d["info"][:120]
    return {
        "call": d["src"], "dst": d["dst"],
        "via": ",".join(d["digis"]),
        "type": (d["info"][0] if 32 <= ord(d["info"][0]) < 127 else "?"),
        "text": text[:120],
        "lat": d.get("lat"), "lon": d.get("lon"), "sym": d.get("sym"),
        "spd": d.get("spd"), "cse": d.get("cse"),
        "msgs": 1, "last_seen": round(now, 2),
    }

def handle_frames(payloads, st, now=None):
    """Parse payload vectors, update the station registry and the
    packet ring, append every frame to the persistent log, and mark
    the burst on the dongle's waterfall."""
    now = now if now is not None else time.time()
    de = state.sdrs.get(st.get("dongle") or "")
    for p in payloads:
        d = parse_ax25(p)
        if d is None:
            st["bad"] += 1
            continue
        st["frames"] += 1
        st["last_frame"] = now
        pos = parse_position(d["info"])
        if pos:
            d["lat"], d["lon"], d["sym"] = pos
        else:
            mice = parse_mice(d["dst"].split("-")[0], d["info"])
            if mice is not None:
                d["lat"] = mice["lat"]
                d["lon"] = mice["lon"]
                d["sym"] = mice["sym"]
                d["spd"] = mice["spd"]
                d["cse"] = mice["cse"]
                d["comment"] = mice["comment"] or ""
                if mice["msg"]:
                    d["comment"] = "[%s] %s" % (mice["msg"], d["comment"])
        entry = _display_entry(d, now)
        entry["ts"] = round(now, 2)
        entry["time"] = datetime.now().strftime("%H:%M:%S")
        tlm = parse_telemetry(d["info"])
        tdef = None if tlm is not None else parse_telemetry_definition(d["info"])
        _log_message(entry)
        with state.aprs_lock:
            prev = state.aprs_stations.get(d["src"])
            if prev:
                prev["msgs"] += 1
                prev.update({k: v for k, v in entry.items()
                             if k not in ("ts", "time", "msgs")})
            else:
                state.aprs_stations[d["src"]] = entry
            state.aprs_packets.append(entry)
            # persistent registry: every station ever received,
            # newest data merged over the old (position only from
            # frames that carry one), plus the last APRS_RECENT_MSGS
            # frames
            reg = state.aprs_stations_all.setdefault(
                d["src"], _new_station_reg(d["src"], now))
            reg["msgs"] += 1
            reg["last_seen"] = entry["last_seen"]
            reg.update({k: entry[k] for k in ("dst", "via", "type", "text")})
            if entry.get("lat") is not None:
                reg["lat"] = entry["lat"]
                reg["lon"] = entry["lon"]
                reg["sym"] = entry["sym"]
                if entry.get("spd") is not None:
                    reg["spd"] = entry["spd"]
                    reg["cse"] = entry["cse"]
            reg["recent"].append({k: entry[k] for k in
                                  ("ts", "time", "dst", "via",
                                   "text", "lat", "lon", "sym")})
            if len(reg["recent"]) > APRS_RECENT_MSGS:
                del reg["recent"][:len(reg["recent"]) - APRS_RECENT_MSGS]
            if tlm is not None:
                _apply_telemetry(reg, tlm, entry)
            elif tdef is not None:
                dreg = state.aprs_stations_all.setdefault(
                    tdef[0], _new_station_reg(tdef[0], now))
                _apply_telemetry_definition(dreg, tdef)
        state.log_console(f"📡 APRS {d['src']}>{d['dst']}"
                          + (f" via {entry['via']} " if d["digis"] else ": ")
                          + d["info"][:80])
        if de is not None:
            with de["lock"]:
                de.setdefault("wf_marks", deque(maxlen=64)).append(0.0)
        _maybe_save_stations()

_log_lock = threading.Lock()
_log_appends = 0

def _trim_log():
    cutoff = time.time() - APRS_LOG_KEEP_HOURS * 3600
    try:
        with db.write() as cur:
            cur.execute("DELETE FROM aprs_messages WHERE ts < ?", (cutoff,))
    except Exception:
        pass

def _log_message(entry):
    """Append one decoded frame to the station database; never fatal."""
    global _log_appends
    with _log_lock:
        try:
            with db.write() as cur:
                cur.execute("INSERT INTO aprs_messages (ts, entry) VALUES (?,?)",
                            (entry.get("ts", time.time()),
                             json.dumps(entry, separators=(",", ":"))))
            _log_appends += 1
            if _log_appends % 256 == 0:
                _trim_log()
        except Exception as e:
            state.log_console(f"APRS log write failed: {e}", "warn")

def aprs_log(count=200):
    """Newest-first tail of the persistent frame log for /aprslog.json."""
    try:
        count = max(1, min(int(count), 2000))
    except ValueError:
        count = 200
    out = []
    for r in db.query("SELECT entry FROM aprs_messages"
                      " ORDER BY id DESC LIMIT ?", (count,)):
        try:
            out.append(json.loads(r["entry"]))
        except ValueError:
            continue
    return out

_MSG_ACK_RE = re.compile(r"^(ack|rej)[0-9]{1,5}$")

def messages_view(count=200):
    """Newest-first tail of the person-to-person text messages from
    the persistent frame log, for /aprsmsgs.json: ':ADDRESSEE:text'
    frames minus the telemetry definition keywords (T# data frames
    never match - their type is not ':'). ack/rej confirmations are
    flagged instead of shown as text."""
    try:
        count = max(1, min(int(count), 500))
    except ValueError:
        count = 200
    out = []
    for e in aprs_log(2000):
        if e.get("type") != ":":
            continue
        m = parse_message(e.get("text") or "")
        if m is None:
            continue
        text = m["text"]
        if any(text.startswith(k + ".") for k in _TELEM_DEF_KINDS):
            continue
        ack = _MSG_ACK_RE.match(text)
        out.append({"ts": e.get("ts"), "time": e.get("time"),
                    "from": e.get("call"), "to": m["to"],
                    "text": text[:200],
                    "ack": ack.group(1) if ack else None})
        if len(out) >= count:
            break
    return out

# ---------- persistent station registry (every station ever received) ----

_stations_file_lock = threading.Lock()
_stations_save_at = 0.0

def load_stations():
    """Read the registry at startup: restores every station ever
    received (with its frame history) from the aprs_stations
    table. Telemetry frames in the rolling aprs_messages log are
    replayed afterwards, so decoded channels survive a restart
    even when the registry has no telemetry saved yet."""
    try:
        rows = db.query("SELECT data FROM aprs_stations")
        with state.aprs_lock:
            state.aprs_stations_all = {}
            for r in rows:
                s = json.loads(r["data"])
                if isinstance(s, dict) and "call" in s:
                    state.aprs_stations_all[s["call"]] = s
        state.log_console(f"📡 APRS station registry: {len(state.aprs_stations_all)}"
                          f" stations ever received, restored from the database")
    except Exception as e:
        state.log_console(f"APRS station registry could not be loaded: {e}",
                          "warn")
        with state.aprs_lock:
            state.aprs_stations_all = {}
    _replay_telemetry_log()

def _replay_telemetry_log():
    """Startup backfill: replay the rolling aprs_messages log through
    the telemetry parsers, oldest frame first (newest T# data and
    the last definitions win), merging into the registry entries.
    Frames decoded before a restart or before a registry save would
    otherwise be lost from the telemetry view until the station
    beaconed again - DB0BMW sends its PARM/UNIT/EQNS/BITS cycle
    rarely. Saved once at the end so the backfill survives the
    next restart even if the channel stays quiet."""
    try:
        rows = db.query("SELECT entry FROM aprs_messages ORDER BY id")
    except Exception as e:
        state.log_console(f"APRS telemetry replay failed: {e}", "warn")
        return
    applied = 0
    for r in rows:
        try:
            e = json.loads(r["entry"])
        except ValueError:
            continue
        info = e.get("text")
        if not info:
            continue
        tlm = parse_telemetry(info)
        tdef = None if tlm is not None else parse_telemetry_definition(info)
        if tlm is None and tdef is None:
            continue
        call = e["call"] if tlm is not None else tdef[0]
        with state.aprs_lock:
            reg = state.aprs_stations_all.setdefault(
                call, _new_station_reg(call, e.get("ts") or time.time()))
            if tlm is not None:
                _apply_telemetry(reg, tlm, e)
            else:
                _apply_telemetry_definition(reg, tdef)
        applied += 1
    if applied:
        state.log_console(f"\U0001f4e1 APRS telemetry: {applied} frames replayed from the persistent log")
        save_stations()

def save_stations():
    """Write the whole registry to the station database (aprs_stations
    table). Never fatal: on an error the receiver keeps running with
    the in-memory registry and warns once per save attempt."""
    with state.aprs_lock:
        snapshot = [dict(s, recent=list(s.get("recent", [])))
                    for s in state.aprs_stations_all.values()]
    with _stations_file_lock:
        try:
            now = time.time()
            with db.write() as cur:
                for s in snapshot:
                    cur.execute("INSERT OR REPLACE INTO aprs_stations VALUES (?,?,?)",
                                (s["call"], json.dumps(s, separators=(",", ":")), now))
                calls = [s["call"] for s in snapshot]
                if calls:
                    cur.execute("DELETE FROM aprs_stations WHERE call NOT IN (%s)"
                                % ",".join("?" * len(calls)), calls)
        except Exception as e:
            state.log_console(f"APRS station registry save failed: {e}", "warn")

def _maybe_save_stations():
    """Throttled save from the capture path: at most one write per
    APRS_STATIONS_SAVE_SECS, so a restart loses at most that window
    (the frames themselves are already safe in the rolling log)."""
    global _stations_save_at
    now = time.time()
    if now < _stations_save_at:
        return
    _stations_save_at = now + APRS_STATIONS_SAVE_SECS
    save_stations()

def stations_registry():
    """Snapshot for /aprs_stations.json: every station ever received,
    newest activity first."""
    with state.aprs_lock:
        return sorted(state.aprs_stations_all.values(),
                      key=lambda s: -s.get("last_seen", 0))

def aprs_status():
    """Snapshot for /aprs.json: registry, stats, recent packets."""
    with state.aprs_lock:
        stations = sorted(state.aprs_stations.values(),
                          key=lambda s: -s.get("last_seen", 0))
        packets = list(state.aprs_packets)
        enabled = state.aprs_enabled
        serial = state.aprs_dongle
        stats = dict(state.aprs_stats)
        freq_hz = state.aprs_freq_hz
    return {"enabled": enabled, "dongle": serial,
            "freq_mhz": round(freq_hz / 1e6, 3) if freq_hz else None,
            "stats": stats, "stations": stations, "packets": packets}

def load_log_summary():
    """Startup line for the console (mirrors the AIS/Orbcomm summary)."""
    try:
        n = db.query("SELECT COUNT(*) AS n FROM aprs_messages")[0]["n"]
        state.log_console(f"📡 APRS frame log: {n} frames on record (last "
                          f"{APRS_LOG_KEEP_HOURS} h)")
    except Exception:
        pass

# ---------- capture thread ----------

def aprs_capture_thread(did):
    """rtl_tcp/rtl_sdr connection + demod for a dongle switched to the
    APRS role. Mirrors the AIS capture thread's connection lifecycle
    (handshake, gain, offset tune, reconnect with backoff); one fixed
    channel instead of two, and no retune (the band never moves). The
    channel is 144.800 MHz (European APRS simplex) or, when the dongle's
    aprs_iss flag is set (dashboard checkbox), 145.825 MHz — the ISS
    (Zarya) digipeater; the flag is read once at thread start, a flip
    restarts the thread (radio.set_dongle_aprs_iss)."""
    from ..sdr.dsp import iq_to_complex
    from ..sdr.radio import (_IQReader, _PipeRecv, push_wf_row,
                             _kill_rtl_sdr, _rtl_tcp_connect, _spawn_rtl_sdr,
                             CAPTURE_BATCH, IQ_BLOCK,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]
    # The channel this thread listens to: the LASARsat digipeater
    # checkbox (aprs_lasar) selects 436.925 MHz GFSK 9600, the ISS
    # digipeater checkbox (aprs_iss) 145.825 MHz, over the 144.800 MHz
    # simplex channel; read once here, a flip restarts the thread
    lasar = bool(entry.get("aprs_lasar"))
    freq_hz = (LASAR_FREQ_HZ if lasar
               else ISS_APRS_FREQ_HZ if entry.get("aprs_iss")
               else APRS_FREQ_HZ)
    # move the channel onto 0 Hz: the dongle is tuned SDR_OFFSET_HZ
    # above the channel, so the signal arrives at -SDR_OFFSET_HZ
    # (see ais._ais_shifts)
    shift = int(SDR_OFFSET_HZ)
    st = new_channel_state(did, lasar=lasar)

    def aprs_reception_off():
        others = [d for d, e in state.sdrs.items()
                  if d != did and e.get("aprs")
                  and not e.get("closed") and not e.get("paused")]
        with state.aprs_lock:
            if others:
                state.aprs_enabled = True
                state.aprs_dongle = others[0]
                return
            state.aprs_enabled = False
            state.aprs_dongle = None
            state.aprs_freq_hz = 0

    with state.aprs_lock:
        state.aprs_enabled = True
        state.aprs_dongle = did
        state.aprs_freq_hz = freq_hz
    state.log_console(f"📡 APRS receiver on dongle {did}: "
                      f"{freq_hz / 1e6:.3f} MHz "
                      f"({'LASARsat digipeater, AX.25 G3RUH' if lasar
                         else 'ISS digipeater' if entry.get('aprs_iss')
                         else 'DL simplex channel'}, "
                      f"{'GFSK' if lasar else 'Bell 202 AFSK'} "
                      f"{LASAR_BITRATE if lasar else APRS_BITRATE} baud)")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("aprs") or entry.get("paused"):
            aprs_reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            correction = tuning_correction(freq_hz, did)
            tune = freq_hz + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = correction_info(freq_hz, did)
            gain_db = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
            if entry.get("usb_idx") is not None:
                proc = _spawn_rtl_sdr(entry, tune, gain_db)
                if proc is None:
                    raise RuntimeError("rtl_sdr not available on this host")
                entry["proc"] = proc
                entry["connected"] = True
                entry["tuner"] = "USB (rtl_sdr)"
                entry["last_data"] = time.time()
                state.log_console(f"Connected to APRS USB dongle {did} "
                                  f"(device {entry['usb_idx']}, rtl_sdr), tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, correction {correction:+d}Hz)")
                reader = _IQReader(_PipeRecv(proc.stdout), IQ_BLOCK)
            else:
                sock, tuner_type, _gain_count = _rtl_tcp_connect(entry["host"], entry["port"], gain_db)
                _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, tune)
                entry["sock"] = sock
                entry["connected"] = True
                entry["tuner"] = TUNER_NAMES.get(tuner_type, f"rtl_tcp type {tuner_type}")
                entry["last_data"] = time.time()
                state.log_console(f"Connected to APRS rtl_tcp {did}, tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, correction {correction:+d}Hz)")
                reader = _IQReader(sock, IQ_BLOCK)
            while True:
                if entry.get("closed") or not entry.get("aprs"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (APRS dongle {did}), reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                c = iq_to_complex(raw)
                # Waterfall + signal for the dongle card (center label
                # reads 144.800 MHz - see push_wf_row)
                for k in range(0, len(c) // FFT_SIZE, APRS_WF_BLOCKS):
                    try:
                        push_wf_row(entry, c, k, _SIG_BINS)
                    except Exception:
                        pass
                cc = frequency_shift(c, shift, SDR_RATE, st)
                freq48, energy = demod_channel_batch(cc, st)
                if len(freq48) == 0:
                    continue
                # Live audio: the discriminator output is the FM demod
                # audio (Hz of deviation at 48 kHz). Scaled to int16 with
                # the same ±fs/2 == ±32767 convention as fm_demodulate,
                # it feeds the 🔊 Listen button — APRS stays listenable
                # (the dongle card keeps the button, unlike AIS/Orbcomm).
                # Audio full-scale: the Bell 202 FM deviation spans most of
                # the +/-24 kHz demod range, but GFSK 9600 only deviates
                # +/-4.8 kHz - unscaled it would sit at ~20% amplitude and
                # the LASARsat bursts would be barely audible on Listen
                audio_scale = 32767.0 / (LASAR_BITRATE / 2.0 if lasar
                                         else (APRS_FS / 2.0))
                audio = np.clip(freq48 * audio_scale,
                                -32767, 32767).astype(np.int16).tobytes()
                state.push_live_audio(entry, audio)
                for burst in collect_bursts(freq48, energy, st):
                    payloads = (decode_burst_gfsk96(burst) if lasar
                                else decode_burst(burst))
                    if payloads:
                        handle_frames(payloads, st)
                    elif len(burst) > 4 * (LASAR_SAMP if lasar else SYM_SAMP):
                        st["bad"] += 1
                with state.aprs_lock:
                    state.aprs_stats = {
                        "frames": st["frames"], "bad": st["bad"],
                        "last_frame": st["last_frame"],
                        "floor": round(st["floor"] or 0.0, 2),
                    }
        except Exception as e:
            if not entry.get("closed") and entry.get("aprs") and not entry.get("paused"):
                state.log_console(f"APRS thread error (dongle {did}): {e}", "error")
        entry["connected"] = False
        entry["sock"] = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill_rtl_sdr(entry.pop("proc", None))
        # A channel-preset flip (aprs_lasar / aprs_iss, radio.set_dongle_*)
        # closes the stream under this thread to force a retune - but the
        # thread's own reconnect loop would come back on the OLD channel
        # (freq_hz and the demod choice are bound at thread start). Exit
        # instead: the supervisor (sdr_thread) starts a fresh thread within
        # ~2 s that reads the new flags and tunes the new channel with the
        # right demodulator. Without this the flip logged "retuning" while
        # the dongle silently stayed put (observed live 2026-10-17).
        want_lasar = bool(entry.get("aprs_lasar"))
        want_freq = (LASAR_FREQ_HZ if want_lasar
                     else ISS_APRS_FREQ_HZ if entry.get("aprs_iss")
                     else APRS_FREQ_HZ)
        if want_freq != freq_hz or want_lasar != lasar:
            return
        if entry.get("closed") or not entry.get("aprs") or entry.get("paused"):
            aprs_reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)

# ---------- test/simulation helpers (used by tests/test_aprs.py) ----------

def build_frame(src, dst, info, *digis, src_ssid=0, dst_ssid=0):
    """Callsigns + info text -> AX.25 UI payload vector (hdlc_frames
    order: MSB-first per byte). Used by the test chain."""
    def addr(call, ssid, last):
        b = [ord(ch) << 1 for ch in call.ljust(6)[:6]]
        b.append((ssid & 0x0F) << 1 | 0x60 | (1 if last else 0))
        return b
    # the last-repeater bit sits on the FINAL address in the chain:
    # src with no digis, the last digi otherwise
    out = addr(dst, dst_ssid, False)
    out += addr(src, src_ssid, not digis)
    for i, call in enumerate(digis):
        out += addr(call.split("-")[0],
                    int(call.split("-")[1]) if "-" in call else 0,
                    i == len(digis) - 1)
    out += [0x03, 0xF0]
    out += [ord(ch) for ch in info[:247]]
    bits = []
    for byte in out:
        for j in range(7, -1, -1):
            bits.append((byte >> j) & 1)
    return bits

def afsk_modulate(data_bits, fs=SDR_RATE, dev=2400.0, amp=0.6):
    """NRZI data bits -> Bell 202 AFSK-on-FM test signal: the tone
    (1200 mark / 2200 space) rides the carrier as FM deviation, phase
    continuous at tone changes, like a real transmitter."""
    level, tones = 0, []
    for b in data_bits:
        if not b:
            level ^= 1                 # NRZI: 0 = tone change
        tones.append(APRS_MARK_HZ if level else APRS_SPACE_HZ)
    tone = np.repeat(np.array(tones), fs // APRS_BITRATE)
    audio = np.sin(2 * np.pi * np.cumsum(tone) / fs)   # phase-continuous tone
    phase = 2 * np.pi * np.cumsum(dev * audio) / fs   # ...as FM deviation
    return (amp * np.exp(1j * phase)).astype(np.complex64)

def gfsk96_modulate(data_bits, fs=SDR_RATE, dev=4800.0, amp=0.6,
                    offset_hz=0.0):
    """Data bits -> GFSK-on-FM test signal at 9600 baud, G3RUH-scrambled
    like a real 9k6 satellite transmitter (LASARsat digipeater). The
    NRZ level codes the frequency directly (no NRZI - the scrambler
    keeps the transitions); a one-symbol moving average approximates
    the Gaussian filter of real GFSK. offset_hz adds a carrier offset
    (Doppler test): the demodulator's median slicer must remove it."""
    assert fs % LASAR_BITRATE == 0
    k = fs // LASAR_BITRATE
    bits = scramble_g3ruh([int(b) & 1 for b in data_bits])
    nrz = np.repeat(np.array(bits, dtype=np.float64) * 2 - 1, k)
    sm = np.convolve(nrz, np.ones(k) / k, mode="same")
    phase = 2 * np.pi * np.cumsum(dev * sm + offset_hz) / fs
    return (amp * np.exp(1j * phase)).astype(np.complex64)

def frame_bits(payload):
    """Payload vector -> data bits of a complete AX.25 burst (preamble +
    flags + stuffed payload + FCS) - the domain decode_burst's NRZI
    stage reconstructs; afsk_modulate applies the tone-change coding."""
    t = payload_to_transmitted(payload)
    frame = stuff_bits(t + fcs_bits(t))
    return [0, 1] * 24 + list(FLAG) + frame + list(FLAG)
