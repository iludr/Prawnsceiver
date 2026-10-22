"""PiSugar battery probing + the /power.json payload (noaa_receiver/power.py).

The TCP dongle hosts may run a PiSugar power manager (pisugar-server,
docs.pisugar.com): a standalone WebSocket API on port 8422 and a raw
TCP command API on port 8423, both speaking the same line commands
("get battery" -> "battery: 63"). power.py probes 8422 first and falls
back to 8423, caches the answers in a background thread, and merges
them into /power.json next to the receiver host's own batteries.

Unit cases: answer parsing (incl. skipping the server's unsolicited
JSON event pushes), status normalization, cache/refresh bookkeeping.
Integration: the real probe against a fake PiSugar WebSocket server
(handshake, ping/pong, one answer frame per command line, an
interleaved JSON event push) and a fake raw TCP server (the fallback
when nothing serves 8422); the /power.json HTTP payload shape.

Run: python -u tests/test_power.py  (no hardware; ephemeral localhost ports)
"""
import base64
import hashlib
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))

from noaa_receiver import power, state  # noqa: E402
from noaa_receiver.web.handler import NOAAHandler  # noqa: E402

HOST = '127.0.0.1'
ANSWERS = {
    'get model': 'model: PiSugar 3 Plus',
    'get battery': 'battery: 63',
    'get battery_v': 'battery_v: 4.087',
    'get battery_charging': 'battery_charging: false',
    'get battery_power_plugged': 'battery_power_plugged: false',
}


def check(name, cond, detail=''):
    assert cond, f'FAIL {name}: {detail}'
    print(f'  {name} OK {detail}')


# ---- server-side WebSocket framing (the fake PiSugar lives here) ----

def _srv_read_frame(recv, buf):
    def exact(n):
        while len(buf['data']) < n:
            chunk = recv(4096)
            if not chunk:
                raise ConnectionError('closed')
            buf['data'] += chunk
        out, buf['data'] = buf['data'][:n], buf['data'][n:]
        return out
    b0, b1 = exact(2)
    opcode = b0 & 0x0F
    ln = b1 & 0x7F
    if ln == 126:
        ln = int.from_bytes(exact(2), 'big')
    elif ln == 127:
        ln = int.from_bytes(exact(8), 'big')
    mask = exact(4) if b1 & 0x80 else None
    payload = exact(ln) if ln else b''
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _srv_send_frame(send, opcode, payload):
    header = bytearray([0x80 | opcode])   # servers send unmasked
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += n.to_bytes(2, 'big')
    else:
        header.append(127)
        header += n.to_bytes(8, 'big')
    send(bytes(header) + payload)


class FakePisugarWs(threading.Thread):
    """A faithful-enough pisugar-server WebSocket API (the documented
    interface on 8422): RFC 6455 handshake, then text frames carrying
    the same line commands as the raw TCP API — one "key: value" text
    frame per command line. Sends a ping first (the client must pong)
    and interleaves an unsolicited JSON event push after the second
    answer (the client must skip it)."""

    def __init__(self, answers):
        super().__init__(daemon=True)
        self.answers = answers
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.got_pong = False
        self.commands = []

    def run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.settimeout(5)
        buf = {'data': b''}
        try:
            while b'\r\n\r\n' not in buf['data']:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf['data'] += chunk
            head, rest = buf['data'].split(b'\r\n\r\n', 1)
            buf['data'] = rest
            key = ''
            for line in head.decode('latin1').split('\r\n'):
                if line.lower().startswith('sec-websocket-key:'):
                    key = line.split(':', 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()
            ).digest()).decode()
            conn.sendall(('HTTP/1.1 101 Switching Protocols\r\n'
                          'Upgrade: websocket\r\n'
                          'Connection: Upgrade\r\n'
                          f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
            # ping first: the client must answer with a pong
            _srv_send_frame(conn.sendall, 0x9, b'')
            answered = 0
            while True:
                opcode, payload = _srv_read_frame(conn.recv, buf)
                if opcode == 0xA:             # pong
                    self.got_pong = True
                    continue
                if opcode == 0x8:             # close
                    _srv_send_frame(conn.sendall, 0x8, b'')
                    return
                if opcode != 0x1:             # text only
                    continue
                for line in payload.decode().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    self.commands.append(line)
                    if line in self.answers:
                        _srv_send_frame(conn.sendall, 0x1,
                                        self.answers[line].encode())
                        answered += 1
                        if answered == 2:
                            # unsolicited battery-state event push —
                            # not a "key: value" answer, the client
                            # must skip it
                            _srv_send_frame(conn.sendall, 0x1,
                                            b'{"battery": 99, "voltage": 4.05}')
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class FakePisugarTcp(threading.Thread):
    """The raw TCP command API (port 8423): newline-separated commands
    in, one "key: value" line out per command."""

    def __init__(self, answers):
        super().__init__(daemon=True)
        self.answers = answers
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.commands = []

    def run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.settimeout(5)
        try:
            buf = b''
            while buf.count(b'\n') < len(self.answers):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            for line in buf.decode().splitlines():
                line = line.strip()
                if line in self.answers:
                    self.commands.append(line)
                    conn.sendall((self.answers[line] + '\n').encode())
            time.sleep(0.3)   # let the client read before the close
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


print("1. parse: \"key: value\" answers; JSON event pushes and noise skipped")
kv = power._parse_kv('model: PiSugar 3 Plus\nbattery: 63\n'
                     '{"battery": 99, "voltage": 4.05}\n'
                     'garbage line\n'
                     'battery_v: 4.087\nbattery_charging: false\n'
                     'battery: 11\n')   # first occurrence wins
check('answers parsed',
      kv == {'model': 'PiSugar 3 Plus', 'battery': '63',
             'battery_v': '4.087', 'battery_charging': 'false'}, str(kv))

print("2. normalize: status mapping; no battery level -> None")
e = power._normalize('h', power._parse_kv('\n'.join(ANSWERS.values())), 'ws')
check('discharging', e['status'] == 'Discharging' and e['capacity'] == 63
      and abs(e['voltage'] - 4.087) < 1e-9 and e['name'] == 'PiSugar 3 Plus',
      str(e))
check('charging', power._normalize(
    'h', {'battery': '80', 'battery_charging': 'true'}, 'tcp')['status'] == 'Charging')
check('full', power._normalize(
    'h', {'battery': '100', 'battery_charging': 'false'}, 'tcp')['status'] == 'Full')
check('unknown without charging info',
      power._normalize('h', {'battery': '50'}, 'tcp')['status'] == 'Unknown')
check('default name', power._normalize('h', {'battery': '5'}, 'ws')['name'] == 'PiSugar')
check('no level -> None', power._normalize('h', {'model': 'PiSugar 3 Plus'}, 'tcp') is None)

print("3. probe: WebSocket API (the documented interface on 8422), end to end")
ws = FakePisugarWs(ANSWERS)
ws.start()
e = power.probe_pisugar(HOST, ws_port=ws.port, tcp_port=1, timeout=3)
check('battery read over ws', e is not None and e['capacity'] == 63
      and e['name'] == 'PiSugar 3 Plus' and e['source'] == 'ws', str(e))
check('voltage parsed', e and abs(e['voltage'] - 4.087) < 1e-9)
ws.join(timeout=5)
check('client answered the ping', ws.got_pong)
check('all commands sent', sorted(ws.commands) == sorted(ANSWERS), str(ws.commands))

print("4. probe: raw TCP API (8423) as fallback when 8422 is closed")
tcp = FakePisugarTcp(ANSWERS)
tcp.start()
e = power.probe_pisugar(HOST, ws_port=1, tcp_port=tcp.port, timeout=3)
check('battery read over tcp', e is not None and e['capacity'] == 63
      and e['source'] == 'tcp', str(e))
tcp.join(timeout=5)
check('all commands sent', sorted(tcp.commands) == sorted(ANSWERS), str(tcp.commands))

print("5. probe: host without a PiSugar -> None, fast")
t0 = time.time()
e = power.probe_pisugar(HOST, ws_port=1, tcp_port=1, timeout=2)
check('None', e is None)
check('fast (connection refused)', time.time() - t0 < 1.5, f'{time.time() - t0:.2f}s')

print("6. power_data(): payload shape (local supplies + cached remote)")
FAKE_BAT = {'host': '192.168.3.245', 'name': 'PiSugar 3 Plus', 'capacity': 63,
            'voltage': 4.087, 'charging': False, 'power_plugged': False,
            'status': 'Discharging', 'source': 'ws'}
power._remote_cache.clear()
with power._cache_lock:
    power._remote_cache['192.168.3.245'] = {'ts': time.time(), 'data': FAKE_BAT}
d = power.power_data()
check('keys', set(d) == {'ac', 'supplies', 'remote'}, str(set(d)))
check('remote present', len(d['remote']) == 1
      and d['remote'][0]['host'] == '192.168.3.245', str(d['remote']))
check('supplies is a list', isinstance(d['supplies'], list))
check('ac is a bool', isinstance(d['ac'], bool))

print("7. _refresh_once: TCP dongle hosts probed, cache follows, USB skipped")
state.sdrs.clear()
state.sdrs['192.168.3.245:1234'] = {'host': '192.168.3.245', 'port': 1234,
                                   'usb_idx': None}
state.sdrs['usb:0'] = {'host': 'usb', 'port': 0, 'usb_idx': 0}
probed = []
orig_probe = power.probe_pisugar


def fake_probe(host):
    probed.append(host)
    return dict(FAKE_BAT, host=host)


power.probe_pisugar = fake_probe
try:
    power._remote_cache.clear()
    power._refresh_once()
    check('tcp dongle host probed (usb skipped)', probed == ['192.168.3.245'],
          str(probed))
    rem = power.remote_batteries()
    check('cached', len(rem) == 1 and rem[0]['capacity'] == 63, str(rem))
    # the host stops answering -> the indicator entry is dropped
    power.probe_pisugar = lambda host: None
    power._refresh_once()
    check('dropped when unreachable', power.remote_batteries() == [])
    # ...and comes back when it answers again
    power.probe_pisugar = fake_probe
    power._refresh_once()
    check('restored when back', len(power.remote_batteries()) == 1)
finally:
    power.probe_pisugar = orig_probe
    state.sdrs.clear()
    power._remote_cache.clear()

print("8. /power.json over HTTP carries the remote batteries")
with power._cache_lock:
    power._remote_cache['192.168.3.245'] = {'ts': time.time(), 'data': FAKE_BAT}
srv = socketserver.ThreadingTCPServer((HOST, 0), NOAAHandler)
srv.daemon_threads = True
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
with urllib.request.urlopen(f'http://{HOST}:{port}/power.json', timeout=5) as r:
    body = json.loads(r.read().decode())
check('http payload', set(body) == {'ac', 'supplies', 'remote'}
      and body['remote'][0]['name'] == 'PiSugar 3 Plus', str(body)[:140])

print("test_power: all OK")
os._exit(0)
