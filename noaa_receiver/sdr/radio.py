"""SDR threads: per-dongle rtl_tcp capture, waterfall FFT, FM demod, WAV recording.

All attached dongles run on the same frequency with the same settings (they
re-tune together via state.current_frequency). A dongle is a REMOTE rtl_tcp
server identified by "host:port" — e.g. the prawntenna dongle manager
publishing one rtl_tcp server per dongle (the serving host pins
which physical dongle answers on which port). A dongle plugged into
the receiver host itself ("usb:<index>") needs no daemon: the capture
threads read it directly with an rtl_sdr child process (raw u8 IQ on
stdout, same sample format — see _spawn_rtl_sdr). Dongles are
added/removed at runtime from the dashboard (radio.add_dongle /
remove_dongle, persisted in the station database).

Each dongle's capture thread reads the IQ stream from its socket and
retunes by sending rtl_tcp frequency commands — the stream keeps flowing
while the tuner re-locks, so a frequency switch takes milliseconds and
does not restart anything, gap the audio, or split the recording. The
configured primary dongle feeds the live audio stream and records WAVs
during passes. Every dongle gets its own waterfall buffer and signal
strength, so receive quality can be compared on the dashboard.
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import wave
import zipfile
from collections import deque
from datetime import datetime, timezone

from .. import db
from .. import state
from ..calibration import (AIRBAND, AIS_DONGLE, DEFAULT_DONGLES, FM_BAND, PRIMARY_DONGLE,
                          SDR_DONGLE_GAIN, correction_info, tuning_correction)
from ..config import (AUDIO_RATE, DOPPLER_APPLY_RANGE_HZ, DECIMATION, DONGLES_FILE,
                     FFT_SIZE, IQ_BLOCK, IQ_RECORD_FREQS, LAT, LOGDIR, LON,
                     MANUAL_REC_MAX_SECS, RECORD_DIR, SDR_GAIN, SDR_OFFSET_HZ,
                     SDR_RATE, WATERFALL_ROWS)

from ..decoding.decode import sat_short_name
from .dsp import (am_demodulate, doppler_shift, fm_demodulate, frequency_shift,
                  iq_to_complex, new_state, ssb_demodulate)

# Waterfall/signal FFT cadence: compute the FFT only every Nth IQ block. The
# dashboard draws ~5 rows/s, so ~234 rows/s (960 kHz: 1875 blocks/s / 8) is still 40x
# oversampled. At full block rate the FFT + row conversion (~1 ms/block on the
# Pi) starved the demod once two dongles were attached.
FFT_EVERY = max(8, round(0.00427 * SDR_RATE / (IQ_BLOCK // 2)))  # ~4 ms of RF between FFTs at any SDR_RATE (8 blocks at 960 kHz)
# Blocks of IQ processed per capture-loop iteration (both capture
# threads). Per-block processing at 2.4 Msps meant ~25 numpy calls
# 4700 times a second per dongle; two dongles saturated the GIL
# (~80% of a core each) and each capture thread reached only
# ~65-72% of real time - the audio rings under-delivered and
# live-edge listeners heard constant stutter (2026-10-11, the
# 'regular audio hangs'). 64 blocks = one 65536-byte pipe chunk =
# ~13.6 ms of RF at 2.4 Msps; the DSP states carry phase/filter
# tails across calls of ANY length, so batching is bit-transparent
# to every downstream demod.
CAPTURE_BATCH = 64

# Retry interval for a WAV that could not be opened/written (disk full etc.)
WAV_RETRY_SECS = 10.0

_fft_window = None

def _get_fft_window():
    """FFT_SIZE-point Hamming window, computed once."""
    global _fft_window
    if _fft_window is None:
        import numpy as np
        _fft_window = np.hamming(FFT_SIZE)
    return _fft_window

# ---------- rtl_tcp control protocol ----------
# rtl_tcp exposes a dongle over TCP: a continuous IQ stream plus a tiny
# command channel (1-byte command + 4-byte big-endian value). Frequency
# changes apply while the stream keeps flowing, so a retune is one socket
# write instead of a kill-restart-sleep cycle (~8 s of dead air).
RTL_TCP_SET_FREQ = 0x01
RTL_TCP_SET_SAMPLE_RATE = 0x02
RTL_TCP_SET_GAIN_MODE = 0x03   # 0 = tuner AGC, 1 = manual
RTL_TCP_SET_GAIN = 0x04        # value = gain in tenths of dB

# Tuner type ids from the rtl_tcp handshake (librtlsdr), for the dongle
# cards' labels. Unknown types fall back to a numeric label.
TUNER_NAMES = {
    1: "E4000", 2: "FC0013", 3: "FC0012", 4: "FC2520",
    5: "R820T", 6: "R828D",
}

def _rtl_tcp_set(sock, cmd, value):
    """Send one rtl_tcp control command: 1 byte command + uint32 big-endian
    ('!BI' is network byte order, standard sizes, no padding: 5 bytes)."""
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f'rtl_tcp command value out of range: {value}')
    sock.sendall(struct.pack('!BI', cmd, value))

def _recv_exact(sock, n):
    """Read exactly n bytes from the socket; None when the stream ends."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def _rtl_tcp_connect(host, port, gain_db):
    """Open one rtl_tcp connection: connect, verify the 'RTL0' handshake,
    set the sample rate (always) and a fixed manual gain (when configured)
    over the control protocol — the rtl_tcp CLI parses -g as an int, so
    fractional gain steps like the R820T's 29.7 dB must go through the
    socket.

    Returns (socket, tuner_type, gain_count); the IQ stream starts flowing
    immediately (the caller reads it via _IQReader).
    """
    sock = socket.create_connection((host, int(port)), timeout=10)
    # The IQ stream delivers a block every ~2 ms; 30 s of silence is a dead
    # stream (daemon wedged, network drop) -> the capture loop reconnects.
    sock.settimeout(30)
    header = _recv_exact(sock, 12)
    if header is None or header[:4] != b'RTL0':
        sock.close()
        raise RuntimeError('bad rtl_tcp handshake (not an rtl_tcp server?)')
    tuner_type, gain_count = struct.unpack('!II', header[4:12])
    # Explicit sample rate on every connect: a daemon spawned without -s
    # serves rtl_tcp's default (~2.4 MHz) while every demodulator here
    # needs SDR_RATE. The AIS dongle's daemon had no -s and the demod
    # silently processed a ~10x-rate stream: waterfall alive, zero
    # decodes, on every antenna and gain (2026-10-10).
    _rtl_tcp_set(sock, RTL_TCP_SET_SAMPLE_RATE, SDR_RATE)
    if gain_db:
        _rtl_tcp_set(sock, RTL_TCP_SET_GAIN_MODE, 1)
        _rtl_tcp_set(sock, RTL_TCP_SET_GAIN, int(round(gain_db * 10)))
    return sock, tuner_type, gain_count

class _IQReader:
    """Block reader for the rtl_tcp IQ stream.

    One recv() takes whatever has arrived (up to `chunk` bytes — it does
    not wait for the chunk to fill, so no latency is added) and the capture
    loop slices IQ_BLOCK-sized blocks off it: ~16x fewer syscalls than one
    recv() per 1 kB block. read_block() returns None when the stream ends.
    """
    def __init__(self, sock, block, chunk=65536):
        self.sock, self.block, self.chunk = sock, block, chunk
        self.buf, self.pos = b'', 0

    def read_block(self):
        buf, pos, block = self.buf, self.pos, self.block
        if len(buf) - pos < block:
            buf = buf[pos:]
            while len(buf) < block:
                data = self.sock.recv(self.chunk)
                if not data:
                    return None
                buf += data
            pos = 0
            self.buf = buf
        self.pos = pos + block
        return buf[pos:pos + block]

    def read_batch(self, count):
        """Concatenated bytes of `count` blocks (same None-at-EOF
        contract as read_block): one iteration's worth of the batched
        capture loop - one pipe read fills it, no extra latency."""
        buf, pos, block = self.buf, self.pos, self.block
        need = block * count
        if len(buf) - pos < need:
            buf = buf[pos:]
            while len(buf) < need:
                data = self.sock.recv(self.chunk)
                if not data:
                    return None
                buf += data
            pos = 0
            self.buf = buf
        self.pos = pos + need
        return buf[pos:pos + need]

class _PipeRecv:
    """Adapter that makes an rtl_sdr stdout pipe look like a socket for
    _IQReader (recv -> blocking read)."""
    def __init__(self, pipe):
        self.pipe = pipe

    def recv(self, n):
        return self.pipe.read(n)

def _spawn_rtl_sdr(entry, tune_hz, gain_db):
    """Start rtl_sdr for a USB dongle ("usb:<index>"): raw u8 IQ on
    stdout, the exact sample format rtl_tcp serves, so the whole
    capture chain (offset rotation, Doppler NCO, FFT, demod) is
    unchanged.

    The device is chosen by index (-d) and tuned with -f at the same
    offset+correction the rtl_tcp path applies; a fixed gain goes in
    tenths of dB, no -g keeps the tuner AGC. Buffer options are never
    touched (non-default -b/-n collapsed dongle throughput on the old
    Pi, measured 2026-10-10). rtl_sdr cannot be retuned while running
    — a band switch restarts the process (the capture loop treats it
    like any lost connection). Returns the Popen, or None when rtl_sdr
    is not installed / cannot start (warned once per dongle).
    """
    idx = entry.get('usb_idx')
    exe = shutil.which('rtl_sdr')
    if not exe:
        if not entry.get('usb_warned'):
            entry['usb_warned'] = True
            state.log_console(f"rtl_sdr not found on this host — cannot read USB dongle {idx}", "warn")
        return None
    argv = [exe, '-d', str(idx), '-f', str(int(tune_hz)), '-s', str(SDR_RATE)]
    if gain_db:
        argv += ['-g', str(int(round(gain_db * 10)))]
    argv += ['-']   # IQ to stdout
    try:
        return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
    except Exception as e:
        state.log_console(f"Cannot start rtl_sdr for USB dongle {idx}: {e}", "warn")
        return None

def _kill_rtl_sdr(proc):
    """Stop an rtl_sdr child for good. A bare terminate() is NOT enough:
    a healthy rtl_sdr exits on SIGTERM, but one blocked writing to a
    full pipe (its reader stalled or gone) never rechecks the exit
    flag and survives SIGTERM forever while holding the USB device -
    observed live 2026-10-11: the role-switched dongle stayed hostage
    to its old rtl_sdr until SIGKILL. Grace first (clean exit), then
    an untrappable SIGKILL; wait() also reaps the zombie.
    """
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def usb_dongles():
    """Enumerate the USB dongles on this host exactly the way rtl_sdr
    itself sees them (name + serial per device index): rtl_sdr is run
    with an out-of-range -d index, so it prints the "Found N device(s)"
    list and exits without touching any device. The indexes shown here
    are the very ones the capture threads use (rtl_sdr -d / "usb:<i>").
    Returns [] when rtl_sdr is missing or the list cannot be parsed.
    """
    exe = shutil.which('rtl_sdr')
    if not exe:
        return []
    try:
        out = subprocess.run([exe, '-d', '99', '-f', '100000000',
                              '-s', '240000', '-'],
                             capture_output=True, text=True, timeout=10)
        text = (out.stderr or '') + (out.stdout or '')
    except Exception:
        return []
    dongles = []
    for m in re.finditer(r'^\s*(\d+):\s*(.+),\s*SN:\s*(\S+)\s*$',
                         text, re.M):
        dongles.append({'index': int(m.group(1)),
                        'name': m.group(2).strip(),
                        'serial': m.group(3).strip()})
    return dongles


def _stop_capture(entry):
    """Close a dongle's IQ source under its capture thread (remove,
    pause, role switch): the rtl_tcp socket and/or the spawned
    rtl_sdr process."""
    sock = entry.get('sock')
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    _kill_rtl_sdr(entry.get('proc'))

# ---------- dongle registry (add/remove at runtime, persisted) ----------

def _dongle_id(host, port):
    """Dongle id: the rtl_tcp server address."""
    return f'{host}:{int(port)}'

def _persist_dongles():
    """Write the registered dongle list to the station database (dongles
    table), so a restart keeps the runtime-added set and each dongle's
    role.

    Never fatal: on a database error the receiver keeps running with
    the in-memory set (the dashboard shows a warning once).
    """
    try:
        with db.write() as cur:
            cur.execute("DELETE FROM dongles")
            for e in state.sdrs.values():
                if e.get('usb_idx') is not None:
                    # A USB dongle persists as its rtl_sdr device index,
                    # not a network address
                    host, port = 'usb', int(e['usb_idx'])
                else:
                    host, port = e['host'], int(e['port'])
                cur.execute("INSERT INTO dongles (host, port, ais, orbcomm,"
                            " aprs, aprs_iss, pocsag, added_ts)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (host, port,
                             1 if e.get('ais') else 0,
                             1 if e.get('orbcomm') else 0,
                             1 if e.get('aprs') else 0,
                             1 if e.get('aprs_iss') else 0,
                             1 if e.get('pocsag') else 0, time.time()))
    except Exception as e:
        state.log_console(f"Cannot persist dongle list: {e}", "warn")


def _new_entry(did, host, port, primary, usb_idx=None, usb_name=None,
               usb_serial=None):
    """Fresh state.sdrs entry for one dongle address (shared by load and
    add_dongle). The primary dongle aliases the legacy waterfall globals so
    the rest of the system (status, live audio, recording) keeps working."""
    if primary:
        waterfall, lock = state.waterfall_buffer, state.waterfall_lock
    else:
        waterfall = deque(maxlen=WATERFALL_ROWS)
        lock = threading.Lock()
    return {
        'id': did, 'host': host, 'port': int(port),
        'label': f'{host}:{port}', 'tuner': 'rtl_tcp', 'primary': primary,
        'ais': did == AIS_DONGLE, 'orbcomm': False, 'aprs': False,
        'aprs_iss': False, 'usb_idx': usb_idx, 'proc': None,
        'usb_name': usb_name, 'usb_serial': usb_serial,
        # dBFS spectrum rows for the WebSocket feed: int16 dB*10 per
        # bin, appended with the display row; wf_db_seq counts them
        'wf_db': deque(maxlen=512), 'wf_db_seq': 0,
        'paused': False,
        'waterfall': waterfall, 'lock': lock,
        'signal': 0.0, 'last_mag': None, 'connected': False,
        'sock': None, 'last_data': 0.0,
        # Live audio ring for this dongle's demodulated audio
        'la': {'data': [], 'base': 0, 'total': 0, 'cond': threading.Condition()},
        # WAV recording state (every dongle records its own file)
        'is_recording': False, 'wav': None, 'wav_path': None, 'iq': None,
    }

def add_dongle(host, port):
    """Register a dongle by its rtl_tcp server address ("host:port"), or a
    USB dongle plugged into the receiver host itself as ("usb", rtl_sdr
    device index): creates its state entry, starts its capture thread
    (via the supervisor loop) and persists the set. Idempotent: a known
    dongle is not added twice.

    Returns (did, created): the dongle id and whether it was newly added.
    """
    host = (host or '').strip().rstrip(':')
    usb_idx = usb_name = usb_serial = None
    if host.lower() == 'usb':
        # Local USB dongle: "port" is the rtl_sdr device index. The id
        # stays "usb:<index>" so calibration and pinning survive
        # restarts; the capture threads read it with rtl_sdr directly
        try:
            usb_idx = int(port)
        except (TypeError, ValueError):
            raise ValueError(f'invalid USB device index: {port!r}')
        if not 0 <= usb_idx <= 31:
            raise ValueError('USB device index must be 0-31')
        did = f'usb:{usb_idx}'
        host = 'usb'
        # Identity for the dashboard (waterfall titles, dongle cards):
        # name + serial as rtl_sdr itself reports them
        usb_name = usb_serial = None
        for u in usb_dongles():
            if u['index'] == usb_idx:
                usb_name, usb_serial = u['name'], u['serial']
                break
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError(f'invalid port: {port!r}')
        if not host or not 1 <= port <= 65535:
            raise ValueError('host must be non-empty and port in 1-65535')
        did = _dongle_id(host, port)
    if did in state.sdrs:
        return did, False
    with state.status_lock:
        # The primary dongle (live audio, WAV recording) is decided once, at
        # the first added non-AIS dongle: the configured PRIMARY_DONGLE when
        # it matches, else the first one added (the AIS dongle never becomes
        # primary)
        if state.primary_dongle is None and did != AIS_DONGLE:
            state.primary_dongle = did
            if did != PRIMARY_DONGLE and PRIMARY_DONGLE is not None:
                state.log_console(f"Configured primary dongle {PRIMARY_DONGLE} not added — using {did}", "warn")
        primary = (did == state.primary_dongle)
        state.sdrs[did] = _new_entry(did, host, port, primary, usb_idx,
                                    usb_name, usb_serial)
    if state.sdrs[did]['ais']:
        state.log_console(f"Dongle {did} dedicated to AIS (ship traffic) — excluded from satellite tracking")
    _name = f" ({usb_name}, SN {usb_serial})" if usb_idx is not None and usb_name else ""
    state.log_console(f"🔌 Dongle added: {did}{_name}" + (" — primary" if primary else ""))
    _persist_dongles()
    return did, True

def remove_dongle(did):
    """Unregister a dongle: its capture thread stops (the socket is closed
    under it, a running WAV is closed by the thread), its overrides and
    scan state are cleared, and the set is persisted. Unknown id: False."""
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    entry['closed'] = True
    _stop_capture(entry)
    from . import scan
    scan.stop_scan(did)
    was_primary = entry['primary']
    with state.status_lock:
        state.sdrs.pop(did, None)
        state.manual_dongle_freq.pop(did, None)
        state.manual_dongle_bw.pop(did, None)
        state.manual_recording.pop(did, None)
        state.scans.pop(did, None)
        # Promote the first remaining non-AIS dongle to primary (it takes
        # over the legacy waterfall globals); with no dongle left there is
        # no primary either
        if was_primary:
            nxt = next((d for d, e in state.sdrs.items()
                        if not e.get('ais')), None)
            state.primary_dongle = nxt
            if nxt is not None:
                e = state.sdrs[nxt]
                e['primary'] = True
                e['waterfall'] = state.waterfall_buffer
                e['lock'] = state.waterfall_lock
                state.log_console(f"Dongle {nxt} is now the primary (live audio, recordings)")
    state.log_console(f"🔌 Dongle removed: {did}")
    _persist_dongles()
    return True

def set_dongle_ais(did, on):
    """Switch one dongle between satellite tracking and AIS reception
    (dashboard button — with a single dongle this is how it listens for
    ship traffic without a dedicated second receiver).

    Flips the registry flag and closes the running capture thread's
    socket under it: the thread leaves its read loop, notices the flag no
    longer matches its role and exits; the supervisor loop (sdr_thread)
    then starts the capture thread for the new role within ~2 s. The
    dongle stays registered — its id, waterfall and card survive the
    switch. Unknown id: False; a no-op request still succeeds.
    """
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('ais')) == on:
        return True
    with state.status_lock:
        entry['ais'] = on
        if on:
            # one dongle, one dedicated role: switching to AIS ends an
            # Orbcomm/APRS session on the same dongle (and vice versa in
            # set_dongle_orbcomm / set_dongle_aprs)
            entry['orbcomm'] = False
            entry['aprs'] = False
            entry['pocsag'] = False
        if on:
            # An AIS capture thread neither records nor demodulates FM —
            # a manual recording left over from satellite mode would sit
            # as a dangling flag and resume the moment the dongle is
            # switched back, so it is dropped here instead
            state.manual_recording.pop(did, None)
    if on:
        state.log_console(f"🚢 Dongle {did} switched to AIS (161.975/162.025 MHz) — satellite reception on it paused")
    else:
        state.log_console(f"🛰 Dongle {did} switched back to satellite tracking")
    _persist_dongles()
    _stop_capture(entry)
    return True

def set_dongle_orbcomm(did, on):
    """Switch one dongle between satellite tracking and Orbcomm
    reception (dashboard button, mirroring set_dongle_ais): on=1 parks
    it on 137.5 MHz — one 2.4 MHz capture covers the whole Orbcomm
    downlink band — and every satellite carrier it finds there is
    demodulated (see noaa_receiver/orbcomm.py). The capture thread
    restarts in the new role within ~2 s; the dongle stays registered.
    Unknown id: False; a no-op request still succeeds.
    """
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('orbcomm')) == on:
        return True
    with state.status_lock:
        entry['orbcomm'] = on
        if on:
            entry['ais'] = False     # one dedicated role per dongle
            entry['aprs'] = False
            entry['pocsag'] = False
            state.manual_recording.pop(did, None)
    if on:
        state.log_console(f"🛰 Dongle {did} switched to Orbcomm (137.0-138.0 MHz) — satellite reception on it paused")
    else:
        state.log_console(f"🛰 Dongle {did} switched back to satellite tracking")
    _persist_dongles()
    _stop_capture(entry)
    return True

def set_dongle_aprs(did, on):
    """Switch one dongle between satellite tracking and the APRS role
    (dashboard button, mirroring set_dongle_ais): on=1 parks it on
    144.800 MHz (or 145.825 MHz, the ISS digipeater, when the dongle's
    aprs_iss flag is set — see set_dongle_aprs_iss) and decodes Bell 202
    AFSK AX.25 frames (see noaa_receiver/decoding/aprs.py). The capture
    thread restarts in the new role within ~2 s; the dongle stays
    registered. Unknown id: False; a no-op request still succeeds."""
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('aprs')) == on:
        return True
    with state.status_lock:
        entry['aprs'] = on
        if on:
            entry['ais'] = False     # one dedicated role per dongle
            entry['orbcomm'] = False
            entry['pocsag'] = False
            state.manual_recording.pop(did, None)
    if on:
        freq = ("145.825 MHz (ISS digipeater)" if entry.get('aprs_iss')
                else "144.800 MHz")
        state.log_console(f"📡 Dongle {did} switched to APRS ({freq}) — satellite reception on it paused")
    else:
        state.log_console(f"📡 Dongle {did} switched back to satellite tracking")
    _persist_dongles()
    _stop_capture(entry)
    return True


def set_dongle_pocsag(did, on):
    """Switch one dongle between satellite tracking and the POCSAG
    paging role (dashboard button, mirroring set_dongle_ais): on=1
    parks it on config.PAGING_CENTER_HZ - one 2.4 MHz capture covers
    the whole local paging ecosystem - and EVERY paging channel
    found there is decoded in parallel (see
    noaa_receiver/decoding/pocsag.py). The capture thread restarts
    in the new role within ~2 s; the dongle stays registered.
    Unknown id: False; a no-op request still succeeds."""
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('pocsag')) == on:
        return True
    with state.status_lock:
        entry['pocsag'] = on
        if on:
            entry['ais'] = False     # one dedicated role per dongle
            entry['orbcomm'] = False
            entry['aprs'] = False
            state.manual_recording.pop(did, None)
    if on:
        state.log_console(f"📟 Dongle {did} switched to POCSAG paging (466.0 MHz) — satellite reception on it paused")
    else:
        state.log_console(f"📟 Dongle {did} switched back to satellite tracking")
    _persist_dongles()
    _stop_capture(entry)
    return True


def set_dongle_aprs_iss(did, on):
    """Flip one dongle's APRS channel between the European simplex
    channel (144.800 MHz) and the ISS (Zarya) digipeater (145.825 MHz)
    — the checkbox under the APRS button on the dashboard card. Only
    meaningful while the dongle is in the APRS role: a running APRS
    capture thread is restarted so it retunes (within ~2 s); when the
    dongle is on the satellites the flag is just stored and takes
    effect on the next APRS switch. Like the role flags it survives
    restarts via the dongles table. Unknown id: False; a no-op request
    still succeeds."""
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('aprs_iss')) == on:
        return True
    with state.status_lock:
        entry['aprs_iss'] = on
    freq = "145.825 MHz" if on else "144.800 MHz"
    if entry.get('aprs'):
        state.log_console(f"🛰 Dongle {did} APRS channel -> {freq} — retuning")
        _stop_capture(entry)
    else:
        state.log_console(f"🛰 Dongle {did} APRS channel preset to {freq} (not in APRS role yet)")
    _persist_dongles()
    return True

def load_dongles():
    """Register the startup dongle set: the persisted dongles table when
    it has rows, else calibration.DEFAULT_DONGLES (persisted right away,
    so later dashboard additions update the database)."""
    entries = [(r['host'], r['port'], bool(r['ais']), bool(r['orbcomm']),
                bool(r['aprs']), bool(r['aprs_iss']), bool(r['pocsag']))
               for r in db.query("SELECT host, port, ais, orbcomm, aprs,"
                                 " aprs_iss, pocsag FROM dongles"
                                 " ORDER BY port, host")]
    if not entries:
        entries = [(h, p, False, False, False, False, False)
                   for h, p in DEFAULT_DONGLES]
    for host, port, was_ais, was_orbcomm, was_aprs, was_iss, was_pocsag \
            in entries:
        try:
            did, _ = add_dongle(host, port)
        except ValueError as e:
            state.log_console(f"Skipping persisted dongle {host}:{port}: {e}", "warn")
            continue
        if was_ais:
            # the AIS switch is persisted too - a restart must not silently
            # put a dongle the operator dedicated to ship traffic back on
            # the satellites (capture threads have not started yet)
            with state.status_lock:
                state.sdrs[did]['ais'] = True
            state.log_console(f"Dongle {did} restored to AIS (ship traffic) - persisted role")
        elif was_orbcomm:
            # same for the Orbcomm role: the receiver comes back up
            # listening to the satellite downlink band
            with state.status_lock:
                state.sdrs[did]['orbcomm'] = True
            state.log_console(f"Dongle {did} restored to Orbcomm (satellite IoT downlink) - persisted role")
        elif was_aprs:
            # and the APRS role: the receiver resumes on 144.800 MHz (or
            # 145.825 MHz, the ISS digipeater, when the checkbox was set)
            with state.status_lock:
                state.sdrs[did]['aprs'] = True
                state.sdrs[did]['aprs_iss'] = was_iss
            state.log_console(f"Dongle {did} restored to APRS (ham packet radio)"
                              + (" — ISS digipeater 145.825 MHz" if was_iss else "")
                              + " - persisted role")
        elif was_pocsag:
            # and the POCSAG role: the receiver resumes on the paging
            # band (every channel decoded, see decoding/pocsag.py)
            with state.status_lock:
                state.sdrs[did]['pocsag'] = True
            state.log_console(f"Dongle {did} restored to POCSAG (paging)"
                              + " - persisted role")
    _persist_dongles()


def set_dongle_paused(did, on):
    """Temporarily stop one dongle's capture (dashboard pause/play
    button).

    Two dongles recording at the same time overload the dongle host's
    shared USB bus (measured 2026-10-10 on the Pi: both streams
    collapsed to a few percent of their nominal rate); pausing one
    frees its bandwidth until the operator plays it back. Closes the
    running capture thread's socket under it - the thread notices the
    flag, closes any recording and exits; the supervisor loop starts
    a fresh thread within ~2 s of unpausing. The dongle stays
    registered - its id, waterfall and card survive. Pause is runtime
    only: a restart brings every dongle back live. Unknown id: False;
    a no-op request still succeeds.
    """
    entry = state.sdrs.get(did)
    if entry is None:
        return False
    on = bool(on)
    if bool(entry.get('paused')) == on:
        return True
    with state.status_lock:
        entry['paused'] = on
    if on:
        _stop_capture(entry)
        state.log_console(f"\u23f8 Dongle {did} paused — capture stopped")
    else:
        state.log_console(f"\u25b6 Dongle {did} played — capture restarting")
    return True


def sdr_thread():
    """Keep one capture thread per registered dongle.

    Dongles come and go at runtime (dashboard add/remove): the supervisor
    loop starts a capture thread for every new state.sdrs entry and lets
    threads of removed dongles exit (their entry carries a 'closed' flag).
    A capture thread whose rtl_tcp server goes away simply retries until
    it comes back — the reconnect cadence is the thread's own backoff."""
    load_dongles()
    threads = {}
    while True:
        for did, entry in list(state.sdrs.items()):
            t = threads.get(did)
            if t is not None and not t.is_alive():
                t = None
            if t is None and not entry.get('paused'):
                # a paused dongle (dashboard pause button) gets its
                # thread back on play, not before
                if entry.get('ais'):
                    from ..decoding.ais import ais_capture_thread
                    t = threading.Thread(target=ais_capture_thread, args=(did,),
                                         daemon=True, name=f'ais-{did}')
                elif entry.get('orbcomm'):
                    from ..decoding.orbcomm import orbcomm_capture_thread
                    t = threading.Thread(target=orbcomm_capture_thread, args=(did,),
                                         daemon=True, name=f'orbcomm-{did}')
                elif entry.get('aprs'):
                    from ..decoding.aprs import aprs_capture_thread
                    t = threading.Thread(target=aprs_capture_thread, args=(did,),
                                         daemon=True, name=f'aprs-{did}')
                elif entry.get('pocsag'):
                    from ..decoding.pocsag import pocsag_capture_thread
                    t = threading.Thread(target=pocsag_capture_thread, args=(did,),
                                         daemon=True, name=f'pocsag-{did}')
                else:
                    t = threading.Thread(target=sdr_capture_thread, args=(did,),
                                         daemon=True, name=f'sdr-{did}')
                t.start()
                threads[did] = t
        # Threads of removed dongles leave the map on their own death
        for did in [d for d, t in threads.items() if not t.is_alive()]:
            threads.pop(did, None)
        time.sleep(2)

def sdr_capture_thread(did):
    """Capture loop for one dongle (a remote rtl_tcp server): IQ stream →
    FFT waterfall + FM demod + live audio; WAV recording during passes
    (every dongle records its own file). Reconnects with backoff while the
    server is unreachable; exits when the dongle is removed."""
    import numpy as np
    entry = state.sdrs[did]
    primary = entry['primary']
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(RECORD_DIR, exist_ok=True)
    # This dongle's own DSP chain state — demod filters, rotator and NCO
    # phases must not be shared across the concurrently demodulating threads
    dst = new_state()
    restart_backoff = 5.0
    # Recording failures (disk full, permissions) must not take the receiver
    # down: the WAV open is retried at most every WAV_RETRY_SECS
    wav_retry_at = 0.0

    def set_primary_recording(path):
        if primary:
            with state.status_lock:
                state.current_wav = None
                state.current_wav_path = path
                state.is_recording = path is not None

    def close_wav():
        """Finish this dongle's current WAV (pass end, band or pass switch)."""
        wav, path = entry['wav'], entry['wav_path']
        iq = entry['iq']
        entry['wav'] = None
        entry['wav_path'] = None
        entry['iq'] = None
        entry['is_recording'] = False
        set_primary_recording(None)
        if iq is not None:
            try:
                iq.close()
            except Exception:
                pass
        if wav is None:
            return
        try:
            wav.close()
            state.log_console(f"🎬 Recording stopped (dongle {did}): {path}")
        except Exception as e:
            state.log_console(f"WAV close error (dongle {did}): {e}", "error")

    def open_wav(sat_name):
        """Start a WAV for sat_name; returns False (and arms the retry
        timer) if the file cannot be opened."""
        nonlocal wav_retry_at
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "" if primary else f"_{did.replace(':', '-')}"
        path = os.path.join(RECORD_DIR, f"{sat_short_name(sat_name)}_{timestamp}{suffix}.wav")
        # Never overwrite: a WAV split and reopened within the same second
        # (fast A -> B -> A pass switch) gets the same timestamp
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(RECORD_DIR, f"{sat_short_name(sat_name)}_{timestamp}-{n}{suffix}.wav")
        try:
            wav = wave.open(path, 'wb')
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(AUDIO_RATE)
        except Exception as e:
            wav_retry_at = time.time() + WAV_RETRY_SECS
            state.log_console(f"Cannot open recording {path} (dongle {did}): {e} — retrying in {WAV_RETRY_SECS:.0f}s", "error")
            return False
        entry['wav'] = wav
        entry['wav_path'] = path
        entry['is_recording'] = True
        # Digital modes (LRPT) on this frequency also capture the raw
        # IQ baseband next to the audio — the demod audio cannot carry them
        entry['iq'] = None
        if tuned_freq in IQ_RECORD_FREQS:
            iq_path = path[:-len('.wav')] + '.iq.u8'
            try:
                entry['iq'] = open(iq_path, 'wb', buffering=1 << 20)
                state.log_console(f"📡 IQ recording started (dongle {did}): {iq_path}")
            except Exception as e:
                state.log_console(f"IQ recording could not start (dongle {did}): {e}", "warn")
        set_primary_recording(path)
        state.log_console(f"🎬 Recording started (dongle {did}): {path}")
        return True

    def open_manual_rec(freq_hz):
        """Start the dashboard Record button's raw-IQ capture: the
        UNTOUCHED uint8 IQ stream (pre offset-shift, pre Doppler -
        the true RF baseband as the dongle sampled it) is written to
        RECORD_DIR/manual/, wrapped into an analysis zip (raw IQ +
        description.json + waterfall.png) when it finishes."""
        manual_dir = os.path.join(RECORD_DIR, "manual")
        os.makedirs(manual_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(manual_dir, "Manual_%.4fMHz_%s"
                            % (freq_hz / 1e6, timestamp))
        n = 1
        while os.path.exists(base + ".iq.u8"):
            n += 1
            base = os.path.join(manual_dir, "Manual_%.4fMHz_%s-%d"
                                % (freq_hz / 1e6, timestamp, n))
        try:
            f = open(base + ".iq.u8", "wb", buffering=1 << 20)
        except Exception as e:
            state.log_console(f"Manual raw recording could not start (dongle {did}): {e}", "warn")
            return False
        mode = state.manual_dongle_mode.get(did)
        if mode is None:
            mode = 'am' if AIRBAND[0] <= freq_hz <= AIRBAND[1] else 'fm'
        entry['manual_iq'] = f
        entry['manual_zip'] = None
        entry['manual_meta'] = {
            "base": base,
            "start_ts": time.time(),
            "start_utc": datetime.now(timezone.utc).isoformat(),
            "freq_hz": freq_hz,
            "correction": entry.get('correction', 0),
            "mode": mode,
            "bw_hz": state.manual_dongle_bw.get(did),
            "gain_db": gain_db,
            "bytes": 0,
            "wf": deque(maxlen=600),
        }
        entry['manual_rec_start'] = entry['manual_meta']['start_ts']
        state.log_console(f"⏺ Manual raw recording started (dongle {did}): "
                          f"{os.path.basename(base)} "
                          f"({MANUAL_REC_MAX_SECS:.0f} s cap, zip on stop)")
        return True

    def finalize_manual_rec():
        """Close the manual raw-IQ capture, write description.json and
        the waterfall PNG, then build the analysis zip in a background
        thread (the STORED payload can be hundreds of MB - the capture
        loop must not wait for it). entry['manual_zip'] points at the
        finished zip; the dashboard auto-downloads it when it appears."""
        f = entry.pop('manual_iq', None)
        meta = entry.pop('manual_meta', None)
        entry.pop('manual_rec_start', None)
        if f is None or meta is None:
            return
        try:
            f.close()
        except Exception:
            pass
        base = meta['base']
        iq_path = base + '.iq.u8'
        rows = (np.frombuffer(b"".join(meta['wf']), dtype=np.int16)
                .reshape(-1, FFT_SIZE)
                if len(meta['wf']) else
                np.zeros((0, FFT_SIZE), dtype=np.int16))
        png_path = None
        if len(rows) >= 8:
            try:
                from PIL import Image
                lo = float(rows.max()) - 600.0    # 60 dB display window
                img = np.clip((rows - lo) * (255.0 / 600.0),
                              0, 255).astype(np.uint8)
                Image.fromarray(img[::-1], mode='L').save(base + '.png')
                png_path = base + '.png'
            except Exception as e:
                state.log_console(f"Waterfall PNG skipped (dongle {did}): {e}", "warn")
        desc = {
            "recording": os.path.basename(base),
            "description": "Raw RTL-SDR baseband captured by the "
                           "prawnceiver station for offline / AI "
                           "signal analysis",
            "start_utc": meta['start_utc'],
            "stop_utc": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.time() - meta['start_ts'], 2),
            "how_to_read": (
                "raw_iq.u8 is the untouched dongle stream: interleaved "
                "unsigned 8-bit I/Q pairs, DC level 127.5, "
                f"{SDR_RATE} complex samples/s. The stream is centered "
                "on tuner_freq_hz (the displayed center_freq_hz plus "
                "the offset tuning and the calibration correction): "
                "the dongle's LO/DC spike sits at 0 Hz, the displayed "
                "center at -offset_tuning_hz. analysis.json lists "
                "absolute RF frequencies on that convention."),
            "preflight": ("analysis.json - the server-side pre-flight "
                          "analysis (averaged spectrum, detected emitters "
                          "with modulation and decoded codes): read this "
                          "first, the raw IQ only for deep dives"),
            "format": {
                "file": "raw_iq.u8",
                "encoding": "interleaved unsigned 8-bit I/Q",
                "dc_level": 127.5,
                "sample_rate_hz": SDR_RATE,
                "bytes": meta['bytes'],
                "complex_samples": meta['bytes'] // 2,
            },
            "radio": {
                "center_freq_hz": meta['freq_hz'],
                "center_freq_mhz": round(meta['freq_hz'] / 1e6, 6),
                "tuner_freq_hz": int(meta['freq_hz'] + SDR_OFFSET_HZ
                                     + meta['correction']),
                "offset_tuning_hz": SDR_OFFSET_HZ,
                "frequency_correction_hz": meta['correction'],
                "capture_span_hz": SDR_RATE,
                "selected_bandwidth_hz": meta['bw_hz'],
                "selected_bandwidth_note": ("demod low-pass chosen on "
                                            "the dashboard (null = mode "
                                            "default); the raw capture "
                                            "always spans capture_span_hz"),
                "demod_mode": meta['mode'],
                "gain_db": meta['gain_db'],
                "doppler_note": "the raw stream is pre-Doppler: "
                                "hardware correction is never applied, "
                                "software Doppler only touches the demod path",
            },
            "dongle": {
                "id": did,
                "label": entry['label'],
                "tuner": entry['tuner'],
                "usb_name": entry.get('usb_name'),
                "usb_serial": entry.get('usb_serial'),
                "primary": primary,
                "source": 'usb(rtl_sdr)' if entry.get('usb_idx') is not None else 'rtl_tcp',
                "host": entry['host'], "port": entry['port'],
            },
            "station": {
                "name": "prawnceiver",
                "lat": LAT, "lon": LON,
            },
            "signal": {
                "waterfall_png": "waterfall.png" if png_path else None,
                "waterfall_rows": len(rows),
                "waterfall_note": "the last dBFS FFT rows of the "
                                  "recording, 60 dB window, newest at top",
                "peak_dbfs": round(float(rows.max()) / 10.0, 1) if len(rows) else None,
                "mean_dbfs": round(float(rows.mean()) / 10.0, 1) if len(rows) else None,
            },
        }
        try:
            with open(base + '.json', 'w', encoding='utf-8') as jf:
                json.dump(desc, jf, indent=2)
        except Exception as e:
            state.log_console(f"description.json write failed (dongle {did}): {e}", "warn")

        def _build_zip(base=base, iq_path=iq_path, png_path=png_path,
                      tune_hz=meta['freq_hz'],
                      corr_hz=meta['correction']):
            # Server-side pre-flight analysis first: the spectrum,
            # the emitters and pre-decoded codes let an analyst (or
            # an AI) start from a small JSON instead of crunching
            # the raw file - same thread, before the zip, so the
            # capture loop never waits for either
            apath = base + '.analysis.json'
            try:
                from ..analysis.iqscan import analyze_recording
                # The raw stream is centered on the TUNER frequency,
                # not the displayed tune: pass center + offset +
                # correction so analysis.json lists absolute RF
                analysis = analyze_recording(
                    iq_path,
                    int(tune_hz + SDR_OFFSET_HZ + corr_hz), SDR_RATE)
                with open(apath, 'w', encoding='utf-8') as jf:
                    json.dump(analysis, jf, indent=1)
            except Exception as e:
                apath = None
                state.log_console(f"Pre-flight analysis skipped (dongle {did}): {e}", "warn")
            try:
                zpath = base + '.zip'
                # STORED, not deflated: like the station backup,
                # the IQ payload does not compress and the wait
                # would be pure CPU
                with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_STORED) as z:
                    z.write(iq_path, arcname='raw_iq.u8')
                    z.write(base + '.json', arcname='description.json')
                    if apath:
                        z.write(apath, arcname='analysis.json')
                    if png_path:
                        z.write(png_path, arcname='waterfall.png')
                entry['manual_zip'] = '/manual/' + os.path.basename(zpath)
                state.log_console(f"📦 Manual raw recording ready (dongle {did}): "
                                  f"{os.path.basename(zpath)} "
                                  f"({os.path.getsize(zpath) / 1e6:.1f} MB)")
            except Exception as e:
                state.log_console(f"Manual recording zip failed (dongle {did}): {e}", "error")
        threading.Thread(target=_build_zip, daemon=True).start()

    while True:
        if entry.get('closed'):
            close_wav()
            finalize_manual_rec()
            return
        if entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') or entry.get('pocsag'):
            # AIS, Orbcomm or POCSAG was switched on from the dashboard after this
            # thread started: hand the dongle over to the supervisor's
            # dedicated capture thread for the new role
            close_wav()
            finalize_manual_rec()
            return
        if entry.get('paused'):
            # Paused from the dashboard (pause/play button): two dongles
            # recording at once overload the dongle host's shared USB bus
            # - stop streaming and free the bandwidth. The supervisor
            # restarts this thread when the dongle is played again.
            close_wav()
            finalize_manual_rec()
            return
        run_started = time.time()
        sock = None
        try:
            with state.status_lock:
                tuned_freq = state.manual_dongle_freq.get(did, state.current_frequency)
            correction = tuning_correction(tuned_freq, did)
            # shown in the dongle cards' tuning infobox
            entry['correction'], entry['correction_src'] = correction_info(tuned_freq, did)
            freq_str = f"{tuned_freq + SDR_OFFSET_HZ + correction}"
            # Fixed tuner gain (per dongle, see calibration.SDR_DONGLE_GAIN):
            # manual gain mode + gain in tenths of dB over the control
            # protocol — the rtl_tcp CLI parses -g as an int, so fractional
            # gain steps like the R820T's 29.7 dB must go through the socket
            gain_db = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
            if entry.get('usb_idx') is not None:
                # Local USB dongle: rtl_sdr streams raw u8 IQ on stdout.
                # It cannot be retuned while running — a band switch
                # breaks the read loop and respawns it at the new
                # frequency (Doppler stays software-rotated either way)
                proc = _spawn_rtl_sdr(entry, tuned_freq + SDR_OFFSET_HZ + correction, gain_db)
                if proc is None:
                    raise RuntimeError('rtl_sdr not available on this host')
                entry['proc'] = proc
                entry['connected'] = True
                entry['tuner'] = 'USB (rtl_sdr)'
                entry['last_data'] = time.time()
                state.log_console(f"Connected to USB dongle {did} (device {entry['usb_idx']}, rtl_sdr), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={gain_db or 'auto'}dB")
                reader = _IQReader(_PipeRecv(proc.stdout), IQ_BLOCK)
            else:
                sock, tuner_type, gain_count = _rtl_tcp_connect(entry['host'], entry['port'], gain_db)
                # Explicit tune on connect: the rtl_tcp daemon serves
                # whatever frequency it last held — normally this
                # dongle's previous session (a plain reconnect), but
                # after a role switch (dashboard AIS button) the AIS
                # thread left it on the 162 MHz AIS band
                _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, tuned_freq + SDR_OFFSET_HZ + correction)
                entry['sock'] = sock
                entry['connected'] = True
                entry['tuner'] = TUNER_NAMES.get(tuner_type, f'rtl_tcp type {tuner_type}')
                entry['last_data'] = time.time()
                if primary:
                    state.log_console(f"Connected to rtl_tcp {did} (primary, tuner {entry['tuner']}, {gain_count} gain steps), tuned {freq_str}Hz (offset +{SDR_OFFSET_HZ + correction}Hz, DC spike displaced), gain={gain_db or 'auto'}dB")
                else:
                    state.log_console(f"Connected to rtl_tcp {did} (dongle, tuner {entry['tuner']}), tuned {freq_str}Hz")
                reader = _IQReader(sock, IQ_BLOCK)
            last_history_append = 0.0
            block_count = 0
            record_name = None

            while True:
                # A remove/role-switch can land while this thread was
                # in its reconnect backoff (the socket close missed it)
                # — without this check the thread would reconnect as
                # a ghost and hold the dongle forever
                if entry.get('closed') or entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') or entry.get('pocsag'):
                    break
                raw = reader.read_batch(CAPTURE_BATCH)
                if raw is None:
                    state.log_console(f"IQ stream ended (dongle {did}), reconnecting...", "warn")
                    break
                if entry['iq'] is not None:
                    try:
                        entry['iq'].write(raw)
                    except Exception as e:
                        state.log_console(f"IQ write error (dongle {did}): {e} — stopping the IQ capture", "error")
                        try: entry['iq'].close()
                        except Exception: pass
                        entry['iq'] = None
                miq = entry.get('manual_iq')
                if miq is not None:
                    try:
                        miq.write(raw)
                        entry['manual_meta']['bytes'] += len(raw)
                    except Exception as e:
                        state.log_console(f"Manual IQ write error (dongle {did}): {e} — finalizing", "error")
                        finalize_manual_rec()
                now_ts = time.time()
                entry['last_data'] = now_ts

                # One consistent snapshot of the scheduler's state per block.
                # Reading the frequency, satellite name and pass flag in
                # separate lock sections let a pass switch land between
                # them — the WAV then got the NEW satellite's name while the
                # dongle stayed on the OLD frequency, and the retune test
                # below never fired again for that pass.
                with state.status_lock:
                    override = did in state.manual_dongle_freq
                    freq_now = state.manual_dongle_freq.get(did, state.current_frequency)
                    sat_now = state.current_sat_name
                    pass_active = state.is_pass_active
                    rec_paused = state.recordings_paused
                    dop_hz, dop_freq = state.doppler_hz, state.doppler_freq_hz
                    bw_hz = state.manual_dongle_bw.get(did)
                    manual_rec = did in state.manual_recording

                # Offset-shift the baseband once: satellite to 0 Hz, DC spike
                # displaced to +SDR_OFFSET_HZ. Shared by waterfall FFT and demod.
                c = iq_to_complex(raw)
                c = frequency_shift(c, SDR_OFFSET_HZ, SDR_RATE, dst)

                # Live Doppler correction (scheduler-computed during passes):
                # rotate the satellite's Doppler-drifted carrier into the demod
                # center in software. Applied only while THIS dongle's current
                # tune (tuned_freq — not the frequency it started on) is near
                # the tracked satellite's frequency; otherwise the NCO runs at
                # 0 Hz, which keeps its phase instead of dropping it.
                if abs(tuned_freq - dop_freq) >= DOPPLER_APPLY_RANGE_HZ:
                    dop_hz = 0
                c = doppler_shift(c, dop_hz, SDR_RATE, dst)

                # FFT for waterfall + signal strength: one row per
                # FFT_EVERY-th block of the batch (see FFT_EVERY — CPU
                # budget, not display needs)
                block_count += CAPTURE_BATCH
                for k in range(0, len(c) // FFT_SIZE, FFT_EVERY):
                    try:
                        window = _get_fft_window()
                        windowed = c[k * FFT_SIZE:(k + 1) * FFT_SIZE] * window
                        fft_result = np.fft.fftshift(np.fft.fft(windowed))
                        magnitude = np.abs(fft_result)
                        center = len(magnitude) // 2
                        # signal metric: ~+/-18.75 kHz around the tracked
                        # carrier (constant Hz width, so signal peaks stay
                        # comparable across SDR_RATE changes)
                        half = max(1, round(18750 / (SDR_RATE / FFT_SIZE)))
                        band = float(magnitude[center-half:center+half].mean())
                        with entry['lock']:
                            entry['signal'] = band
                            # Raw (pre-normalization) row for the scanner
                            # (scan.py): peak/floor detection and the
                            # recorded-bandwidth fit need true magnitudes;
                            # the waterfall row below is display-scaled
                            entry['last_mag'] = magnitude.astype(np.float32)
                        if primary:
                            with state.signal_lock:
                                state.signal_strength = band
                                if entry['is_recording']:
                                    state.pass_signal_peak = max(state.pass_signal_peak, band)
                                if now_ts - last_history_append >= 1.0:
                                    last_history_append = now_ts
                                    state.signal_history.append(band)
                        # dBFS row for the WebSocket feed: absolute
                        # per-bin level, computed BEFORE the display
                        # normalization below (which rescales to the
                        # row max and destroys level - useless for a
                        # spectrum display with contrast/range/offset
                        # controls). Full scale: a +/-127.5 amplitude
                        # sine, Hann-windowed, peaks at sum(window)/2
                        # = 0.25 * FFT_SIZE of |X|. Runs for EVERY
                        # dongle - this block was once mis-indented
                        # under the primary-only signal history, which
                        # silently froze every non-primary dongle's
                        # waterfall (signal updated, rows never grew).
                        db_row = (20 * np.log10(
                            np.maximum(magnitude, 1e-6)
                            / (31.875 * FFT_SIZE)) * 10).astype(np.int16).tobytes()
                        # Display normalization by the row max. The DC
                        # spike at +SDR_OFFSET_HZ is a mild artifact on
                        # both current dongles (~2x the noise floor,
                        # never the row max - measured 2026-10-08 from
                        # raw IQ), so it needs no special-casing; the
                        # blanking that used to live here existed for
                        # the removed FC0013, whose spike saturated at
                        # 7x its strongest signal.
                        peak = magnitude.max()
                        if peak > 0:
                            magnitude *= 255.0 / peak
                        row = magnitude.astype(int).tolist()
                        with entry['lock']:
                            entry['waterfall'].append(row)
                            entry['wf_db'].append(db_row)
                            entry['wf_db_seq'] += 1
                            mm = entry.get('manual_meta')
                            if mm is not None:
                                mm['wf'].append(db_row)
                    except Exception:
                        pass

                # Retune when this dongle's target frequency changes (idle
                # park -> pass band, e.g. 137 MHz -> 437 MHz ISS; a manual
                # override set or cleared): one rtl_tcp command on the open
                # stream — the tuner re-locks in milliseconds and the IQ
                # keeps flowing, so there is no restart gap and the live
                # audio does not cut out. A running WAV is closed first so
                # one file never spans two bands. A pass switch between two
                # satellites SHARING a frequency (Meteor-M 2-3 / 2-4 at
                # 137.9125) needs no
                # retune, but the WAV is closed and reopened all the same so
                # one file never spans two passes.
                # Name for a (re)opened WAV: the tracked satellite during a
                # pass this dongle actually follows; otherwise — a manual
                # recording (dashboard Record button) or a dongle parked on
                # the operator's override frequency — the tune itself, so a
                # file is never named for a satellite this dongle is not
                # receiving (and never collides with pass attribution).
                if pass_active and not override:
                    rec_name = sat_now
                elif entry['is_recording']:
                    rec_name = f"Manual {freq_now / 1e6:.4f} MHz"
                else:
                    rec_name = None
                switch_band = freq_now != tuned_freq
                switch_sat = entry['is_recording'] and rec_name != record_name
                if switch_band or switch_sat:
                    if entry['is_recording']:
                        close_wav()
                    if switch_band:
                        if entry.get('manual_iq') is not None:
                            # a raw file must never span two tunes
                            with state.status_lock:
                                state.manual_recording.pop(did, None)
                            finalize_manual_rec()
                        if entry.get('usb_idx') is not None:
                            # rtl_sdr cannot retune live: break to the
                            # reconnect loop, which respawns it on the
                            # new frequency (the running WAV is closed
                            # above, exactly like the rtl_tcp path)
                            if primary:
                                state.log_console(f"Retuning (restart): {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                            break
                        new_corr = tuning_correction(freq_now, did)
                        _rtl_tcp_set(sock, RTL_TCP_SET_FREQ, freq_now + SDR_OFFSET_HZ + new_corr)
                        entry['correction'], entry['correction_src'] = correction_info(freq_now, did)
                        if primary:
                            state.log_console(f"Retuning (live): {tuned_freq} Hz -> {freq_now} Hz ({sat_now})")
                        tuned_freq = freq_now
                    elif primary:
                        state.log_console(f"Recording switch on the same frequency: {record_name} -> {rec_name}")
                    record_name = None

                # Record to WAV during passes — every dongle records its own
                # file (suffixed with its id); the primary additionally
                # mirrors its state into the legacy globals used by the
                # status/handler/scheduler. A dongle with a manual frequency
                # override is parked on the operator's frequency — an
                # automatic pass recording would fill a satellite-named WAV
                # with the wrong band. The dashboard's global pause
                # (rec_paused) skips the automatic WAV entirely: mid-pass it
                # closes a running file within one block, and no new one
                # opens until it is cleared.
                should_record = pass_active and not override and not rec_paused
                if should_record and not entry['is_recording']:
                    if now_ts >= wav_retry_at and open_wav(rec_name):
                        record_name = rec_name
                elif not should_record and entry['is_recording']:
                    close_wav()

                # Manual raw-IQ recording (dashboard Record button): the
                # UNTOUCHED uint8 IQ stream of the current tune, with its
                # own open/close independent of the pass WAV machinery —
                # no audio WAV, the analysis zip replaces it. Works while
                # paused and on an overridden frequency (the operator
                # asked by hand); a retune finalizes it (see switch_band
                # above), and the MANUAL_REC_MAX_SECS cap stops a
                # forgotten recording from filling the disk.
                if manual_rec and entry.get('manual_iq') is None:
                    open_manual_rec(freq_now)
                elif not manual_rec and entry.get('manual_iq') is not None:
                    finalize_manual_rec()
                elif entry.get('manual_meta') is not None \
                        and now_ts - entry['manual_meta']['start_ts'] >= MANUAL_REC_MAX_SECS:
                    with state.status_lock:
                        state.manual_recording.pop(did, None)
                    state.log_console(f"⏺ Manual raw recording reached the "
                                      f"{MANUAL_REC_MAX_SECS:.0f} s cap (dongle {did}) — finalizing")
                    finalize_manual_rec()

                # Demodulate the batch (every dongle: each feeds its own
                # live audio ring and its own WAV). Mode dispatch:
                # - manual per-dongle override (state.manual_dongle_mode),
                #   else auto: AM on the airband voice segment, FM
                #   everywhere else (broadcast FM and NFM satellites)
                # - FM: broadcast tunes deviate ±75 kHz, so the whole ±120
                #   kHz capture band is demodulated and only the audio is
                #   low-passed; satellite SSTV keeps the narrow 22 kHz.
                # - AM/SSB/CW: channel low-pass + demodulator (dsp.py);
                #   demod channel width = the manual per-dongle override
                #   (bw_hz) if set, else the mode default.
                try:
                    mode = state.manual_dongle_mode.get(did)
                    if mode is None:
                        mode = 'am' if AIRBAND[0] <= freq_now <= AIRBAND[1] else 'fm'
                    if mode == 'am':
                        audio = am_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz or 10000, st=dst)
                    elif mode in ('usb', 'lsb', 'cw'):
                        audio = ssb_demodulate(c, DECIMATION, mode,
                                               iq_cutoff_hz=bw_hz or (4000 if mode == 'cw' else 6000),
                                               st=dst)
                    elif FM_BAND[0] <= freq_now <= FM_BAND[1]:
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz, audio_cutoff_hz=18000, st=dst)
                    else:
                        # Auto demod width: the narrow 22 kHz satellite default
                        audio = fm_demodulate(c, DECIMATION, iq_cutoff_hz=bw_hz or 22000, st=dst)
                except Exception:
                    audio = b''
                if audio:
                    state.push_live_audio(entry, audio)
                    if entry['wav'] is not None:
                        try:
                            entry['wav'].writeframes(audio)
                        except Exception as e:
                            # Disk full etc.: stop this file instead of
                            # logging 469 errors/s; the open is retried
                            # after WAV_RETRY_SECS while the pass lasts
                            state.log_console(f"WAV write error (dongle {did}): {e} — closing {entry['wav_path']}", "error")
                            close_wav()
                            wav_retry_at = now_ts + WAV_RETRY_SECS

        except Exception as e:
            if not entry.get('closed') and not entry.get('ais') \
                    and not entry.get('orbcomm') and not entry.get('paused') \
                    and not entry.get('pocsag'):   # a removed/role-switched/paused dongle's socket is
                state.log_console(f"SDR thread error (dongle {did}): {e}", "error")  # closed under the thread — not an error
        entry['connected'] = False
        entry['sock'] = None
        if sock is not None:
            try: sock.close()
            except Exception: pass
        _kill_rtl_sdr(entry.pop('proc', None))
        close_wav()
        finalize_manual_rec()
        if entry.get('closed') or entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') or entry.get('paused'):
            # a removed/role-switched/paused dongle never reconnects;
            # a dongle removed mid-backoff also needs its WAV closed here
            close_wav()
            return
        # Backoff when the rtl_tcp server stays unreachable (daemon down,
        # host off, network drop): 5 s doubling up to 15 s, reset after a
        # stable run. The prawntenna manager (see its API.md) kills a
        # wedged rtl_tcp and republishes the same port within ~30 s, so
        # retrying more slowly than that only adds dead air.
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)
