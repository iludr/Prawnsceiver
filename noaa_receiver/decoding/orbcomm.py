"""Orbcomm receiver: satellite IoT downlink on 137.0-138.0 MHz.

A dongle switched to the Orbcomm mode (dashboard button, mirroring the
AIS mode switch) tunes to config.ORBCOMM_CENTER_HZ = 137.5 MHz: one
2.4 MHz capture spans the whole downlink band. Unlike AIS (two fixed
channels), the satellites sit anywhere in the band on a 2.5 kHz raster
with up to +/-2.5 kHz Doppler, so the receiver finds each active
satellite in the spectrum: per waterfall-row FFT it clusters band
energy above the per-bin noise floor, and every cluster becomes a
dynamically tracked demodulation channel (created on appearance,
dropped after ORBCOMM_CHANNEL_TIMEOUT_SECS of silence).

Radio layer (parameters from Frank Bieberly's ORBCOMM-receiver, whose
decoder is validated against real passes; framing cross-checked against
patent US8750429B2 and the emitters.space downlink analysis):

- SDPSK 4800 sym/s: a '1' advances the carrier phase by +90 deg over
  the symbol, a '0' by -90 deg - which is continuous-phase 2-FSK at
  +/-1200 Hz (modulation index 0.5), so the same frequency-slicing
  discriminator the AIS chain uses recovers the bits: the sign of the
  instantaneous frequency IS the differential phase, no NRZI.
- The downlink is a CONTINUOUS 4800 bps stream while a satellite is
  up (not slot bursts like AIS): packets follow each other back-to-back
  on a fixed 96-bit grid, so the bit-grid phase is constant during a
  pass and locks after the first checksum-valid packet.
- Packets: 12 bytes, the first byte is the type (0x65 Sync, 0x1A
  Message, 0x1B Uplink, 0x1C Downlink, 0x1D Network, 0x1E Fill, 0x22
  Orbital); the Ephemeris type 0x1F takes TWO grid slots (24 bytes) and
  carries the satellite's own GPS position (ECEF, 20-bit fields) plus
  GPS week / time-of-week - the live satellite position on the
  dashboard is decoded from the satellite's telemetry itself.
- Checksum: Fletcher-16 over the whole packet (mod 256, sum2 trailing
  sum1); a good packet leaves both sums zero. This validates every
  packet before it is logged, like the AIS FCS residue.
- Bit order: bytes are transmitted LSB-first on air (the header byte
  0x65 shows up as the bit pattern 10100110); both byte orders and both
  frequency polarities are tried, whichever validates wins.

Live data (mirrors the AIS dashboard section): per-channel stats and
position in state.orbcomm_channels, the newest decoded packets in
state.orbcomm_packets, and every decoded packet appended to the
orbcomm_messages table (trimmed to ORBCOMM_LOG_KEEP_HOURS).
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import numpy as np

from .. import db
from .. import state
from ..sdr.dsp import frequency_shift, sinc_taps
from ..calibration import SDR_DONGLE_GAIN, correction_info, tuning_correction
from ..config import (ORBCOMM_BAND_HZ, ORBCOMM_CENTER_HZ,
                      ORBCOMM_CHANNEL_TIMEOUT_SECS, ORBCOMM_LOG_KEEP_HOURS,
                      ORBCOMM_SYMRATE,
                      FFT_SIZE, IQ_BLOCK, SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE)

# Demod rate: 2.4 MHz -> 48 kHz (50x), the same discriminator rate as
# AIS - 10 samples per symbol at 4800 baud
ORBCOMM_DEC = SDR_RATE // 48000
ORBCOMM_FS = SDR_RATE // ORBCOMM_DEC
SYM_SAMP = ORBCOMM_FS // ORBCOMM_SYMRATE
assert SDR_RATE % 48000 == 0 and ORBCOMM_FS % ORBCOMM_SYMRATE == 0
_BLK_S = (IQ_BLOCK // 2) / SDR_RATE   # block duration, seconds
ORBCOMM_WF_BLOCKS = max(8, round(4.27e-3 / _BLK_S))   # one row per ~4 ms
# Rolling bit buffer per channel: ~0.9 s of stream. The grid scan
# needs at most 192 bits ahead of its anchor; trials keep a little
# history so a header straddling block edges is still found.
_BITBUF = 4096
_TRIALBUF = 2048
# A channel is dropped when it has produced no checksum-valid packet
# for this long - whether it never locked (a persistent local carrier
# looks like a carrier but never validates) or its last packet is
# older than this (a one-off false lock; real satellites emit Sync
# packets continuously, so seconds without one means not a satellite).
# The band detector opens noise-cluster channels constantly; each
# costs ~3-4 ms of CPU per 13.7 ms batch, so a short timeout plus the
# opening cap keeps the demod budget bounded.
_NOLOCK_TIMEOUT_SECS = 12.0
# Maximum simultaneously demodulating channels. A real pass has 1-2
# satellites; beyond this the detector's noise clusters would spend
# the whole CPU budget (and starve a real satellite's lock attempt).
# A carrier arriving while every slot is taken is ignored until a
# slot frees (drop timeout above, at most _NOLOCK_TIMEOUT_SECS).
_MAX_DEMOD_CHANNELS = 4

# ---------- Fletcher-16 (mod 256, over the whole packet) ----------

def fletcher16(data):
    """(sum1, sum2) of the Fletcher-16 checksum over a bytes-like."""
    s1 = s2 = 0
    for b in bytes(data):     # bytes() also normalizes numpy arrays
        s1 = (s1 + b) % 256
        s2 = (s1 + s2) % 256
    return s1, s2

def fletcher_tail(data):
    """The two bytes that make the full packet's Fletcher sums zero:
    fletcher16(data + fletcher_tail(data)) == (0, 0)."""
    s1, s2 = fletcher16(data)
    return bytes(((-(s1 + s2)) % 256, s2 % 256))

# ---------- packet types and contents ----------

PACKET_TYPES = {
    0x65: "Sync", 0x1A: "Message", 0x1B: "Uplink", 0x1C: "Downlink",
    0x1D: "Network", 0x1E: "Fill", 0x1F: "Ephemeris", 0x22: "Orbital",
}
EPHEMERIS = 0x1F
SYNC = 0x65

def _ecef_to_lla(x, y, z):
    """ECEF (m) -> (lat, lon, alt m); closed-form per EPSG 373-07-2 p.97
    (the same source ORBCOMM-receiver uses)."""
    a = 6378137.0
    f = 1.0 / 298.257223563
    b = a * (1.0 - f)
    e2 = 1.0 - b * b / (a * a)
    eps = e2 / (1.0 - e2)
    p = np.sqrt(x * x + y * y)
    q = np.arctan2(z * a, p * b)
    lat = np.arctan2(z + eps * b * np.sin(q) ** 3,
                     p - e2 * a * np.cos(q) ** 3)
    lon = np.arctan2(y, x)
    v = a / np.sqrt(1.0 - e2 * np.sin(lat) ** 2)
    alt = p / np.cos(lat) - v
    return float(np.degrees(lat)), float(np.degrees(lon)), float(alt)

def parse_ephemeris(data):
    """Satellite position + GPS time from a 24-byte Ephemeris packet.

    Field layout per ORBCOMM-receiver (validated against live
    satellites): payload is packet bytes 2..21 in REVERSE order; the
    20-bit ECEF components are 5-hex-char fields with per-nibble
    reversal, scaled +/- max_r_sat around zero.
    """
    if len(data) < 22:
        return None
    ph = bytes(data).hex()
    payload = "".join(ph[xx:xx + 2] for xx in range(42, 2, -2))
    try:
        week = int(payload[:4], 16)
        tow = int(payload[4:10], 16)

        def comp(field):   # 5 reversed hex chars -> 20-bit int
            s = field[::-1]
            return int(s[:2][::-1], 16) + 256 * int(s[2:4][::-1], 16) \
                + 65536 * int(s[4:], 16)

        max_r, scale = 8378155.0, 1048576.0

        def ecef(field):
            return 2.0 * comp(field) * max_r / scale - max_r

        x = ecef(payload[35:40])
        y = ecef(payload[30:35])
        z = ecef(payload[25:30])
        lat, lon, alt = _ecef_to_lla(x, y, z)
        gps = datetime(1980, 1, 6) + timedelta(weeks=week, seconds=tow)
    except (ValueError, ZeroDivisionError):
        return None
    return {"lat": round(lat, 5), "lon": round(lon, 5), "alt": round(alt),
            "gps_time": gps.strftime("%Y-%m-%d %H:%M:%S"), "gps_week": week}

# ---------- bit-level framing ----------

# The Sync header byte 0x65 as it appears on air, in all four
# orientations: (pattern, lsb-first bytes, inverted frequency).
# LSB-first + non-inverted is the live-satellite case per
# ORBCOMM-receiver, the others are kept because a wrong assumption
# costs nothing (the checksum rejects false anchors).
_H = (0, 1, 1, 0, 0, 1, 0, 1)   # 0x65, MSB-first
_HEADER_CANDIDATES = (
    (_H, False, False),
    (tuple(reversed(_H)), True, False),
    (tuple(1 - b for b in _H), False, True),
    (tuple(1 - b for b in reversed(_H)), True, True),
)
# Pre-converted +/-1 match vectors for _find_anchor's correlations
# (it runs up to 10 phases x 4 candidates per channel per batch -
# rebuilding these per call showed up in the profile)
_HEADER_MATCHERS = tuple((np.array(p, dtype=np.int8) * 2 - 1, rev, pol)
                         for p, rev, pol in _HEADER_CANDIDATES)

def _bytes_from_bits(bits, rev, pol):
    """A bit array -> packet bytes. `rev`: the air bits are LSB-first
    per byte; `pol`: the demodulator's frequency polarity was inverted
    (every bit flipped)."""
    n = (len(bits) // 8) * 8
    if n < len(bits):
        bits = np.asarray(bits[:n], dtype=np.int8)
    b = np.asarray(bits, dtype=np.int8).reshape(-1, 8)
    if rev:
        b = b[:, ::-1]
    if pol:
        b = 1 - b
    powers = 2 ** np.arange(7, -1, -1)
    return (b.astype(np.int64) * powers).sum(axis=1).astype(np.uint8)

def _header_byte(bits, rev, pol):
    return int(_bytes_from_bits(bits[:8], rev, pol)[0])

def _try_packet(bits, start, rev, pol, header=None):
    """Checksum-valid packet bytes at `start`, or None. The length is
    192 bits for the Ephemeris type (two grid slots), else 96."""
    seg = bits[start:start + 192 if header == EPHEMERIS else start + 96]
    if len(seg) < (192 if header == EPHEMERIS else 96):
        return None
    data = _bytes_from_bits(seg, rev, pol)
    if header is not None and data[0] != header:
        return None
    s1, s2 = fletcher16(data)
    if s1 or s2:
        return None
    return data

def _find_anchor(bits, from_ix=0):
    """Header scan: the first checksum-valid Sync packet at or after
    `from_ix` in the bit stream, as (start index, packet bytes, (rev,
    pol)). Anchors the 96-bit grid; without a valid Fletcher no random
    bit noise can pass (the AIS flag+CRC scan works the same way).
    Searching forward from the failed slot after a corrupted packet
    is what makes re-anchoring progress: restarting from the buffer
    head would find the already-seen first Sync and rewind the grid
    into seen packets forever.

    from_ix also bounds the correlation itself: a header match at or
    after from_ix has all 8 of its bits there, so the candidate window
    is sliced first - the per-batch incremental scan (demod_batch)
    correlates ~160 bits instead of the whole rolling buffer."""
    if len(bits) < 96:
        return None
    from_ix = max(0, int(from_ix))
    seg = bits[from_ix:]
    x = np.asarray(seg, dtype=np.int8) * 2 - 1
    for f, rev, pol in _HEADER_MATCHERS:
        corr = np.correlate(x, f, mode="valid") == 8
        for ix in np.flatnonzero(corr):
            data = _try_packet(bits, int(ix) + from_ix, rev, pol,
                               header=SYNC)
            if data is not None:
                return int(ix) + from_ix, data, (rev, pol)
    return None

# ---------- per-channel demod state ----------

# 610 taps at 2.4 MHz: -3 dB ~6 kHz, stopband by ~14 kHz. SDPSK at
# 4800 baud occupies +/-4 kHz (Carson); the neighboring Orbcomm
# channel (>=25 kHz away) and the offset-tuned DC spike (60 kHz)
# stay outside.
_ORB_TAPS = sinc_taps(8000, 610)
# polyphase form used by _demod_channel_batch (see there): complex64 so
# the window dot product stays in the input's dtype
_ORB_TAPS_C = _ORB_TAPS.astype(np.complex64)

def new_channel_state(did, key, freq_hz):
    """Fresh demodulation state for one tracked satellite channel."""
    return {
        "dongle": did, "key": key,
        "freq_hz": freq_hz,          # smoothed carrier centroid (display)
        # Fixed rotator offset: recomputing it from the drifting centroid
        # would switch the frequency_shift LUT mid-stream (its period
        # changes with the offset, invalidating st["rot"]) and jump the
        # rotation phase; the demod's dc estimator tracks the residual
        # Doppler instead. Quantized to 500 Hz to bound the LUT cache.
        "shift_hz": int(SDR_OFFSET_HZ + (ORBCOMM_CENTER_HZ
                                        - _quantize(freq_hz))),
        "rot": 0,                    # offset rotator counter (dsp.frequency_shift)
        "fir_tail": np.zeros(len(_ORB_TAPS) - 1, dtype=np.complex64),
        "last_c": None, "dec_pos": 0,
        "f48_count": 0,              # freq48 samples consumed (phase carrier)
        "dc": None,                   # carrier center estimate (see demod_batch)
        "bits": np.empty(0, dtype=np.int8), "bit_base": 0,
        "trial": [np.empty(0, dtype=np.int8) for _ in range(SYM_SAMP)],
        "phase": None, "orient": None,
        "grid": None,                # next expected packet start (absolute)
        "rescan": None,              # anchor search floor after a bad packet
        "seen": deque(maxlen=64),     # recent packet hex (dedup)
        "packets": 0, "bad": 0, "last_packet": 0.0,
        "sat_id": None, "lat": None, "lon": None, "alt": None,
        "gps_time": None, "gps_week": None,
        "first_seen": time.time(), "last_seen": time.time(),
    }

def _demod_channel_batch(c, st):
    """Offset-rotated IQ batch -> 48 kHz frequency samples (the 96-bit
    grid's phase source). Identical in output to filtering the whole
    2.4 MHz batch with the 610-tap channel FIR and keeping every
    50th sample - but the FIR is evaluated only AT those samples
    (polyphase: one 610-wide dot product per output, via
    sliding_window_view), ~20x cheaper than the full-rate FFT
    convolution. On this machine the full-rate chain cost ~13 ms per
    channel per 13.7 ms batch, so a single channel already saturated
    the thread (waterfall fell behind real time); the polyphase form
    leaves headroom for several simultaneous channels.

    The filter tail and the decimation phase carry across batches
    exactly like the AIS chain (fir_tail, dec_pos, last_c)."""
    x = np.concatenate([st["fir_tail"], c])
    st["fir_tail"] = x[-(len(_ORB_TAPS) - 1):].copy()
    m = len(x) - len(_ORB_TAPS) + 1       # valid FIR output positions
    if m <= 0:
        return np.empty(0, dtype=np.float32)
    start = (-st["dec_pos"]) % ORBCOMM_DEC
    st["dec_pos"] = (st["dec_pos"] + m) % ORBCOMM_DEC
    # convolve(x, h, 'valid')[j] == x[j:j+len] @ h[::-1]; the windowed
    # sinc is symmetric, so the reversal is a no-op and the taps are
    # used as they are
    f = (np.lib.stride_tricks.sliding_window_view(x, len(_ORB_TAPS))
         [start::ORBCOMM_DEC] @ _ORB_TAPS_C)
    prev = st["last_c"]
    if prev is None:
        prev = f[0]
    dd = np.empty(len(f), dtype=np.complex64)
    dd[0] = f[0] * np.conj(prev)
    np.multiply(f[1:], np.conj(f[:-1]), out=dd[1:])
    st["last_c"] = f[-1].copy()
    return np.arctan2(dd.imag, dd.real) * (ORBCOMM_FS / (2 * np.pi))

def _bits_append(st, new_bits):
    st["bits"] = np.concatenate([st["bits"], new_bits])
    if len(st["bits"]) > _BITBUF:
        cut = len(st["bits"]) - _BITBUF
        st["bits"] = st["bits"][cut:]
        st["bit_base"] += cut

def _scan_channel(st, now):
    """New checksum-valid packets in the locked-phase bit stream.

    While the 96-bit grid is anchored, packets are read back-to-back
    from it (Ephemeris consuming two slots); when a packet fails the
    checksum the grid is dropped and the Sync header scan re-anchors
    it FROM THE FAILED SLOT onward - a corrupted packet costs at most
    the seconds until the next Sync packet, exactly like the AIS flag
    scan between frames."""
    found = []
    bits = st["bits"]
    base = st["bit_base"]
    if st["grid"] is not None:
        while True:
            off = st["grid"] - base
            if off < 0:
                st["grid"] = None   # fell out of the rolling buffer
                st["rescan"] = base  # anchor search: whole buffer
                break
            if off + 96 > len(bits):
                break                # wait for more of the stream
            head = _header_byte(bits[off:], *st["orient"])
            if head not in PACKET_TYPES:
                st["bad"] += 1
                st["grid"] = None
                st["rescan"] = base + off
                break
            need = 192 if head == EPHEMERIS else 96
            if off + need > len(bits):
                break                # wait for the rest of the packet
            data = _try_packet(bits, off, *st["orient"], header=head)
            if data is None:
                st["bad"] += 1
                st["grid"] = None
                st["rescan"] = base + off
                break
            found.append((data, st["grid"]))
            st["grid"] += need
    if st["grid"] is None:
        lo = max(0, st.get("rescan", 0) - base)
        anchor = _find_anchor(bits, lo)
        if anchor is not None:
            ix, data, orient = anchor
            st["orient"] = orient
            st["rescan"] = None
            found.append((data, base + ix))
            st["grid"] = base + ix + (192 if data[0] == EPHEMERIS else 96)
    out = []
    for data, _pos in found:
        hexstr = bytes(data).hex()
        if hexstr in st["seen"]:
            continue
        st["seen"].append(hexstr)
        out.append(data)
    return out

def demod_batch(c, st):
    """One IQ batch for one channel -> new valid packet bytes.

    Trial phase: the symbol grid phase is unknown, so every one of the
    10 sample offsets within a symbol is demodulated into its own
    rolling stream and the Sync header scan looks for a validating
    packet in each; the first checksum-valid packet locks the phase
    (the stream is continuous, so the phase stays put for the whole
    pass). Locked phase: the single stream is scanned on its grid.

    The bit grid is continuous across batches: freq48 sample n of the
    channel maps to symbol slot n % 10, with n carried in
    st["f48_count"] (each batch's freq48 array starts mid-symbol, so
    slicing every batch from its own sample 0 would scramble the
    stream at the batch boundary).
    """
    f48 = _demod_channel_batch(c, st)
    if len(f48) < 2 * SYM_SAMP:
        return []
    # Residual carrier offset: the two FSK tones sit exactly +/-1200 Hz
    # around the carrier, so the robust mid-range of a batch estimates
    # the center. A MEDIAN would not do - an unbalanced batch (almost
    # all space bits while a satellite idles on Fill packets) puts the
    # median ON a tone and corrupts every bit sliced from it. The
    # estimate is clamped to +/-600 Hz per batch and EMA-smoothed, so
    # even a single-tone batch cannot drag it outside the +/-1200
    # slicing margin.
    # 10th/90th percentiles by hand (np.percentile costs ~0.4 ms per
    # channel per batch here) - linear interpolation, same values
    s = np.sort(f48)
    i10, i90 = 0.1 * (len(s) - 1), 0.9 * (len(s) - 1)
    lo10, lo90 = int(i10), int(i90)
    p10 = s[lo10] + (i10 - lo10) * (s[lo10 + 1] - s[lo10])
    p90 = s[lo90] + (i90 - lo90) * (s[min(lo90 + 1, len(s) - 1)] - s[lo90])
    obs = 0.5 * (float(p90) + float(p10))
    if st["dc"] is None:
        st["dc"] = obs
    else:
        obs = min(max(obs, st["dc"] - 600.0), st["dc"] + 600.0)
        st["dc"] += 0.1 * (obs - st["dc"])
    x = f48 - st["dc"]
    n0 = st["f48_count"]
    st["f48_count"] += len(f48)
    now = time.time()
    if st["phase"] is None:
        for ph in range(SYM_SAMP):
            trial = st["trial"][ph]
            start = (ph - n0) % SYM_SAMP
            len_before = len(trial)
            trial = np.concatenate([trial, x[start::SYM_SAMP] > 0])
            if len(trial) > _TRIALBUF:
                trial = trial[-_TRIALBUF:]
            st["trial"][ph] = trial
            # Only Fletcher-test header matches that involve new bits:
            # every position holding a complete packet was already
            # tested (and failed) last batch, and on noise the full
            # re-scan ran ~90 Python-level packet builds per phase
            # per batch. A 96-bit packet incomplete last batch starts
            # at len_before-95 .. len_before-1; earlier starts are old
            # news (anchors are always 96-bit windows).
            anchor = _find_anchor(trial, max(0, len_before - 95))
            if anchor is None:
                continue
            ix, data, orient = anchor
            st["phase"] = ph
            st["orient"] = orient
            st["bits"] = trial.astype(np.int8)
            st["bit_base"] = 0
            st["grid"] = ix + (192 if data[0] == EPHEMERIS else 96)
            st["seen"].append(bytes(data).hex())
            return [data]        # lock: the grid takes over from here
        return []
    start = (st["phase"] - n0) % SYM_SAMP
    _bits_append(st, (x[start::SYM_SAMP] > 0).astype(np.int8))
    return _scan_channel(st, now)

# ---------- decoded packets -> state, log, waterfall marks ----------

def _display_entry(st):
    return {
        "dongle": st["dongle"], "key": st["key"],
        "freq_khz": round(st["freq_hz"] / 1000.0, 1),
        "packets": st["packets"], "bad": st["bad"],
        "first_seen": round(st["first_seen"], 2),
        "last_seen": round(st["last_seen"], 2),
        "last_packet": round(st["last_packet"], 2),
        "sat_id": st["sat_id"], "lat": st["lat"], "lon": st["lon"],
        "alt": st["alt"], "gps_time": st["gps_time"],
        "gps_week": st["gps_week"],
    }

def handle_packets(st, packets, now=None):
    """Decode contents, update the live channel registry, the packet
    ring, the persistent log and the dongle card's waterfall marks."""
    now = now if now is not None else time.time()
    khz = (st["freq_hz"] - ORBCOMM_CENTER_HZ) / 1000.0
    de = state.sdrs.get(st["dongle"])
    for data in packets:
        st["packets"] += 1
        st["last_packet"] = now
        ptype = PACKET_TYPES.get(data[0], "Unrecognized")
        entry = {
            "ts": round(now, 2),
            "time": datetime.now().strftime("%H:%M:%S"),
            "dongle": st["dongle"],
            "freq_khz": round(st["freq_hz"] / 1000.0, 1),
            "type": ptype, "hex": bytes(data).hex(),
        }
        if ptype == "Sync":
            st["sat_id"] = bytes(data[6:8]).hex()
            entry["sat_id"] = st["sat_id"]
        elif ptype == "Ephemeris":
            pos = parse_ephemeris(data)
            if pos:
                st.update(pos)
                entry.update(pos)
        _log_message(entry)
        with state.orbcomm_lock:
            state.orbcomm_packets.append(entry)
            state.orbcomm_channels[st["key"]] = _display_entry(st)
        if de is not None:
            with de["lock"]:
                de.setdefault("wf_marks", deque(maxlen=64)).append(khz)

_log_lock = threading.Lock()
_log_appends = 0

def _trim_log():
    cutoff = time.time() - ORBCOMM_LOG_KEEP_HOURS * 3600
    try:
        with db.write() as cur:
            cur.execute("DELETE FROM orbcomm_messages WHERE ts < ?", (cutoff,))
    except Exception:
        pass

def _log_message(entry):
    """Append one decoded packet to the station database; never fatal."""
    global _log_appends
    with _log_lock:
        try:
            with db.write() as cur:
                cur.execute(
                    "INSERT INTO orbcomm_messages (ts, entry) VALUES (?,?)",
                    (entry.get("ts", time.time()),
                     json.dumps(entry, separators=(",", ":"))))
            _log_appends += 1
            if _log_appends % 256 == 0:
                _trim_log()
        except Exception as e:
            state.log_console(f"Orbcomm log write failed: {e}", "warn")

def orbcomm_log(count=200):
    """Newest-first tail of the persistent packet log."""
    try:
        count = max(1, min(int(count), 2000))
    except ValueError:
        count = 200
    out = []
    for r in db.query("SELECT entry FROM orbcomm_messages"
                      " ORDER BY id DESC LIMIT ?", (count,)):
        try:
            out.append(json.loads(r["entry"]))
        except ValueError:
            continue
    return out

def orbcomm_status():
    """Snapshot for /orbcomm.json and the WS 'orbcomm' topic: tracked
    channels with their stats and satellite positions, plus the most
    recent decoded packets for the live feed."""
    with state.orbcomm_lock:
        channels = sorted(state.orbcomm_channels.values(),
                          key=lambda c: -c.get("last_seen", 0))
        packets = list(state.orbcomm_packets)
        enabled = state.orbcomm_enabled
        dongle = state.orbcomm_dongle
    return {"enabled": enabled, "dongle": dongle, "channels": channels,
            "packets": packets[-40:]}

def load_log_summary():
    """Startup line for the console: how many packets the persistent
    log holds (not loaded into memory - it is queryable via
    /orbcommlog.json)."""
    try:
        r = db.query_one("SELECT COUNT(*) AS n FROM orbcomm_messages")
        state.log_console(f"🛰 Orbcomm packet log: {r['n']} packets "
                          f"on record (last {ORBCOMM_LOG_KEEP_HOURS} h)")
    except Exception:
        pass

# ---------- capture thread ----------

def _band_bin_range(correction):
    """FFT bin slice covering ORBCOMM_BAND_HZ in display (rolled-row)
    coordinates: bin 256 of the rolled row is the display center =
    ORBCOMM_CENTER_HZ + correction (the tuner's crystal correction
    shifts what the labeled center really is)."""
    bin_hz = SDR_RATE / FFT_SIZE
    lo = ORBCOMM_BAND_HZ[0] - ORBCOMM_CENTER_HZ - correction
    hi = ORBCOMM_BAND_HZ[1] - ORBCOMM_CENTER_HZ - correction
    k_lo = max(0, int(256 + np.floor(lo / bin_hz)))
    k_hi = min(FFT_SIZE, int(256 + np.ceil(hi / bin_hz)))
    return k_lo, k_hi, bin_hz

def _detect_channels(mag_rolled, det, k_lo, k_hi, bin_hz, correction):
    """Active carrier clusters in the band -> per-channel updates.

    Per-bin noise-floor EMA: static carriers (the offset-tuned DC
    spike, local land-mobile) raise their own bins' floor and stop
    counting as activity; a satellite that appears lights its bins
    above 2.5x the floor. Clusters (2-bin gaps bridged) become
    channel candidates at their power-weighted centroid.
    """
    band = mag_rolled[k_lo:k_hi]
    if det["floor"] is None:
        det["floor"] = band.astype(np.float64).copy()
    floor = det["floor"]
    floor += 0.02 * (band - floor)
    active = band > 2.5 * floor
    # mask the display position of the dongle's own DC spike
    rot = int(SDR_OFFSET_HZ / SDR_RATE * FFT_SIZE)
    dc = 256 + rot + int(round(correction / bin_hz))
    lo_i = max(0, dc - k_lo - 4)
    hi_i = min(len(active), dc - k_lo + 5)
    active[lo_i:hi_i] = False
    found = []
    n = len(active)
    i = 0
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j < n and gap <= 2:
            if active[j]:
                j += 1
                gap = 0
            else:
                j += 1
                gap += 1
        j -= gap          # trailing gap bins are not part of the cluster
        seg = band[i:j]
        w = seg / max(seg.sum(), 1e-9)
        center_bin = i + float((w * np.arange(len(seg))).sum())
        freq = (ORBCOMM_CENTER_HZ + correction
                + (center_bin - (256 - k_lo)) * bin_hz)
        found.append((freq, float(seg.max() / max(floor[i:j].mean(), 1e-9))))
        i = j
    return found

def _quantize(freq_hz):
    """500 Hz steps: bounds the frequency_shift phasor cache (the
    per-channel shift follows the Doppler-drifting centroid, and every
    distinct offset value would otherwise live in the LUT cache
    forever); residual offset is removed by the demodulator's
    two-tone center tracker."""
    return int(round(freq_hz / 500.0) * 500)

def orbcomm_capture_thread(did):
    """rtl_tcp/rtl_sdr connection + band-scan + per-channel demod for a
    dongle switched to Orbcomm reception. Mirrors the AIS capture
    thread's connection lifecycle (handshake, gain, reconnect with
    backoff); instead of two fixed channels it tracks every carrier it
    finds in the 137.0-138.0 MHz downlink band."""
    from ..sdr.dsp import iq_to_complex
    from ..sdr.radio import (_IQReader, _PipeRecv, push_wf_row,
                             _kill_rtl_sdr, _rtl_tcp_connect, _spawn_rtl_sdr,
                             CAPTURE_BATCH, IQ_BLOCK,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]
    det = {"floor": None, "channels": {}}

    def orbcomm_reception_off():
        others = [d for d, e in state.sdrs.items()
                  if d != did and e.get('orbcomm')
                  and not e.get('closed') and not e.get('paused')]
        with state.orbcomm_lock:
            if others:
                state.orbcomm_enabled = True
                state.orbcomm_dongle = others[0]
                return
            state.orbcomm_enabled = False
            state.orbcomm_dongle = None

    with state.orbcomm_lock:
        state.orbcomm_enabled = True
        state.orbcomm_dongle = did
        state.orbcomm_channels = {}
    state.log_console(f"🛰 Orbcomm receiver on dongle {did}: "
                      f"{ORBCOMM_BAND_HZ[0]/1e6:.1f}-{ORBCOMM_BAND_HZ[1]/1e6:.1f} MHz")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("orbcomm") or entry.get("paused"):
            orbcomm_reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            correction = tuning_correction(ORBCOMM_CENTER_HZ, did)
            tune = ORBCOMM_CENTER_HZ + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = correction_info(ORBCOMM_CENTER_HZ, did)
            gain_db = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
            if entry.get("usb_idx") is not None:
                proc = _spawn_rtl_sdr(entry, tune, gain_db)
                if proc is None:
                    raise RuntimeError("rtl_sdr not available on this host")
                entry["proc"] = proc
                entry["connected"] = True
                entry["tuner"] = "USB (rtl_sdr)"
                entry["last_data"] = time.time()
                state.log_console(f"Connected to Orbcomm USB dongle {did} "
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
                state.log_console(f"Connected to Orbcomm rtl_tcp {did}, tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, correction {correction:+d}Hz)")
                reader = _IQReader(sock, IQ_BLOCK)
            k_lo, k_hi, bin_hz = _band_bin_range(correction)
            sig_bins = max(1, round(39375 / (SDR_RATE / FFT_SIZE)))
            while True:
                if entry.get("closed") or not entry.get("orbcomm"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (Orbcomm dongle {did}), reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                c = iq_to_complex(raw)
                # Waterfall + signal for the dongle card, plus the band
                # energy scan riding on the same FFTs (push_wf_row hands
                # back the rolled pre-normalization magnitude)
                for k in range(0, len(c) // FFT_SIZE, ORBCOMM_WF_BLOCKS):
                    try:
                        rolled = push_wf_row(entry, c, k, sig_bins)
                        for freq, _snr in _detect_channels(
                                rolled, det, k_lo, k_hi, bin_hz, correction):
                            _track_candidate(det, did, freq, bin_hz)
                    except Exception:
                        pass
                _demod_channels(det, c)
                _prune_channels(det)
        except Exception as e:
            if not entry.get("closed") and entry.get("orbcomm") and not entry.get("paused"):
                state.log_console(f"Orbcomm thread error (dongle {did}): {e}", "error")
        entry["connected"] = False
        entry["sock"] = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill_rtl_sdr(entry.pop("proc", None))
        if entry.get("closed") or not entry.get("orbcomm") or entry.get("paused"):
            orbcomm_reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)

def _track_candidate(det, did, freq, bin_hz):
    """Match a detected carrier to an existing channel (within 4 kHz -
    a pass's Doppler drifts by ~5 kHz over minutes, never 4 kHz per
    scan) or open a new one; refreshes the channel's last_seen. New
    channels open only while a demod slot is free (_MAX_DEMOD_CHANNELS
    - every channel costs CPU per batch, and the detector feeds noise
    clusters in constantly)."""
    for key, st in det["channels"].items():
        if abs(st["freq_hz"] - freq) < 4000.0:
            st["freq_hz"] += 0.25 * (freq - st["freq_hz"])
            st["last_seen"] = time.time()
            return
    if len(det["channels"]) >= _MAX_DEMOD_CHANNELS:
        return
    key = f"{did}:{_quantize(freq) // 1000}"
    st = new_channel_state(did, key, freq)
    det["channels"][key] = st
    state.log_console(f"🛰 Orbcomm: new carrier at "
                      f"{freq / 1e6:.4f} MHz — demodulating (channel {key})")

def _demod_channels(det, c):
    """One IQ batch through every tracked channel's demodulator."""
    for st in det["channels"].values():
        cc = frequency_shift(c, st["shift_hz"], SDR_RATE, st)
        packets = demod_batch(cc, st)
        if packets:
            handle_packets(st, packets)

def _prune_channels(det):
    """Drop dead channels: no carrier for ORBCOMM_CHANNEL_TIMEOUT_SECS
    (pass over / noise cluster), or no checksum-valid packet for
    _NOLOCK_TIMEOUT_SECS - a channel that never locked (persistent
    local carrier) and one whose last packet has gone stale (a one-off
    false lock would otherwise occupy a demod slot forever, since the
    carrier keeps being re-detected). The live registry entry lingers
    on the dashboard until it times out there."""
    now = time.time()
    for key in [k for k, st in det["channels"].items()
                if now - st["last_seen"] > ORBCOMM_CHANNEL_TIMEOUT_SECS
                or (st["packets"] == 0
                    and now - st["first_seen"] > _NOLOCK_TIMEOUT_SECS)
                or (st["packets"] > 0
                    and now - st["last_packet"] > _NOLOCK_TIMEOUT_SECS)]:
        st = det["channels"].pop(key)
        state.log_console(f"🛰 Orbcomm: channel {key} at "
                          f"{st['freq_hz'] / 1e6:.4f} MHz dropped "
                          f"({st['packets']} packets)")

# ---------- test/simulation helpers (used by tests/test_orbcomm.py) ----------

def sdpsk_modulate(data_bits, fs=SDR_RATE, amp=0.6):
    """SDPSK test signal: +90 deg/symbol for a 1, -90 for a 0 == a
    continuous-phase +/-1200 Hz FSK (what the discriminator's sign
    slicing recovers bit-for-bit)."""
    up = np.repeat(np.array(data_bits, dtype=np.int8) * 2 - 1,
                   fs // ORBCOMM_SYMRATE)
    fm = up * (ORBCOMM_SYMRATE / 4.0)
    phase = 2 * np.pi * np.cumsum(fm) / fs
    return (amp * np.exp(1j * phase)).astype(np.complex64)

def packet_bits(data):
    """Packet bytes -> on-air bit order: LSB-first per byte (the
    orientation live satellites use)."""
    bits = []
    for b in data:
        for i in range(8):
            bits.append((b >> i) & 1)
    return bits

def build_packet(ptype, payload, sat_id=None):
    """A checksum-valid packet: type byte + payload + Fletcher tail."""
    data = bytes([ptype]) + bytes(payload)
    if sat_id is not None:
        data = bytes([ptype, sat_id]) + bytes(payload)
    return data + fletcher_tail(data)

def build_ephemeris(sat_id, x, y, z, week, tow):
    """24-byte Ephemeris packet for a chosen ECEF position (m) and GPS
    time (the inverse of parse_ephemeris)."""
    max_r, scale = 8378155.0, 1048576.0

    def field(v):
        t = int(round((v + max_r) * scale / (2 * max_r)))
        t = max(0, min(t, 2 ** 20 - 1))
        a, t = t & 0xFF, t >> 8
        b, c = t & 0xFF, t >> 8
        s = f"{a:02X}"[::-1] + f"{b:02X}"[::-1] + f"{c:1X}"
        return s[::-1]

    payload = (f"{week:04X}" + f"{tow:06X}"
               + field(0) * 3          # velocities: unused by the parser
               + field(z) + field(y) + field(x))
    # payload is packet bytes 2..21 in reverse order
    body = bytes([EPHEMERIS, sat_id]) + bytes.fromhex(
        "".join(payload[xx:xx + 2] for xx in range(len(payload) - 2, -1, -2)))
    return body + fletcher_tail(body)
