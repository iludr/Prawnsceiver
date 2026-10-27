"""433.92 MHz ISM sensor decoding — a dongle role like POCSAG/AIS.

A dongle in the ISM role parks on 433.92 MHz and decodes the OOK
telemetry of the cheap 433 MHz thermo-hygrometers heard there (the
2026-10-23/24 analysis: Nexus/Rubicson-family PPM rows at
433.9008/433.9295/433.9389 MHz and an unnamed PWM device at
433.930957 MHz — see tools/iq433/README.md).

Demod: |LP(shift)| envelope of the baseband around 433.92 MHz
(the iq433 toolkit's shift_env chain, ported to a live streaming
decoder), ON/OFF run timing, packet splitting at > 3 ms gaps, PWM/PPM
auto-detect per packet, then the sensor payload decoders:

- Nexus/Rubicson family, 36-bit PPM rows: [id0 id1 flags t0 t1 t2 0xF h0 h1]
  (verified against the 2026-10-23 recording: temp = ((t0&7)<<8|t1<<4|t2)/10
  °C with t0 bit3 = sign, flags bit3 = battery-low, (flags>>2)&3 + 1 =
  channel, humidity byte when the Rubicson CRC-8 (poly 0x31, init 0x6c)
  does not match).
- The unnamed PWM device, 48-bit frames after a 7-symbol preamble:
  6 bytes, last byte = sum of the first five, temp = ((b1&0xF)<<8|b2)/10,
  humidity = b3 when plausible.
"""
import json
import threading
import time

import numpy as np

from .. import state
from ..config import (ISM_CENTER_HZ, ISM_LOG_KEEP_HOURS, SDR_OFFSET_HZ,
                      SDR_RATE)
from .. import db

# gaps longer than this end a packet (the Nexus sync gap is ~3.9 ms)
_PACKET_GAP_US = 3000
# ON runs shorter than this are single-envelope-sample noise spikes, not
# pulses: the shortest legit pulse here is ~420 us (Nexus) and a spike
# inside a gap would otherwise split it into two phantom bits
_MIN_PULSE_US = 150
# dedupe window: sensors repeat each row ~10x per burst (~74 ms apart)
_DEDUPE_SECS = 3.0

# ---------- dsp helpers (ports of the iq433 toolkit's live-capable parts) ---

def _runs(env, thr):
    """ON runs of env > thr: (starts, ends) in samples."""
    on = env > thr
    d = np.diff(on.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if on[0]:
        starts = [0] + starts
    if on[-1]:
        ends = ends + [len(on)]
    return np.array(starts), np.array(ends)


def _merge_close(starts, ends, min_gap):
    """Merge runs separated by less than min_gap samples (noise dips)."""
    if len(starts) == 0:
        return starts, ends
    s2, e2 = [starts[0]], [ends[0]]
    for s, e in zip(starts[1:], ends[1:]):
        if s - e2[-1] < min_gap:
            e2[-1] = e
        else:
            s2.append(s)
            e2.append(e)
    return np.array(s2), np.array(e2)


def _two_cluster_bits(values, ratio=1.5):
    """Split values into two clusters at the largest gap of the sorted
    values (lower = 0, upper = 1); median*ratio fallback when they do
    not separate."""
    v = np.sort(np.asarray(values, dtype=float))
    if len(v) < 2:
        return '0' * len(v)
    gaps = np.diff(v)
    i = int(np.argmax(gaps))
    if gaps[i] > 0.25 * v[-1]:
        thr = (v[i] + v[i + 1]) / 2.0
    else:
        thr = np.median(v) * (ratio or 1.5)
    return ''.join('1' if x > thr else '0' for x in values)


def _crc8(bits, poly=0x31, init=0x6c):
    """MSB-first CRC-8 over a bit string (Rubicson: poly 0x31, init 0x6c)."""
    crc = init
    for ch in bits:
        bit = int(ch)
        crc ^= bit << 7
        if crc & 0x80:
            crc = ((crc << 1) ^ poly) & 0xFF
        else:
            crc = (crc << 1) & 0xFF
    return crc


def _bits_hex(bits):
    bb = bits[:len(bits) // 8 * 8]
    return ' '.join('%02X' % int(bb[i:i + 8], 2) for i in range(0, len(bb), 8))


# ---------- sensor payload decoders ----------

def _nexus_decode(bits):
    """Nexus/Rubicson family, 36-bit PPM row (verified against the
    2026-10-23 recording). Returns the sensor dict or None."""
    nib = [int(bits[i:i + 4], 2) for i in range(0, 36, 4)]
    if len(nib) < 9 or nib[6] != 0xF:
        return None
    id_ = nib[0] << 4 | nib[1]
    flags = nib[2]
    t12 = ((nib[3] & 0x7) << 8) | (nib[4] << 4) | nib[5]
    temp = t12 / 10.0 * (-1 if nib[3] & 0x8 else 1)
    if id_ == 0xFF or not -40.0 <= temp <= 60.0:
        return None     # silence/noise rows decode as all-ones — reject
    crc_ok = _crc8(bits[:28]) == (nib[7] << 4 | nib[8])
    out = {
        "proto": "rubicson" if crc_ok else "nexus",
        "id": "%02X" % id_,
        # channel switch in flags bits 0-1 (verified against all three
        # recorded sensors: 0xA->ch3, 0x0->ch1, 0x8->ch1)
        "channel": (flags & 0x3) + 1,
        # flags bit3 = battery OK (verified: the id-03 row's flags 0xA
        # with a good battery; the low-battery row clears it)
        "battery": "ok" if flags & 0x8 else "low",
        "temp_c": round(temp, 1),
    }
    if crc_ok:
        # the last byte is the CRC: temperature-only sensor
        out["humidity"] = None
    else:
        hum = nib[7] << 4 | nib[8]
        out["humidity"] = hum if 20 <= hum <= 90 else None
    return out


def _pwm48_decode(bits):
    """The unnamed 48-bit PWM device (6 bytes, last byte = sum of the
    first five; 7-symbol preamble skipped by the caller). Temp/humidity
    layout fitted to the 2026-10-23 payload: temp = ((b1&0xF)<<8|b2)/10,
    humidity = b3."""
    by = [int(bits[i:i + 8], 2) for i in range(0, 48, 8)]
    if len(by) < 6:
        return None
    if by[5] != (sum(by[:5]) & 0xFF):
        return None
    t12 = ((by[1] & 0xF) << 8) | by[2]
    temp = t12 / 10.0 * (-1 if by[1] & 0x8 else 1)
    if not -40 <= temp <= 60:
        return None
    hum = by[3] if 20 <= by[3] <= 90 else None
    return {
        "proto": "pwm48",
        "id": "%02X" % by[0],
        "channel": None,
        "battery": None,
        "temp_c": round(temp, 1),
        "humidity": hum,
    }


# ---------- live streaming decoder ----------

class OokDecoder:
    """Stateful OOK envelope decoder across IQ blocks.

    feed(raw_bytes, now) -> list of decoded packet dicts. The envelope
    (|LP 25 kHz| of the baseband shifted to DC, decimated to 100 kS/s)
    is thresholded per block (median + 0.45 * (p99 - median), the iq433
    toolkit's rule); ON/OFF runs assemble into packets across block
    boundaries; a gap > 3 ms ends a packet. Each packet: PWM/PPM
    auto-detect, both polarities, 0/7-bit preamble skip, sensor payload
    decode; identical packets within _DEDUPE_SECS are dropped (the
    sensors repeat each row ~10x per burst).
    """

    def __init__(self):
        from ..sdr.dsp import new_state
        self.st = new_state()
        self.pos = 0            # global envelope-sample counter
        self.in_on = False      # a pulse is open (may span block edges)
        self.on_start = 0       # global sample the open pulse started at
        self.last_end = 0       # global sample of the last ON sample
        self.on_us = []         # current packet's ON widths
        self.gap_us = []        # current packet's inter-pulse gaps
        self.seen = {}          # bits -> ts of the last identical packet

    def feed(self, raw, now):
        from ..sdr.dsp import (iq_to_complex, frequency_shift, lowpass,
                               decimate_cont)
        c = iq_to_complex(raw)
        # the capture tunes ISM_CENTER_HZ + SDR_OFFSET_HZ: shift the
        # sensor band to DC (same chain as the iq433 toolkit's shift_env)
        c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE, self.st)
        c = lowpass(c, 25000.0, self.st)
        env = decimate_cont(np.abs(c).astype(np.float32), 24, self.st)
        return self._env(env, now)

    def _end_block(self, now, fs2, out):
        """A packet that ends in trailing silence: close an open pulse
        and flush when the silence exceeds the packet gap."""
        if self.on_us and (self.pos - self.last_end) / fs2 * 1e6 > _PACKET_GAP_US:
            if self.in_on:
                self.on_us.append((self.last_end - self.on_start) / fs2 * 1e6)
                self.in_on = False
            out += self._flush(now)

    def _env(self, env, now):
        out = []
        fs2 = SDR_RATE / 24.0
        med = float(np.median(env))
        p99 = float(np.percentile(env, 99))
        if p99 - med < 1.2:
            # a noise-only block (no signal above the floor): treat as
            # silence — running the run detector on noise would fabricate
            # all-ones garbage packets. (Measured on the 2026-10-23
            # recording: noise p99-med ~0.7, the sensor bursts ~1.6.)
            self.pos += len(env)
            self._end_block(now, fs2, out)
            return out
        thr = med + 0.45 * (p99 - med)
        starts, ends = _runs(env, thr)
        # merge runs separated by < 300 us (threshold wobble splits a
        # long pulse into fragments — the shortest real inter-pulse gap
        # here is the PWM device's ~975 us, so 300 us is safe)
        starts, ends = _merge_close(starts, ends, int(300 * fs2 / 1e6))
        dip = int(300 * fs2 / 1e6)   # noise dips / block-edge quantization
        for s, e in zip(starts, ends):
            gs, ge = self.pos + s, self.pos + e
            if self.in_on:
                if gs - self.last_end <= dip:
                    self.last_end = ge      # the pulse continues
                    continue
                # the open pulse ended at last_end
                self.on_us.append((self.last_end - self.on_start) / fs2 * 1e6)
                self.in_on = False
            # a noise spike (shorter than any legit pulse): ignore it
            # completely — opening it would split the surrounding gap
            # into two phantom bits and pollute the gap clusters
            if (e - s) / fs2 * 1e6 < _MIN_PULSE_US:
                continue
            # entering a new pulse at gs
            if self.on_us:
                gap = (gs - self.last_end) / fs2 * 1e6
                if gap > _PACKET_GAP_US:
                    out += self._flush(now)
                else:
                    self.gap_us.append(gap)
            self.in_on = True
            self.on_start = gs
            self.last_end = ge
        self.pos += len(env)
        # a packet that ends in trailing silence (no further pulse this block)
        self._end_block(now, fs2, out)
        return out

    def _flush(self, now):
        out = self._decode_packet(now)
        self.on_us = []
        self.gap_us = []
        self.in_on = False
        return out

    def _decode_packet(self, now):
        if not 8 <= len(self.on_us) <= 120:
            return []
        on = np.array(self.on_us)
        gaps = np.array(self.gap_us) if self.gap_us else np.array([0.0])
        on_med = float(np.median(on))
        intra = gaps[gaps < _PACKET_GAP_US]
        if not 100 <= on_med <= 2500:       # not sensor timing
            return []
        if len(intra):
            g_med = float(np.median(intra))
            if not 200 <= g_med <= 2800:
                return []
        else:
            g_med = 0.0
        on_lo, on_hi = np.percentile(on, 10), np.percentile(on, 90)
        g_lo, g_hi = (np.percentile(intra, 10), np.percentile(intra, 90)) \
            if len(intra) else (0.0, 0.0)
        cands = []
        if on_hi > on_lo * 1.6:
            cands.append(_two_cluster_bits(on))
        if g_hi > g_lo * 1.6:
            cands.append(_two_cluster_bits(intra))
        out = []
        for bits in cands:
            for inv in (False, True):
                b = bits.translate(str.maketrans('01', '10')) if inv else bits
                for pre in (0, 7):
                    payload = b[pre:]
                    if len(payload) < 24:
                        continue
                    dec = None
                    if len(payload) >= 36:
                        dec = _nexus_decode(payload[:36])
                    if dec is None and len(payload) >= 48:
                        dec = _pwm48_decode(payload[:48])
                    if dec is None:
                        continue
                    key = dec["proto"] + ":" + _bits_hex(payload[:48])
                    if now - self.seen.get(key, 0) < _DEDUPE_SECS:
                        continue
                    self.seen[key] = now
                    dec["hex"] = _bits_hex(payload[:48])
                    out.append(dec)
        # keep the dedupe map bounded
        if len(self.seen) > 512:
            cut = now - 60
            self.seen = {k: v for k, v in self.seen.items() if v > cut}
        return out


# ---------- registry + log ----------

def _sensor_key(d):
    return (d.get("proto"), d.get("id"), d.get("channel"))


def _register(p):
    """Merge one decoded packet into the in-memory sensor registry."""
    key = _sensor_key(p)
    now = time.time()
    s = state.ism_sensors.get(key)
    if s is None:
        s = state.ism_sensors[key] = {
            "proto": p["proto"], "id": p["id"], "channel": p["channel"],
            "freq_mhz": p["freq_mhz"], "first_seen": now, "msgs": 0}
    s["temp_c"] = p.get("temp_c")
    s["humidity"] = p.get("humidity")
    s["battery"] = p.get("battery")
    s["last_seen"] = now
    s["msgs"] += 1


def restore_sensors():
    """Rebuild the sensor registry from the last 24 h of the message log."""
    since = time.time() - 24 * 3600
    for r in db.query("SELECT entry FROM ism_messages WHERE ts > ?"
                      " ORDER BY ts", (since,)):
        try:
            p = json.loads(r["entry"])
        except ValueError:
            continue
        _register(p)


def ism_status():
    """Snapshot for /ism.json and the WS 'ism' topic."""
    with state.ism_lock:
        enabled = state.ism_enabled
        dongle = state.ism_dongle
        sensors = sorted(state.ism_sensors.values(),
                         key=lambda s: -s.get("last_seen", 0))
        recent = list(state.ism_packets)
    now = time.time()
    for s in sensors:
        s["last_seen_age_s"] = round(now - s.get("last_seen", now), 1)
    return {
        "enabled": enabled, "dongle": dongle,
        "freq_mhz": round(ISM_CENTER_HZ / 1e6, 4),
        "sensors": sensors, "recent": recent,
    }


def ism_sensor_history(sensor_id, hours=24):
    """Time series for one sensor id over the last `hours` hours, from the
    persistent message log: [{ts, temp_c, humidity, battery}, ...]."""
    since = time.time() - hours * 3600
    out = []
    for r in db.query("SELECT ts, entry FROM ism_messages"
                      " WHERE ts > ? ORDER BY ts", (since,)):
        try:
            p = json.loads(r["entry"])
        except ValueError:
            continue
        if p.get("id") != sensor_id:
            continue
        out.append({"ts": r["ts"], "temp_c": p.get("temp_c"),
                    "humidity": p.get("humidity"),
                    "battery": p.get("battery")})
    return out


def ism_log(count=200):
    """Newest-first tail of the persistent decoded-packet log."""
    try:
        count = max(1, min(int(count), 2000))
    except ValueError:
        count = 200
    out = []
    for r in db.query("SELECT entry FROM ism_messages"
                      " ORDER BY id DESC LIMIT ?", (count,)):
        try:
            out.append(json.loads(r["entry"]))
        except ValueError:
            continue
    return out


def _log_packet(p):
    try:
        with db.write() as cur:
            cur.execute("INSERT INTO ism_messages (ts, entry) VALUES (?,?)",
                        (p.get("ts", time.time()),
                         json.dumps(p, separators=(",", ":"))))
            cur.execute("DELETE FROM ism_messages WHERE ts < ?",
                        (time.time() - ISM_LOG_KEEP_HOURS * 3600,))
    except Exception as e:
        state.log_console(f"ISM log write failed: {e}", "warn")


# ---------- capture thread ----------

def ism_capture_thread(did):
    """rtl_tcp/rtl_sdr connection + OOK sensor decoding for a dongle
    switched to the ISM role. Mirrors the POCSAG capture thread's
    connection lifecycle (handshake, gain, reconnect with backoff, role
    handover); the IQ stream is envelope-demodulated around 433.92 MHz
    and every sensor packet decoded (see OokDecoder)."""
    from ..sdr.radio import (_IQReader, _PipeRecv, dongle_gain_db,
                             push_wf_row, _kill_rtl_sdr, _rtl_tcp_connect,
                             _spawn_rtl_sdr, CAPTURE_BATCH, IQ_BLOCK,
                             FFT_SIZE, FFT_EVERY,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]

    def reception_off():
        others = [d for d, e in state.sdrs.items()
                  if d != did and e.get('ism')
                  and not e.get('closed') and not e.get('paused')]
        with state.ism_lock:
            if others:
                state.ism_enabled = True
                state.ism_dongle = others[0]
                return
            state.ism_enabled = False
            state.ism_dongle = None

    with state.ism_lock:
        state.ism_enabled = True
        state.ism_dongle = did
    state.log_console(f"🌡 ISM receiver on dongle {did}: "
                      f"{ISM_CENTER_HZ / 1e6:.3f} MHz — decoding 433 MHz "
                      f"sensor telemetry (Nexus/Rubicson family)")
    dec = OokDecoder()
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("ism") or entry.get("paused"):
            reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            from ..calibration import tuning_correction, correction_info
            correction = tuning_correction(ISM_CENTER_HZ, did)
            tune = ISM_CENTER_HZ + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = \
                correction_info(ISM_CENTER_HZ, did)
            gain_db = dongle_gain_db(did)
            if entry.get("usb_idx") is not None:
                proc = _spawn_rtl_sdr(entry, tune, gain_db)
                if proc is None:
                    raise RuntimeError("rtl_sdr not available on this host")
                entry["proc"] = proc
                entry["connected"] = True
                entry["tuner"] = "USB (rtl_sdr)"
                entry["last_data"] = time.time()
                state.log_console(f"Connected to ISM USB dongle {did}, "
                                  f"tuned {tune}Hz")
                reader = _IQReader(_PipeRecv(proc.stdout), IQ_BLOCK)
            else:
                sock, tuner_type, _g = _rtl_tcp_connect(
                    entry["host"], entry["port"], gain_db)
                _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, tune)
                entry["sock"] = sock
                entry["connected"] = True
                entry["tuner"] = TUNER_NAMES.get(tuner_type,
                                                 f"rtl_tcp type {tuner_type}")
                entry["last_data"] = time.time()
                state.log_console(f"Connected to ISM rtl_tcp {did}, "
                                  f"tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, "
                                  f"correction {correction:+d}Hz)")
                reader = _IQReader(sock, IQ_BLOCK)
            from ..sdr.radio import FFT_SIZE, FFT_EVERY
            sig_bins = max(1, round(39375 / (SDR_RATE / FFT_SIZE)))
            # separate DSP state for the waterfall rows: the decoder's
            # state carries the demod's shift/LP phase continuity
            from ..sdr.dsp import new_state as _ns, iq_to_complex as _i2c, \
                frequency_shift as _fs
            wf_st = _ns()
            while True:
                if entry.get("closed") or not entry.get("ism"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (ISM dongle {did}),"
                                      " reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                now = time.time()
                # waterfall + signal for the dongle card
                c = _i2c(raw)
                c = _fs(c, SDR_OFFSET_HZ, SDR_RATE, wf_st)
                for k in range(0, len(c) // FFT_SIZE, FFT_EVERY):
                    try:
                        push_wf_row(entry, c, k, sig_bins)
                    except Exception:
                        pass
                for p in dec.feed(raw, now):
                    p["ts"] = round(now, 2)
                    p["freq_mhz"] = round(ISM_CENTER_HZ / 1e6, 4)
                    with state.ism_lock:
                        state.ism_packets.append(p)
                        _register(p)
                    _log_packet(p)
                    state.log_console(
                        f"🌡 ISM {p['freq_mhz']:.4f} MHz: {p['proto']} "
                        f"id {p['id']}"
                        + (f" ch{p['channel']}" if p.get("channel") else "")
                        + f" {p['temp_c']}°C"
                        + (f" {p['humidity']}%RH" if p.get("humidity") else "")
                        + (f" battery {p['battery']}" if p.get("battery") else ""))
        except Exception as e:
            if not entry.get("closed") and entry.get("ism") \
                    and not entry.get("paused"):
                state.log_console(f"ISM thread error (dongle {did}): {e}",
                                  "error")
        entry["connected"] = False
        entry["sock"] = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill_rtl_sdr(entry.pop("proc", None))
        if entry.get("closed") or not entry.get("ism") or entry.get("paused"):
            reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)
