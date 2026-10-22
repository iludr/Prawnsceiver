"""Battery status for the dashboard's power indicator (#power-status).

Two sources, merged by power_data() — served as /power.json and pushed
on the WebSocket 'power' topic:

* the receiver host's own batteries, read from /sys/class/power_supply
  (the notebook case — a machine without batteries returns none);

* PiSugar batteries on the TCP dongle hosts: when the dongles are
  served by prawntenna on a Raspberry Pi, that Pi's PiSugar power
  manager (pisugar-server, docs.pisugar.com) reports the HAT battery.
  Per its documentation the server exposes a standalone WebSocket API
  on port 8422 and a raw TCP command API on port 8423, both speaking
  the same line commands ("get battery" -> "battery: 85"). power.py
  probes the WebSocket API on 8422 first and falls back to the raw TCP
  API, so a dongle host that "provides data on 8422" is picked up.

Probing happens in a background thread (power_refresh_thread) and the
answers are cached — the HTTP/WS payload builders never touch the
network. A host that answers with a battery level appears in the
'remote' list; hosts without a PiSugar stay out of it (and the
dashboard shows nothing for them).
"""
import base64
import os
import socket
import threading
import time

from . import state
from .config import (PISUGAR_PROBE_TIMEOUT, PISUGAR_TCP_PORT,
                     PISUGAR_WS_PORT, POWER_REFRESH_SECS)


# ---------------------------------------------------------------------------
# PiSugar power-manager client
# ---------------------------------------------------------------------------

# Commands (docs.pisugar.com command table) and the answer keys they
# produce: "get battery" -> "battery: 85" (level %), "get battery_v" ->
# "battery_v: 4.087" (volts), "get battery_charging" ->
# "battery_charging: true", USB input on the new models ->
# "battery_power_plugged: true", "get model" -> "model: PiSugar 3 Plus".
_PISUGAR_CMDS = ("get model", "get battery", "get battery_v",
                 "get battery_charging", "get battery_power_plugged")
_PISUGAR_KEYS = frozenset(("model", "battery", "battery_v",
                           "battery_charging", "battery_power_plugged"))


class _WsClient:
    """Minimal RFC 6455 WebSocket client (stdlib only) — enough for the
    PiSugar power manager's command API: one handshake, masked text
    frames out, unmasked text frames in, ping/pong, close."""

    def __init__(self, host, port, timeout):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b''
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = ("GET / HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n"
               "\r\n")
        self._sock.sendall(req.encode('ascii'))
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("pisugar ws handshake: connection closed")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode('latin1')
        if " 101" not in status:
            raise ConnectionError(f"pisugar ws handshake refused: {status}")

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("pisugar ws: connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])   # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)           # masked (clients must mask)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, 'big')
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, 'big')
        header += mask
        self._sock.sendall(bytes(header) +
                           bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv_text(self):
        """Next text message. Answers pings, skips pongs, accumulates
        fragmented messages; raises ConnectionError on a close frame."""
        data = bytearray()
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read_exact(2), 'big')
            elif ln == 127:
                ln = int.from_bytes(self._read_exact(8), 'big')
            mask = self._read_exact(4) if b1 & 0x80 else None
            payload = self._read_exact(ln) if ln else b''
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                 # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                 # pong
                continue
            if opcode == 0x8:                 # close
                raise ConnectionError("pisugar ws: close frame")
            if opcode in (0x0, 0x1, 0x2):     # continuation / text / binary
                data += payload
                if fin:
                    return bytes(data).decode('utf-8', 'replace')

    def close(self):
        try:
            self._send_frame(0x8, b'')
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _parse_kv(text):
    """Parse the power manager's "key: value" answer lines. The server
    also pushes unsolicited battery-state events as JSON lines (they
    start with '{') — those produce keys nobody reads and are skipped
    here."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(':')
        if not sep:
            continue
        k = k.strip()
        if k in _PISUGAR_KEYS and k not in out:
            out[k] = v.strip()
    return out


def _collect_ws(ws, deadline):
    found = {}
    while len(found) < len(_PISUGAR_KEYS) and time.time() < deadline:
        try:
            msg = ws.recv_text()
        except (socket.timeout, TimeoutError):
            break
        for k, v in _parse_kv(msg).items():
            found.setdefault(k, v)
    return found


def _pisugar_ws_query(host, port, timeout):
    """Query via the standalone WebSocket API (8422): one text frame
    carrying all command lines — the server answers one text frame per
    line (plus asynchronous event pushes, which _parse_kv skips)."""
    ws = _WsClient(host, port, timeout)
    try:
        ws.send_text("\n".join(_PISUGAR_CMDS))
        return _collect_ws(ws, time.time() + timeout)
    finally:
        ws.close()


def _pisugar_tcp_query(host, port, timeout):
    """Query via the raw TCP command API (8423): newline-separated
    commands in, one "key: value" line out per command."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(("\n".join(_PISUGAR_CMDS) + "\n").encode('utf-8'))
        found = {}
        buf = b''
        deadline = time.time() + timeout
        while len(found) < len(_PISUGAR_KEYS) and time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                for k, v in _parse_kv(line.decode('utf-8', 'replace')).items():
                    found.setdefault(k, v)
        return found


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    if v is None:
        return None
    return v.strip().lower() in ("true", "1", "yes", "on")


def _normalize(host, kv, source):
    """The dashboard-facing battery dict, or None when the answers carry
    no battery level (not a PiSugar, or the read failed)."""
    capacity = _to_int(kv.get("battery"))
    if capacity is None:
        return None
    charging = _to_bool(kv.get("battery_charging"))
    if charging:
        status = "Charging"
    elif capacity >= 100:
        status = "Full"
    elif charging is False:
        status = "Discharging"
    else:
        status = "Unknown"
    return {
        "host": host,
        "name": kv.get("model") or "PiSugar",
        "capacity": capacity,
        "voltage": _to_float(kv.get("battery_v")),
        "charging": charging,
        "power_plugged": _to_bool(kv.get("battery_power_plugged")),
        "status": status,
        "source": source,
    }


def probe_pisugar(host, ws_port=None, tcp_port=None, timeout=None):
    """Query one host's PiSugar power manager: WebSocket API on 8422
    first (the documented interface), raw TCP API on 8423 as fallback.
    Returns the normalized battery dict, or None when the host runs no
    PiSugar (nothing listening, or no battery level in the answers)."""
    ws_port = PISUGAR_WS_PORT if ws_port is None else ws_port
    tcp_port = PISUGAR_TCP_PORT if tcp_port is None else tcp_port
    timeout = PISUGAR_PROBE_TIMEOUT if timeout is None else timeout
    try:
        kv = _pisugar_ws_query(host, ws_port, timeout)
        entry = _normalize(host, kv, "ws")
        if entry is not None:
            return entry
    except (OSError, ConnectionError):
        pass
    try:
        kv = _pisugar_tcp_query(host, tcp_port, timeout)
        return _normalize(host, kv, "tcp")
    except (OSError, ConnectionError):
        return None


# ---------------------------------------------------------------------------
# Cached background probing of the dongle hosts
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_remote_cache = {}   # host -> {"ts": float, "data": battery dict}


def _dongle_hosts():
    """Unique hosts of the TCP dongles. USB dongles live on the receiver
    host itself (host 'usb', usb_idx set) and are skipped; when the
    receiver runs on the dongle Pi itself, that host is 127.0.0.1 and
    gets probed too."""
    with state.status_lock:
        return sorted({e["host"] for e in state.sdrs.values()
                       if e.get("usb_idx") is None and e.get("host")
                       and e["host"] != "usb"})


def remote_batteries():
    """Cached PiSugar batteries of the dongle hosts, sorted by host."""
    with _cache_lock:
        return [dict(c["data"])
                for c in sorted(_remote_cache.values(),
                                key=lambda c: c["data"]["host"])]


def _refresh_once():
    for host in _dongle_hosts():
        data = probe_pisugar(host)
        with _cache_lock:
            known = host in _remote_cache
            if data is not None:
                _remote_cache[host] = {"ts": time.time(), "data": data}
            elif known:
                del _remote_cache[host]
        if data is not None and not known:
            state.log_console(f"🪫 Battery on {host}: {data['name']} "
                              f"{data['capacity']}% "
                              f"({data['status'].lower()}) — shown in the "
                              f"dashboard header")
        elif data is None and known:
            state.log_console(f"🪫 Battery status on {host} lost "
                              f"(PiSugar power manager unreachable)", "warn")


def power_refresh_thread():
    """Background: probe every TCP dongle host's PiSugar power manager
    and cache the answers, so /power.json and the WS 'power' push never
    block on the network. A host that starts answering is announced
    once; a host that stops answering loses its indicator until it is
    back."""
    while True:
        try:
            _refresh_once()
        except Exception as e:
            state.log_console(f"Power probe failed: {e}", "warn")
        time.sleep(POWER_REFRESH_SECS)


# ---------------------------------------------------------------------------
# The receiver host's own batteries (the notebook case)
# ---------------------------------------------------------------------------

def local_supplies():
    """(supplies, ac) from the receiver host's /sys/class/power_supply:
    every battery with capacity and status, plus whether an external
    supply (AC adapter, USB-C dock, ...) is online. A machine without
    batteries (desktop/container) returns ([], False)."""
    supplies = []
    ac = False
    pbase = '/sys/class/power_supply'
    if os.path.isdir(pbase):
        for pname in sorted(os.listdir(pbase)):
            pdir = os.path.join(pbase, pname)

            def rd(f, _d=pdir):
                try:
                    with open(os.path.join(_d, f)) as fh:
                        return fh.read().strip()
                except OSError:
                    return ''

            ptype = rd('type')
            if ptype in ('Mains', 'USB'):
                online = rd('online') == '1'
                ac = ac or online
                supplies.append({'name': pname, 'type': ptype,
                                 'online': online})
            elif ptype == 'Battery':
                status = rd('status')
                ac = ac or status == 'Charging'
                try:
                    cap = int(rd('capacity'))
                except ValueError:
                    cap = None
                supplies.append({'name': pname, 'type': 'Battery',
                                 'capacity': cap, 'status': status})
    return supplies, ac


def power_data():
    """The /power.json payload: the receiver host's own supplies plus
    the cached PiSugar batteries of the TCP dongle hosts."""
    supplies, ac = local_supplies()
    return {'ac': ac, 'supplies': supplies, 'remote': remote_batteries()}
