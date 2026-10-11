"""WebSocket dBFS push: handshake + frame protocol against the real handler.

Run: python -u tests/test_ws.py  (no hardware; binds an ephemeral localhost port)
"""
import base64
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))

import socketserver

import noaa_receiver.state as state
from noaa_receiver.web.handler import NOAAHandler

# fabricate a dongle entry with a known dB row (no capture thread needed)
import numpy as np
ROW = np.full(512, -1000, dtype=np.int16)   # -100.0 dB everywhere
ROW[100] = -300                              # one -30 dB peak
ROW_BYTES = ROW.tobytes()
DID = "test:1234"

entry = {
    'id': DID, 'host': 'test', 'port': 1234, 'label': 'test', 'tuner': 'test',
    'primary': True, 'ais': False, 'paused': False, 'connected': True,
    'waterfall': deque(maxlen=10), 'lock': threading.Lock(),
    'signal': 0.0, 'last_mag': None, 'sock': None, 'last_data': 0.0,
    'la': {'data': [], 'base': 0, 'total': 0, 'cond': threading.Condition()},
    'is_recording': False, 'wav': None, 'wav_path': None, 'iq': None,
    'wf_db': deque(maxlen=512), 'wf_db_seq': 0,
}
state.sdrs[DID] = entry

# the status.json endpoint needs the full station state; not used here


def feed(n):
    with entry['lock']:
        entry['wf_db'].append(ROW_BYTES)
        entry['wf_db_seq'] += 1


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), NOAAHandler)
srv.daemon_threads = True   # the WS loop runs until its socket closes
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# --- 1. handshake ---
key = base64.b64encode(os.urandom(16)).decode()
req = (f"GET /waterfall.ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
       f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
s = socket.create_connection(("127.0.0.1", port), timeout=5)
s.sendall(req.encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += s.recv(4096)
head = resp.split(b"\r\n\r\n")[0].decode()
assert "101" in head.splitlines()[0], head
import hashlib
expect = base64.b64encode(hashlib.sha1((key + NOAAHandler._WS_GUID).encode()).digest()).decode()
assert f"Sec-WebSocket-Accept: {expect}" in head, head
print("1. websocket handshake + accept key: ok")

# --- 2. first frame arrives and parses ---
time.sleep(0.5)
feed(1)
s.settimeout(5)
buf = resp.split(b"\r\n\r\n", 1)[1]
deadline = time.time() + 5
frame = None
while time.time() < deadline:
    try:
        buf += s.recv(4096)
    except socket.timeout:
        break
    if len(buf) >= 4:
        ln = int.from_bytes(buf[2:4], "big")
        if len(buf) >= 4 + ln:
            frame = buf[4:4 + ln]
            break
assert frame is not None, "no binary frame within 5 s"
assert frame[0] == 1, frame[0]
nlen = int.from_bytes(frame[1:3], "big")
did = frame[3:3 + nlen].decode()
row = frame[3 + nlen:]
assert did == DID, did
assert len(row) == 1024, len(row)
vals = np.frombuffer(row, dtype="<i2")
assert vals[100] == -300 and vals[0] == -1000, (vals[100], vals[0])
print(f"2. binary frame: id={did}, 512 int16 dB*10, peak -30.0 dB at bin 100: ok")

# --- 3. rate control text frame is accepted (no crash, feed continues) ---
s.sendall(bytes([0x81, 7]) + b"rate=30")   # length MUST match the payload
feed(1)
time.sleep(0.1)
got = False
deadline = time.time() + 5
buf2 = b""
while time.time() < deadline and not got:
    try:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf2 += chunk
        while len(buf2) >= 4:
            ln = int.from_bytes(buf2[2:4], "big")
            if len(buf2) < 4 + ln:
                break
            got = True
            buf2 = buf2[4 + ln:]
    except socket.timeout:
        break
assert got, "no frame after rate control"
print("3. rate control frame accepted, stream continues: ok")

# --- 4. ping -> pong ---
s.sendall(bytes([0x89, 4]) + b"ping")
buf3 = b""
deadline = time.time() + 3
while time.time() < deadline:
    try:
        buf3 += s.recv(4096)
    except socket.timeout:
        break
    if len(buf3) >= 6 and buf3[0] == 0x8a:
        break
# server frames always use the 16-bit extended length (126 prefix):
# [0x8a, 126, len_hi, len_lo, payload]
assert len(buf3) >= 8 and buf3[0] == 0x8a and buf3[1] == 126, buf3[:8]
assert buf3[4:8] == b"ping", buf3[:8]
print("4. ping answered with pong: ok")

# --- 5. status.json carries the span field ---
st = json.loads if False else None
import json as _json
st = _json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/status.json").read())
assert st.get("sdr_khz") == 2400, st.get("sdr_khz")
print("5. status.json sdr_khz = 2400: ok")

# --- 5b. JSON topic subscription: 'sub=dongles' pushes immediately ---
s.sendall(bytes([0x81, 11]) + b"sub=dongles")   # length MUST match the payload
buf5 = b""
topic = None
payload = None
deadline = time.time() + 5
while time.time() < deadline and topic is None:
    try:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf5 += chunk
    except socket.timeout:
        break
    while len(buf5) >= 4 and topic is None:
        if buf5[0] != 0x82:
            buf5 = b""
            break
        ln = int.from_bytes(buf5[2:4], "big")
        if len(buf5) < 4 + ln:
            break
        frame5 = buf5[4:4 + ln]
        buf5 = buf5[4 + ln:]
        if frame5[0] == 1:
            continue           # dB row from the earlier tests: skip
        if frame5[0] == 2:
            nlen5 = int.from_bytes(frame5[1:3], "big")
            topic = frame5[3:3 + nlen5].decode()
            payload = _json.loads(frame5[3 + nlen5:].decode())
assert topic == "dongles", topic
assert isinstance(payload, list) and payload and payload[0]["id"] == DID, payload[:1]
print("5b. 'sub=dongles': immediate type=2 JSON push with the dongle list: ok")

# --- 6. close frame terminates the server side cleanly ---
s.sendall(bytes([0x88, 2]) + b"\x03\xe8")
time.sleep(0.3)
s.close()
srv.shutdown()
print("\nall WebSocket tests passed")
