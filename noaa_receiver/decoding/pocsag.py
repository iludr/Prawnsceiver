"""POCSAG pager protocol decoder (ITU-R R-REC-M.584 / ETSI ETS 300 401).

POCSAG rides direct 2-FSK (1200 baud everywhere in the European
466.0-466.3 MHz paging downlink) and frames pages in batches:

- preamble: >= 576 bits of alternating 1010... (receiver clock sync)
- batch: the 32-bit sync code 0x7CD215D8 followed by 16 codeframes -
  544 bits total (empirically confirmed on a live 466.2 MHz paging
  channel 2026-10-16: sync words every 544 bits; the 16 codeframes
  form the spec's 8 FRAMES of two codeframes each)
- codeframe: 32 bits = flag(1) + payload(20) + BCH(31,21) check(10) +
  even parity(1); g(x) = x^10+x^9+x^8+x^6+x^5+x^3+1 (0x769)
- address codeframe: flag 1, payload = 18 address bits + 2 function
  bits; the addressed frame pair = address & 7, so an address lands
  in codeframe 2*(address & 7) of a batch
- message codeframe: flag 0, payload = data; a message continues
  across codeframes and batches until the next address or the idle
  codeframe 0x7A89C197 (padding)
- numeric messages: 5 chars x 4 bits per codeframe; alphanumeric:
  7-bit ASCII packed LSB-first, continuously across codeframes

Used by the recording pre-flight (analysis/iqscan.py): every narrowband
non-OOK emitter gets a decode attempt - the continuously-keyed idle
code identifies a paging channel even when nobody is being paged.
Pages report 'addr' (18 address bits), 'func' (0 numeric, 1/2 tone,
3 alpha) and 'ric' (the usually-quoted pager identity,
addr*4 + func).
"""
import json

import numpy as np

SC = 0x7CD215D8
IDLE = 0x7A89C197
_BCH_G = 0x769                      # x^10+x^9+x^8+x^6+x^5+x^3+1
_SC_BITS = np.array([int(c) for c in format(SC, '032b')], dtype=np.int8)
_NUM = '0123456789U -][.'           # numeric charset (15 = reserved)
MAX_SECS = 30                       # decode at most this much per band


def _bch_remainder(w31):
    """Remainder of a 31-bit word mod g(x); 0 = valid codeword."""
    for i in range(30, 9, -1):
        if (w31 >> i) & 1:
            w31 ^= _BCH_G << (i - 10)
    return w31


def encode_word(flag, payload20):
    """Build one 32-bit codeframe: BCH(31,21)-encode the 21-bit
    flag+payload, append even parity."""
    msg21 = ((flag & 1) << 20) | (payload20 & 0xFFFFF)
    w = msg21 << 10
    for i in range(30, 9, -1):
        if (w >> i) & 1:
            w ^= _BCH_G << (i - 10)
    word31 = (msg21 << 10) | (w & 0x3FF)
    return (word31 << 1) | (bin(word31).count('1') & 1)


def correct_word(word32):
    """BCH-verify one 32-bit codeframe and correct up to 2 flipped
    bits (brute force over 1- and 2-bit patterns - exact for the
    code's 2-bit correction radius). None when uncorrectable."""
    w31 = word32 >> 1
    if _bch_remainder(w31) == 0:
        return (w31 << 1) | (bin(w31).count('1') & 1)
    for i in range(31):
        w = w31 ^ (1 << i)
        if _bch_remainder(w) == 0:
            return (w << 1) | (bin(w).count('1') & 1)
    for i in range(30):
        for j in range(i + 1, 31):
            w = w31 ^ (1 << i) ^ (1 << j)
            if _bch_remainder(w) == 0:
                return (w << 1) | (bin(w).count('1') & 1)
    return None


def _decode_numeric(payload20):
    """5 chars x 4 bits, first char in the highest nibble."""
    return ''.join(_NUM[(payload20 >> (16 - 4 * k)) & 0xF] for k in range(5))


def _finish_alpha(alpha_words):
    """7-bit ASCII packed LSB-first across codeframes; drop padding
    and non-printables."""
    stream = ''.join(alpha_words)
    out = []
    for i in range(0, len(stream) - 6, 7):
        ch = sum(int(stream[i + j]) << j for j in range(7))
        if 32 <= ch < 127:
            out.append(chr(ch))
    return ''.join(out).rstrip()


def decode_frames(bitstr, sc_positions):
    """Decode POCSAG batches from a symbol-aligned bit string
    (MSB-first words, one polarity already chosen) at the given sync
    code positions. Returns (pages, stats)."""
    pages, stats = [], {'batches': 0, 'idle_words': 0, 'bad_words': 0}
    cur, alpha = None, []

    def finish():
        if cur is not None:
            if cur['type'] == 'alpha' and alpha:
                cur['text'] = _finish_alpha(alpha)
            pages.append(cur)

    n = len(bitstr)
    last = -1
    for i in sorted(sc_positions):
        if i <= last:                       # overlapping detection
            continue
        stats['batches'] += 1
        w = i + 32
        end = min(w + 16 * 32, n)
        while w + 32 <= end:
            word = int(bitstr[w:w + 32], 2)
            w += 32
            cw = correct_word(word)
            if cw is None:
                stats['bad_words'] += 1
                finish()
                cur, alpha = None, []
                continue
            if cw == IDLE:
                stats['idle_words'] += 1
                finish()
                cur, alpha = None, []
                continue
            payload = (cw >> 11) & 0xFFFFF
            if (cw >> 31) & 1:              # address codeframe
                finish()
                addr, func = payload >> 2, payload & 3
                cur = {'addr': addr, 'func': func,
                       'ric': (addr << 2) | func,
                       'type': {0: 'numeric', 1: 'tone', 2: 'tone',
                                3: 'alpha'}.get(func, 'numeric'),
                       'text': ''}
                alpha = []
            elif cur is not None:           # message data
                if cur['type'] == 'numeric':
                    cur['text'] += _decode_numeric(payload)
                else:
                    alpha.append(format(payload, '020b'))
        last = i + 32                       # next SC must be past this
    finish()
    # exact-duplicate pages (overlapping batches) collapse
    seen, out = set(), []
    for p in pages:
        key = (p['ric'], p['text'])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out, stats


def _find_sc(bits, maxerr=2):
    """Positions where the sync code matches within maxerr bits."""
    b = (bits * 2 - 1).astype(np.int8)
    corr = np.correlate(b, _SC_BITS * 2 - 1, 'valid')
    return np.where((32 - corr) // 2 <= maxerr)[0]


def decode_band(getc, n_cplx, offset_hz, fs, baud=1200):
    """Attempt a POCSAG decode of one narrowband emitter's band from
    the raw IQ stream (getc memmap interface, analysis/iqscan.py).

    FM-discriminates the band (chunked, decimated to ~19200 Hz), tries
    both FSK polarities and all symbol phases, locks on the sync code
    and decodes the batches. Returns a JSON-ready dict when the
    protocol is detected, None when no sync code locks."""
    if baud != 1200:
        return None
    m = fs // 19200
    if m < 1:
        return None
    sps = fs / m / baud
    if sps < 4:
        return None
    sps = int(round(sps))
    # Channel selectivity FIRST: a wideband FM discriminator would ride
    # whatever signal is strongest anywhere in the capture (the live
    # 466 MHz recording has a +2 dBFS paging channel that bled into
    # every offset). Low-pass +-25 kHz around the shift before
    # discriminating.
    taps = np.sinc(2 * 25_000 / fs * (np.arange(33) - 16)) * np.hamming(33)
    taps /= taps.sum()
    chunks = []
    k = 0
    chunk = m * 4000                       # exactly 4000 decimated samples
    while k + chunk + 1 <= n_cplx \
            and len(chunks) * 4000 < 19200 * MAX_SECS:
        x = getc(k, chunk + 1)
        n0 = np.arange(k, k + chunk + 1, dtype=np.float64)
        bb = x * np.exp(-2j * np.pi * offset_hz * n0 / fs)
        bb = np.convolve(bb, taps, 'same')
        d = np.angle(bb[1:] * np.conj(bb[:-1])) * fs / (2 * np.pi)
        usable = (len(d) // m) * m
        chunks.append(d[:usable].reshape(-1, m).mean(axis=1))
        k += chunk
    if not chunks:
        return None
    f = np.concatenate(chunks)
    r = _lock_and_decode(f)
    if r is None:
        return None
    pages, stats = r
    out = {'baud': baud,
           'batches': stats['batches'],
           'idle_words': stats['idle_words'],
           'bad_words': stats['bad_words'],
           'pages': pages[:20],
           'note': ("POCSAG paging channel (sync code locked; idle words "
                    "mean nobody was paged during the capture). 'ric' is "
                    "the usually-quoted pager identity (address*4 + "
                    "function).")}
    return out


# ---------- live band decoder + capture thread (dongle role) ----------
# A dongle switched to the POCSAG role (dashboard button) tunes to
# config.PAGING_CENTER_HZ - one 2.4 MHz capture covers the whole local
# paging ecosystem (observed 2026-10-16: channels from 465.69 to
# 466.25) - and decodes EVERY paging channel it finds there in
# parallel: a light discovery FFT tracks the narrowband channels, each
# gets a channelizer demodulator (aprs.py-style polyphase FIR, ~0.5 ms
# per channel per 13.6 ms batch) and a rolling-window POCSAG decoder
# shared with the recording pre-flight.

from collections import deque
from datetime import datetime
import threading
import time

from .. import db
from .. import state
from ..calibration import (SDR_DONGLE_GAIN, correction_info,
                            tuning_correction)
from ..config import (FFT_SIZE, PAGING_CENTER_HZ, POCSAG_LOG_KEEP_HOURS,
                      SDR_GAIN, SDR_OFFSET_HZ, SDR_RATE)

PAGING_CHANNELS_MAX = 8           # demod slots (CPU budget per batch)
_CHANNEL_TIMEOUT = 30.0           # forget a channel quiet this long
_DECODE_EVERY = 1.2               # per-channel decode cadence, seconds
_FREQ_KEEP = int(19200 * 6)       # rolling discriminator buffer, 6 s
_DISCOVERY_EVERY = 1.0            # channel-scan cadence, seconds
_LO_SKIP_HZ = 15_000              # raw-DC LO spike is not a channel
_PAGE_DEDUPE_SECS = 60.0          # same RIC+text within this: duplicate
_SPS = 16                         # 19200 Hz / 1200 baud samples/symbol
_WF_EVERY = 8                     # waterfall row every 8th FFT block

_log_lock = threading.Lock()
_log_appends = 0


def _lock_and_decode(freq):
    """Slice a 19200 Hz discriminator stream into bits (both FSK
    polarities, all symbol phases), lock on the sync code and decode
    the batches. Returns (pages, stats) or None when nothing locks."""
    freq = np.asarray(freq, dtype=np.float64)
    freq = freq - np.median(freq)
    best = None
    for inv in (0, 1):
        s = (freq > 0).astype(np.int8) if not inv \
            else (freq < 0).astype(np.int8)
        for ph in range(_SPS):
            bits = s[ph::_SPS]
            if len(bits) < 700:
                continue
            scpos = _find_sc(bits)
            if len(scpos) and (best is None or len(scpos) > len(best[2])):
                best = (inv, ph, scpos, bits)
    if best is None:
        return None
    inv, ph, scpos, bits = best
    bitstr = ''.join('1' if v else '0' for v in bits)
    pages, stats = decode_frames(bitstr, list(scpos))
    stats['inverted'] = bool(inv)
    return pages, stats


class BandDecoder:
    """Every POCSAG channel of one 2.4 MHz capture, decoded in parallel
    (the dongle role's DSP core - fed whole IQ batches by
    pocsag_capture_thread, testable standalone)."""

    def __init__(self, did, tuner_hz, fs):
        self.did = did
        self.tuner_hz = float(tuner_hz)     # raw-stream center (abs RF)
        self.fs = fs
        self.dec = fs // 19200            # 125 @ 2.4 MHz
        # Two-stage decimation (the orbcomm.py trick): a 610-tap
        # sinc FIR cannot sharpen below a ~4 kHz transition band,
        # so a one-shot 125x drop to 19.2 kHz put the 9.6 kHz
        # Nyquist inside the transition band -> full-scale
        # discriminator aliasing (caught by the two-channel
        # BandDecoder test). Stage 1: FIR decimate by 25 ->
        # 96 kHz (transition band fits under the 48 kHz Nyquist),
        # discriminate there. Stage 2: boxcar-average 5 ->
        # 19200 Hz for the 16-sps slicer. The 7 kHz cutoff passes
        # the POCSAG +-4.5 kHz deviation with margin.
        self.dec1 = max(1, fs // 96000)
        while self.dec % self.dec1:
            self.dec1 -= 1
        self.dec2 = self.dec // self.dec1
        cutoff = 7000.0
        taps = (np.sinc(2 * cutoff / fs     # FM discriminator
                        * (np.arange(610) - 304.5)) * np.hamming(610))
        self.taps_c = (taps / taps.sum()).astype(np.complex64)
        self.floor = None
        self.seen = {}                       # (ric, text) -> emitted at
        self.channels = {}                  # raw offset Hz -> state
        self.last_discovery = -1e6

    def feed(self, c, now=None):
        """One complex IQ batch (raw stream) -> list of new pages."""
        now = time.time() if now is None else now
        pages = []
        if now - self.last_discovery >= _DISCOVERY_EVERY:
            self.last_discovery = now
            self._discover(c, now)
        for off in list(self.channels):
            st = self.channels[off]
            self._demod(st, c)
            if now - st["last_decode"] >= _DECODE_EVERY:
                st["last_decode"] = now
                pages.extend(self._decode(st, now))
            if now - st["last_seen"] > _CHANNEL_TIMEOUT:
                del self.channels[off]
        return pages

    def channel_status(self):
        """Snapshot of the tracked channels for /pocsag.json."""
        now = time.time()
        out = {}
        for st in self.channels.values():
            abs_hz = self.tuner_hz + st["off_hz"]
            out[round(abs_hz / 1e3)] = {
                "freq_hz": round(abs_hz),
                "freq_mhz": round(abs_hz / 1e6, 5),
                "level_dbfs": round(st["level"], 1),
                "last_seen_age_s": round(now - st["last_seen"], 1),
                "pages": st["pages"],
            }
        return out

    def _discover(self, c, now):
        """One 4096-point FFT finds the narrowband channels: clusters
        over the EMA floor, wide clusters and the dongle's own LO spike
        (raw DC) excluded."""
        n = min(4096, len(c))
        win = np.hanning(n)
        mag = np.abs(np.fft.fftshift(np.fft.fft(c[:n] * win)))
        med = float(np.median(mag))
        self.floor = med if self.floor is None \
            else self.floor + 0.1 * (med - self.floor)
        bin_hz = self.fs / n
        # 12x the median (21.6 dB) keeps the -30 dBFS telemetry
        # crowd (18-21 dB over the floor) out of the demod slots;
        # real paging transmitters run 25+ dB over the floor
        hot = mag > self.floor * 12.0
        dil = hot.copy()
        for d in range(1, 3):
            dil[d:] |= hot[:-d]
            dil[:-d] |= hot[d:]
        full_scale = 0.25 * n * 127.5
        i = 0
        while i < n:
            if not dil[i]:
                i += 1
                continue
            j = i
            while j < n and dil[j]:
                j += 1
            pk = i + int(np.argmax(mag[i:j]))
            off = (pk - n // 2) * bin_hz
            width = (j - i) * bin_hz
            level = 20.0 * np.log10(mag[pk] / full_scale + 1e-9)
            if abs(off) > _LO_SKIP_HZ and width < 80_000:
                self._track(off, level, now)
            i = j

    def _track(self, off, level, now):
        """Merge a detection into a tracked channel (6.25 kHz, half
        the 12.5 kHz channel grid - covers both FSK tones of one
        channel) or open a new demod slot while one is free."""
        for o, st in self.channels.items():
            if abs(st["off_hz"] - off) < 6250.0:
                st["off_hz"] += 0.25 * (off - st["off_hz"])
                st["last_seen"] = now
                st["level"] = level
                return
        if len(self.channels) >= PAGING_CHANNELS_MAX:
            return
        self.channels[off] = {
            "off_hz": off,
            "rot": 0.0,                    # per-channel NCO phase
            "fir_tail": np.zeros(609, dtype=np.complex64),
            "dec_pos": 0,
            "last_c": None,
            "acc": 0.0,                    # stage-2 boxcar sum
            "acc_n": 0,
            "freq": deque(maxlen=_FREQ_KEEP),
            "last_seen": now,
            "level": level,
            "last_decode": now,
            "pages": 0,
        }
        state.log_console(f"📟 POCSAG: new channel at "
                          f"{(self.tuner_hz + off) / 1e6:.4f} MHz "
                          f"({level:.0f} dBFS) — decoding")

    def _demod(self, st, c):
        """One batch through one channel: rotate to 0 Hz, polyphase
        channel FIR (decimated output positions only, the aprs.py
        trick), FM discriminate, append to the rolling buffer."""
        n = np.arange(len(c), dtype=np.float64)
        slope = 2.0 * np.pi * (st["off_hz"] / self.fs)
        ph = st["rot"] + slope * n
        # next batch starts one sample after ph[-1]
        st["rot"] = float((ph[-1] + slope) % (2.0 * np.pi)) \
            if len(ph) else st["rot"]
        bb = c * np.exp(-1j * ph)
        x = np.concatenate([st["fir_tail"], bb])
        st["fir_tail"] = x[-609:].copy()
        m = len(x) - 610 + 1
        if m <= 0:
            return
        start = (-st["dec_pos"]) % self.dec1
        st["dec_pos"] = (st["dec_pos"] + m) % self.dec1
        f = (np.lib.stride_tricks.sliding_window_view(x, 610)
             [start::self.dec1] @ self.taps_c)
        if st["last_c"] is not None:
            dd = f * np.conj(np.concatenate([[st["last_c"]], f[:-1]]))
        else:
            dd = f[1:] * np.conj(f[:-1])
        if len(f):
            st["last_c"] = f[-1].copy()
        d = np.angle(dd) * (self.fs / self.dec1 / (2 * np.pi))
        # stage 2: average groups of dec2 discriminator samples
        # into the 19200 Hz slicer stream; a partial group at the
        # batch tail carries over in (acc, acc_n)
        if st["acc_n"]:
            need = self.dec2 - st["acc_n"]
            if len(d) >= need:
                st["freq"].append((st["acc"] + float(np.sum(d[:need])))
                                / self.dec2)
                d = d[need:]
                st["acc"] = 0.0
                st["acc_n"] = 0
            else:
                st["acc"] += float(np.sum(d))
                st["acc_n"] += len(d)
                return
        trim = len(d) % self.dec2
        if trim:
            st["acc"] = float(np.sum(d[len(d) - trim:]))
            st["acc_n"] = trim
            d = d[:len(d) - trim]
        if len(d):
            g = d.reshape(-1, self.dec2).mean(axis=1)
            st["freq"].extend(g.tolist())

    def _decode(self, st, now):
        """Lock + decode the rolling window; dedupe repeated pages."""
        if len(st["freq"]) < 19200 * 2:       # need >= 2 s
            return []
        r = _lock_and_decode(np.asarray(st["freq"]))
        if r is None:
            return []
        pages, _stats = r
        out = []
        for p in pages:
            key = (p["ric"], p["text"])
            seen_at = self.seen.get(key)
            if seen_at is not None and \
                    now - seen_at < _PAGE_DEDUPE_SECS:
                continue
            self.seen[key] = now
            st["pages"] += 1
            p["ts"] = round(now, 2)
            p["time"] = datetime.now().strftime("%H:%M:%S")
            p["channel_mhz"] = round((self.tuner_hz + st["off_hz"]) / 1e6, 5)
            out.append(p)
        for k in [k for k, t in self.seen.items()
                  if now - t > _PAGE_DEDUPE_SECS * 2]:
            del self.seen[k]
        return out


def _log_page(entry):
    """Append one decoded page to the station database (pocsag_messages);
    never fatal. Trimmed to POCSAG_LOG_KEEP_HOURS like the other logs."""
    global _log_appends
    with _log_lock:
        try:
            with db.write() as cur:
                cur.execute("INSERT INTO pocsag_messages (ts, entry) VALUES (?,?)",
                            (entry.get("ts", time.time()),
                             json.dumps(entry, separators=(",", ":"))))
            _log_appends += 1
            if _log_appends % 256 == 0:
                cutoff = time.time() - POCSAG_LOG_KEEP_HOURS * 3600
                cur.execute("DELETE FROM pocsag_messages WHERE ts < ?",
                            (cutoff,))
        except Exception as e:
            state.log_console(f"POCSAG log write failed: {e}", "warn")


def pocsag_log(count=200):
    """Newest-first tail of the persistent page log for /pocsaglog.json."""
    try:
        count = max(1, min(int(count), 1000))
    except ValueError:
        count = 200
    out = []
    for r in db.query("SELECT entry FROM pocsag_messages"
                      " ORDER BY id DESC LIMIT ?", (count,)):
        try:
            out.append(json.loads(r["entry"]))
        except ValueError:
            continue
    return out


def load_pages():
    """Startup: restore the newest pages into the live ring (the log
    itself stays in the database) + a console summary."""
    try:
        rows = db.query("SELECT entry FROM pocsag_messages"
                        " ORDER BY id DESC LIMIT 100")
        with state.pocsag_lock:
            for r in reversed(rows):
                try:
                    state.pocsag_pages.append(json.loads(r["entry"]))
                except ValueError:
                    continue
        n = db.query("SELECT COUNT(*) AS n FROM pocsag_messages")[0]["n"]
        state.log_console(f"📟 POCSAG page log: {n} pages on record (last "
                          f"{POCSAG_LOG_KEEP_HOURS} h)")
    except Exception:
        pass


def pocsag_status():
    """Snapshot for /pocsag.json: channels, recent pages, stats."""
    with state.pocsag_lock:
        channels = sorted(state.pocsag_channels.values(),
                         key=lambda ch: -ch.get("level_dbfs", 0))
        pages = list(state.pocsag_pages)
        enabled = state.pocsag_enabled
        serial = state.pocsag_dongle
    return {"enabled": enabled, "dongle": serial,
            "freq_mhz": round(PAGING_CENTER_HZ / 1e6, 3),
            "channels": channels, "pages": pages}


def pocsag_capture_thread(did):
    """rtl_tcp/rtl_sdr connection + whole-band POCSAG decoding for a
    dongle switched to the paging role. Mirrors the Orbcomm capture
    thread's connection lifecycle (handshake, gain, reconnect with
    backoff, role handover); instead of satellite carriers it tracks
    every paging channel in the 2.4 MHz capture."""
    from ..sdr.dsp import iq_to_complex
    from ..sdr.radio import (_IQReader, _PipeRecv, push_wf_row,
                             _kill_rtl_sdr, _rtl_tcp_connect, _spawn_rtl_sdr,
                             CAPTURE_BATCH, IQ_BLOCK,
                             RTL_TCP_SET_FREQ, TUNER_NAMES, _rtl_tcp_set)

    entry = state.sdrs[did]

    def reception_off():
        others = [d for d, e in state.sdrs.items()
                  if d != did and e.get('pocsag')
                  and not e.get('closed') and not e.get('paused')]
        with state.pocsag_lock:
            if others:
                state.pocsag_enabled = True
                state.pocsag_dongle = others[0]
                return
            state.pocsag_enabled = False
            state.pocsag_dongle = None

    with state.pocsag_lock:
        state.pocsag_enabled = True
        state.pocsag_dongle = did
        state.pocsag_channels = {}
    state.log_console(f"📟 POCSAG receiver on dongle {did}: "
                      f"{PAGING_CENTER_HZ / 1e6:.3f} MHz "
                      f"(±1.2 MHz — every paging channel decoded)")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("pocsag") or entry.get("paused"):
            reception_off()
            return
        run_started = time.time()
        sock = None
        try:
            correction = tuning_correction(PAGING_CENTER_HZ, did)
            tune = PAGING_CENTER_HZ + SDR_OFFSET_HZ + correction
            entry["correction"], entry["correction_src"] = \
                correction_info(PAGING_CENTER_HZ, did)
            gain_db = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
            if entry.get("usb_idx") is not None:
                proc = _spawn_rtl_sdr(entry, tune, gain_db)
                if proc is None:
                    raise RuntimeError("rtl_sdr not available on this host")
                entry["proc"] = proc
                entry["connected"] = True
                entry["tuner"] = "USB (rtl_sdr)"
                entry["last_data"] = time.time()
                state.log_console(f"Connected to POCSAG USB dongle {did} "
                                  f"(device {entry['usb_idx']}), tuned "
                                  f"{tune}Hz")
                reader = _IQReader(_PipeRecv(proc.stdout), IQ_BLOCK)
            else:
                sock, tuner_type, _gain_count = _rtl_tcp_connect(
                    entry["host"], entry["port"], gain_db)
                _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, tune)
                entry["sock"] = sock
                entry["connected"] = True
                entry["tuner"] = TUNER_NAMES.get(tuner_type,
                                                 f"rtl_tcp type {tuner_type}")
                entry["last_data"] = time.time()
                state.log_console(f"Connected to POCSAG rtl_tcp {did}, "
                                  f"tuned {tune}Hz "
                                  f"(offset +{SDR_OFFSET_HZ + correction}Hz, "
                                  f"correction {correction:+d}Hz)")
                reader = _IQReader(sock, IQ_BLOCK)
            dec = BandDecoder(did, tune, SDR_RATE)
            sig_bins = max(1, round(39375 / (SDR_RATE / FFT_SIZE)))
            while True:
                if entry.get("closed") or not entry.get("pocsag"):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (POCSAG dongle "
                                      f"{did}), reconnecting...", "warn")
                    break
                entry["last_data"] = time.time()
                c = iq_to_complex(raw)
                # waterfall + signal for the dongle card
                # (see push_wf_row)
                for k in range(0, len(c) // FFT_SIZE, _WF_EVERY):
                    try:
                        push_wf_row(entry, c, k, sig_bins)
                    except Exception:
                        pass
                pages = dec.feed(c)
                if pages:
                    with state.pocsag_lock:
                        for p in pages:
                            state.pocsag_pages.append(p)
                    for p in pages:
                        _log_page(p)
                        state.log_console(
                            f"📟 POCSAG {p['channel_mhz']:.4f} MHz: "
                            f"RIC {p['ric']} ({p['type']}"
                            + (f") {p['text'][:60]}" if p['text'] else ")"))
                with state.pocsag_lock:
                    state.pocsag_channels = dec.channel_status()
        except Exception as e:
            if not entry.get("closed") and entry.get("pocsag") \
                    and not entry.get("paused"):
                state.log_console(f"POCSAG thread error (dongle {did}): {e}",
                                  "error")
        entry["connected"] = False
        entry["sock"] = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill_rtl_sdr(entry.pop("proc", None))
        if entry.get("closed") or not entry.get("pocsag") or entry.get("paused"):
            reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)
