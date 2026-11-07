"""readsb-backed ADS-B: when a dongle is switched to the ADS-B role and
its rtl_tcp feed is the one readsb is configured to consume
(/etc/default/readsb: RTL_TCP_HOST/RTL_TCP_PORT), prawnceiver does NOT
run its own envelope demodulator on that dongle. Instead it leaves the
rtl_tcp client slot free for readsb's bridge and consumes readsb's Beast
binary output (TCP 30005) here in the master process, reusing
adsb.parse_frame/handle_message so the live table, registry, message
log, WS feed and /adsb.json are unchanged.

Why: rtl_tcp serves a single client — while prawnceiver's own ADS-B
capture held the dongle, readsb's bridge could not connect (and vice
versa). readsb is the better decoder (proper Mode S handling, MLAT,
CRC filtering); prawnceiver keeps its UI/DB/API.

The consumer threads run in the MASTER (not in per-dongle workers):
adsb.handle_message is the single writer for the registry/DB and in
worker mode the worker's copy of it is patched to forward to the master
anyway.
"""
import os
import re
import socket
import subprocess
import threading
import time

from .. import state
from . import adsb

READSB_DEFAULTS = "/etc/default/readsb"
BEAST_HOST = "127.0.0.1"
BEAST_PORT = 30005          # readsb --net-bo-port (Beast binary out)

# Beast frame: 0x1A <type> <payload>, 0x1A escaped as 0x1A 0x1A.
# Type '2' = 6-byte MLAT + 1-byte signal + 7-byte Mode S short,
# type '3' = 6 + 1 + 14-byte Mode S long.
_BEAST_LEN = {'1': 2, '2': 14, '3': 21, '4': 2}

_feed_cache = (None, 0.0)   # (feed_id, mtime)


def readsb_feed_id():
    """The 'host:port' readsb is configured to feed from, or None.
    Re-read when /etc/default/readsb changes."""
    global _feed_cache
    try:
        mt = os.path.getmtime(READSB_DEFAULTS)
    except OSError:
        return _feed_cache[0]
    if _feed_cache[0] is not None and mt == _feed_cache[1]:
        return _feed_cache[0]
    host = port = None
    try:
        with open(READSB_DEFAULTS, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'\s*RTL_TCP_HOST=(\S+)', line)
                if m:
                    host = m.group(1)
                m = re.match(r'\s*RTL_TCP_PORT=(\d+)', line)
                if m:
                    port = m.group(1)
    except OSError:
        pass
    feed = f"{host}:{port}" if host and port else None
    _feed_cache = (feed, mt)
    return feed


def is_readsb_dongle(did):
    """True when this dongle's rtl_tcp endpoint is readsb's configured
    feed — for such a dongle the ADS-B role is served by readsb, not by
    our own capture thread."""
    feed = readsb_feed_id()
    return feed is not None and did == feed


# The readsb system service is managed via sudo -n with a narrow
# sudoers rule (/etc/sudoers.d/prawnsdr-readsb: only start/stop/restart/
# is-active of readsb.service, no password). NOTE: sudo matches the
# arguments exactly — always pass the full unit name readsb.service.
_SYSTEMCTL = "/usr/bin/systemctl"
_SUDO = ["sudo", "-n"] if os.path.exists("/usr/bin/sudo") else []


def _readsb_ctl(verb):
    """systemctl <verb> readsb.service (via sudo -n when needed)."""
    cmd = _SUDO + [_SYSTEMCTL, verb, "readsb.service"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=15)
    except Exception:
        # fall back to a direct call (works when running as root)
        try:
            return subprocess.run([_SYSTEMCTL, verb, "readsb.service"],
                                  capture_output=True, text=True, timeout=15)
        except Exception as e:
            state.log_console(f"readsb {verb} failed: {e}", "warn")
            return None


def _readsb_active():
    r = _readsb_ctl("is-active")
    return r is not None and r.stdout.strip() == "active"


def ensure_readsb_service():
    """Make sure the readsb service is up when a readsb-backed dongle
    is in the ADS-B role."""
    if _readsb_active():
        return True
    _readsb_ctl("start")
    if not _readsb_active():
        state.log_console("readsb service is not active — ADS-B via "
                          "readsb unavailable (needs the sudoers rule "
                          "for readsb.service)", "warn")
        return False
    state.log_console("readsb service started — feeding the ADS-B dongle")
    return True


def stop_readsb_service():
    """Stop readsb when no readsb-backed dongle is in the ADS-B role:
    its bridge holds the dongle's single rtl_tcp client slot, which
    would block the dongle for any other role."""
    if not _readsb_active():
        return
    r = _readsb_ctl("stop")
    if _readsb_active():
        state.log_console(f"could not stop readsb ({(r.stderr or '').strip()})"
                          " — the dongle's rtl_tcp slot stays blocked",
                          "warn")
    else:
        state.log_console("readsb service stopped — no dongle in the "
                          "ADS-B role, rtl_tcp slot freed")


def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def beast_frames(sock):
    """Yield Mode S data bytes (7 short / 14 long) from a Beast stream.
    Raises OSError on connection loss; ValueError on an unknown frame
    type (stream cannot be resynced safely)."""
    while True:
        b = _recv_exact(sock, 1)
        if b is None:
            raise OSError("beast stream ended")
        if b[0] != 0x1A:
            continue                    # resync
        t = _recv_exact(sock, 1)
        if t is None:
            raise OSError("beast stream ended")
        typ = chr(t[0])
        ln = _BEAST_LEN.get(typ)
        if ln is None:
            raise ValueError(f"unknown beast frame type {typ!r}")
        # read until ln UNESCAPED bytes: 0x1A 0x1A on the wire is one 0x1A
        # payload byte (the length counts payload bytes, not wire bytes)
        out = bytearray()
        while len(out) < ln:
            c = _recv_exact(sock, 1)
            if c is None:
                raise OSError("beast stream ended")
            if c[0] == 0x1A:
                c2 = _recv_exact(sock, 1)
                if c2 is None:
                    raise OSError("beast stream ended")
                if c2[0] == 0x1A:
                    out.append(0x1A)
                else:
                    # a new frame started early: resync from its type byte
                    typ = chr(c2[0])
                    ln = _BEAST_LEN.get(typ)
                    if ln is None:
                        raise ValueError(f"unknown beast frame type {typ!r}")
                    out = bytearray()
            else:
                out.append(c[0])
        if typ == '2':
            yield bytes(out[7:14])      # short Mode S
        elif typ == '3':
            yield bytes(out[7:21])      # long Mode S


def _bytes_to_bits(data):
    bits = []
    for byte in data:
        for k in range(7, -1, -1):
            bits.append((byte >> k) & 1)
    return bits


def readsb_consumer_thread(did, stop):
    """Consume readsb's Beast output (30005) for one readsb-backed
    dongle in the ADS-B role: every frame goes through the same
    parse_frame/handle_message path as the homegrown demodulator.
    Runs in the master process. Reconnects with backoff; exits when
    stop is set or the dongle leaves the ADS-B role / is paused."""
    entry = state.sdrs.get(did)
    if entry is None:
        return
    st = {"frames": 0, "bad": 0, "last_frame": 0.0, "floor": None,
          "dongle": did}
    state.log_console(f"✈️ ADS-B on dongle {did} via readsb "
                      f"(Beast {BEAST_HOST}:{BEAST_PORT}) — readsb decodes, "
                      f"prawnceiver logs/maps")
    backoff = 5.0
    sock = None
    while not stop.is_set():
        e = state.sdrs.get(did)
        if e is None or e.get('closed') or not e.get('adsb') \
                or e.get('paused') or not is_readsb_dongle(did):
            break
        try:
            ensure_readsb_service()
            sock = socket.create_connection((BEAST_HOST, BEAST_PORT),
                                            timeout=10)
            sock.settimeout(30)
            backoff = 5.0
            for data in beast_frames(sock):
                if stop.is_set():
                    break
                e = state.sdrs.get(did)
                if e is None or e.get('closed') or not e.get('adsb') \
                        or e.get('paused'):
                    break
                bits = _bytes_to_bits(data)
                d = adsb.parse_frame(bits)
                if d is None:
                    st["bad"] += 1
                else:
                    d["hex"] = adsb.bits_to_hex(bits)
                    adsb.handle_message(d, st)
                st["last_frame"] = time.time()
                with state.adsb_lock:
                    state.adsb_stats = {"frames": st["frames"],
                                        "bad": st["bad"],
                                        "last_frame": st["last_frame"],
                                        "floor": None,
                                        "src": "readsb"}
        except (OSError, ValueError) as ex:
            if not stop.is_set():
                state.log_console(f"readsb feed ({did}) interrupted: {ex}"
                                  f" — reconnecting", "warn")
        except Exception as ex:
            if not stop.is_set():
                state.log_console(f"readsb consumer error ({did}): {ex}",
                                  "error")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
        if stop.is_set():
            break
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
    state.log_console(f"✈️ ADS-B readsb consumer for {did} stopped")


# ---------- master-side supervisor ----------

_consumers = {}          # did -> {"thread": Thread, "stop": Event}
_consumers_lock = threading.Lock()


def _reception_off(did):
    """Mirror of adsb_capture_thread.reception_off: clear the global
    ADS-B enabled flag when no other active dongle serves ADS-B."""
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


def readsb_supervisor():
    """Master loop: for every readsb-backed dongle currently in the
    ADS-B role (and not paused), keep one Beast consumer thread alive
    and mark ADS-B enabled. Started from radio.sdr_thread."""
    while True:
        try:
            wanted = {}
            for did, e in list(state.sdrs.items()):
                if e.get('adsb') and not e.get('closed') \
                        and not e.get('paused') and is_readsb_dongle(did):
                    wanted[did] = True
            with _consumers_lock:
                for did in list(_consumers):
                    if did not in wanted or did not in state.sdrs:
                        c = _consumers.pop(did)
                        c["stop"].set()
                for did in wanted:
                    if did not in _consumers:
                        stop = threading.Event()
                        t = threading.Thread(
                            target=readsb_consumer_thread, args=(did, stop),
                            daemon=True, name=f'readsb-{did}')
                        _consumers[did] = {"thread": t, "stop": stop}
                        t.start()
                        with state.adsb_lock:
                            state.adsb_enabled = True
                            state.adsb_dongle = did
                # drop consumers whose thread died
                for did in [d for d, c in _consumers.items()
                            if not c["thread"].is_alive()]:
                    c = _consumers.pop(did)
                    c["stop"].set()
            if not wanted:
                # nothing readsb-backed active: only clear the flag if
                # it was ours (a homegrown ADS-B dongle may own it)
                with state.adsb_lock:
                    ours = state.adsb_dongle is not None \
                        and is_readsb_dongle(state.adsb_dongle)
                if ours:
                    _reception_off(state.adsb_dongle)
                # no dongle needs readsb right now: stop the service so
                # its bridge releases the dongle's rtl_tcp slot
                stop_readsb_service()
        except Exception as e:
            state.log_console(f"readsb supervisor error: {e}", "error")
        time.sleep(2.0)
