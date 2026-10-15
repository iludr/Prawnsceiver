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
radios encoding the position in the destination field) are logged with
callsign and text but without position - a follow-up if the local
traffic turns out to be mostly Mic-E.

Live data: state.aprs_stations (callsign -> newest info), the newest
packets in state.aprs_packets, every frame appended to the aprs_messages
table (trimmed to APRS_LOG_KEEP_HOURS).
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
from ..sdr.dsp import frequency_shift
from ..calibration import SDR_DONGLE_GAIN, correction_info, tuning_correction
from ..config import (APRS_FREQ_HZ, APRS_LOG_KEEP_HOURS,
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


# ---------- AFSK demodulation ----------

def _sinc_taps(cutoff_hz, numtaps, fs=SDR_RATE):
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / fs * m) * np.hamming(numtaps)
    return (h / h.sum()).astype(np.float64)


# 610 taps at 2.4 MHz: -3 dB ~12 kHz, stopband by ~20 kHz. Bell 202 on
# FM occupies 2*(deviation + tone) ~ 2*(3 + 2.2) = 10.4 kHz (Carson),
# so 12 kHz keeps the whole signal while staying inside the 25 kHz
# channel raster; the offset DC spike (60 kHz) is far outside.
_APRS_TAPS = _sinc_taps(12000, 610)
_APRS_TAPS_C = _APRS_TAPS.astype(np.complex64)   # polyphase matmul form

def new_channel_state(did):
    """Fresh DSP state for the one APRS channel of one dongle."""
    return {
        "dongle": did,
        "rot": 0,                        # offset rotator counter
        "fir_tail": np.zeros(len(_APRS_TAPS) - 1, dtype=np.complex64),
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
    x = np.concatenate([st["fir_tail"], c])
    st["fir_tail"] = x[-(len(_APRS_TAPS) - 1):].copy()
    m = len(x) - len(_APRS_TAPS) + 1
    if m <= 0:
        return np.empty(0, dtype=np.float32), 0.0
    start = (-st["dec_pos"]) % APRS_DEC
    st["dec_pos"] = (st["dec_pos"] + m) % APRS_DEC
    f = (np.lib.stride_tricks.sliding_window_view(x, len(_APRS_TAPS))
         [start::APRS_DEC] @ _APRS_TAPS_C)
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
                    min_batches=APRS_MIN_BURST_BATCHES):
    """Noise-floor tracking + burst assembly at batch granularity
    (see ais.collect_bursts - the same EMA floor, the same one-quiet-
    block close, with batch counts instead of 512-sample blocks)."""
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

# ---------- decoded frames -> registry, log, marks ----------

def _display_entry(d, now):
    return {
        "call": d["src"], "dst": d["dst"],
        "via": ",".join(d["digis"]),
        "type": (d["info"][0] if 32 <= ord(d["info"][0]) < 127 else "?"),
        "text": d["info"][:120],
        "lat": d.get("lat"), "lon": d.get("lon"), "sym": d.get("sym"),
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
        entry = _display_entry(d, now)
        entry["ts"] = round(now, 2)
        entry["time"] = datetime.now().strftime("%H:%M:%S")
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
        state.log_console(f"📡 APRS {d['src']}>{d['dst']}"
                          + (f" via {entry['via']} " if d["digis"] else ": ")
                          + d["info"][:80])
        if de is not None:
            with de["lock"]:
                de.setdefault("wf_marks", deque(maxlen=64)).append(0.0)

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

def aprs_status():
    """Snapshot for /aprs.json: registry, stats, recent packets."""
    with state.aprs_lock:
        stations = sorted(state.aprs_stations.values(),
                          key=lambda s: -s.get("last_seen", 0))
        packets = list(state.aprs_packets)
        enabled = state.aprs_enabled
        serial = state.aprs_dongle
        stats = dict(state.aprs_stats)
    return {"enabled": enabled, "dongle": serial, "stats": stats,
            "stations": stations, "packets": packets}

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
    channel instead of two, and no retune (the band never moves)."""
    from ..sdr.dsp import iq_to_complex
    from ..sdr.radio import (_IQReader, _PipeRecv, _get_fft_window,
                             _kill_rtl_sdr, _rtl_tcp_connect, _spawn_rtl_sdr,
                             CAPTURE_BATCH, IQ_BLOCK,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]
    # move the 144.800 MHz channel onto 0 Hz: the dongle is tuned
    # SDR_OFFSET_HZ above APRS_FREQ_HZ, so the signal arrives at
    # -SDR_OFFSET_HZ (see ais._ais_shifts)
    shift = int(SDR_OFFSET_HZ)
    st = new_channel_state(did)

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

    with state.aprs_lock:
        state.aprs_enabled = True
        state.aprs_dongle = did
    state.log_console(f"📡 APRS receiver on dongle {did}: "
                      f"{APRS_FREQ_HZ / 1e6:.3f} MHz (Bell 202 AFSK 1200 baud)")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("aprs") or entry.get("paused"):
            aprs_reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            correction = tuning_correction(APRS_FREQ_HZ, did)
            tune = APRS_FREQ_HZ + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = correction_info(APRS_FREQ_HZ, did)
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
            rot = int(SDR_OFFSET_HZ / SDR_RATE * FFT_SIZE)
            while True:
                if entry.get("closed") or not entry.get("aprs"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (APRS dongle {did}), reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                c = iq_to_complex(raw)
                # Waterfall + signal for the dongle card (same rows and
                # dBFS feed as the other role threads), rotated by the
                # display offset so the card's center reads 144.800 MHz
                for k in range(0, len(c) // FFT_SIZE, APRS_WF_BLOCKS):
                    try:
                        window = _get_fft_window()
                        magnitude = np.abs(np.fft.fftshift(np.fft.fft(
                            c[k * FFT_SIZE:(k + 1) * FFT_SIZE] * window)))
                        center = len(magnitude) // 2
                        db_row = np.roll((20 * np.log10(
                            np.maximum(magnitude, 1e-6)
                            / (31.875 * FFT_SIZE)) * 10).astype(np.int16), rot).tobytes()
                        rolled = np.roll(magnitude, rot)
                        with entry["lock"]:
                            entry["signal"] = float(
                                magnitude[center - _SIG_BINS:center + _SIG_BINS].mean())
                            peak = magnitude.max()
                            if peak > 0:
                                magnitude *= 255.0 / peak
                            entry["waterfall"].append(rolled.astype(int).tolist())
                            entry["wf_db"].append(db_row)
                            entry["wf_db_seq"] += 1
                    except Exception:
                        pass
                cc = frequency_shift(c, shift, SDR_RATE, st)
                freq48, energy = demod_channel_batch(cc, st)
                if len(freq48) == 0:
                    continue
                for burst in collect_bursts(freq48, energy, st):
                    payloads = decode_burst(burst)
                    if payloads:
                        handle_frames(payloads, st)
                    elif len(burst) > 4 * SYM_SAMP:
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
    t = np.arange(len(tone)) / fs
    audio = np.sin(2 * np.pi * np.cumsum(tone) / fs)   # phase-continuous tone
    phase = 2 * np.pi * np.cumsum(dev * audio) / fs   # ...as FM deviation
    return (amp * np.exp(1j * phase)).astype(np.complex64)

def frame_bits(payload):
    """Payload vector -> data bits of a complete AX.25 burst (preamble +
    flags + stuffed payload + FCS) - the domain decode_burst's NRZI
    stage reconstructs; afsk_modulate applies the tone-change coding."""
    t = payload_to_transmitted(payload)
    frame = stuff_bits(t + fcs_bits(t))
    return [0, 1] * 24 + list(FLAG) + frame + list(FLAG)
