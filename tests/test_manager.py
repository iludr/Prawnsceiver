"""Prawntenna-manager republish on a dead rtl_tcp (radio._manager_republish).

The manager's watchdog kills a wedged rtl_tcp and frees its port, but it
never restarts it — its API.md puts republishing on the CLIENT ("if
/dongles.json shows the dongle as not published, call /publish again").
The satellite capture thread must do that itself, or one stalled dongle
leaves the receiver deaf until someone republishes by hand (observed
live 2026-10-22: both dongles' rtl_tcp died at 09:48, the manager freed
the ports, 14 min of dead air until a manual /publish).

Unit cases: unpublished+remembered -> /publish; already published -> no
call; no manager -> silent; no matching dongle -> no call; publish
failure -> warning, no exception.
Integration: the real sdr_capture_thread against a fake manager + fake
rtl_tcp. The fake rtl_tcp dies with its client (the watchdog's kill) and
the manager republishes on /publish — the thread must self-heal twice:
once at startup (nothing served yet) and once after a mid-run stall.

Run: python -u tests/test_manager.py
"""
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))

import numpy as np  # noqa: E402

from noaa_receiver import state  # noqa: E402
from noaa_receiver.sdr import radio  # noqa: E402

HOST = '127.0.0.1'
RTL_PORT = 1298          # the "published" rtl_tcp port
DID = f'{HOST}:{RTL_PORT}'


def check(name, cond, detail=""):
    assert cond, f"FAIL {name}: {detail}"
    print(f"  {name} OK {detail}")


# ---- fake prawntenna manager ----
class FakeManager:
    """Serves /dongles.json and /publish; /publish starts the fake
    rtl_tcp (a plain publish?d=ID&port=P restores remembered config)."""

    def __init__(self):
        self.published = False
        self.publish_calls = []
        self.publish_fails = False
        self.dongles_extra = []
        self.rtl = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == '/dongles.json':
                    dgs = list(outer.dongles_extra)
                    dgs.append({"id": "test123", "name": "Fake RTL",
                                "port": RTL_PORT if outer.published else None,
                                "published": outer.published,
                                "remembered_port": RTL_PORT})
                    self._json({"success": True, "dongles": dgs})
                elif u.path == '/publish':
                    outer.publish_calls.append((q.get('d', [None])[0],
                                                q.get('port', [None])[0]))
                    if outer.publish_fails:
                        self._json({"success": False,
                                    "message": "dongle not found"})
                        return
                    outer.published = True
                    # the real manager spawns a FRESH rtl_tcp on every
                    # /publish (publishing again replaces the process)
                    if outer.rtl is None or not outer.rtl.alive:
                        outer.rtl = FakeRtlTcp()
                        outer.rtl.start()
                    self._json({"success": True,
                                "message": f"rtl_tcp serving on {RTL_PORT}"})
                else:
                    self.send_response(404)
                    self.end_headers()

        self.httpd = ThreadingHTTPServer((HOST, 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def mark_unpublished(self):
        """The watchdog killed the wedged rtl_tcp."""
        self.published = False


# ---- fake rtl_tcp: header + endless IQ; dies with its client ----
class FakeRtlTcp(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, RTL_PORT))
        self.srv.listen(1)
        self.conn = None
        self.alive = True

    def run(self):
        rng = np.random.default_rng(0)
        chunk = rng.integers(100, 156, 65536, dtype=np.uint8).tobytes()
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn = conn
        try:
            conn.sendall(b'RTL0' + struct.pack('!II', 5, 29))
            conn.settimeout(0.01)
            while self.alive:
                conn.sendall(chunk)
                try:
                    cmd = conn.recv(5)
                    if not cmd:
                        break
                except socket.timeout:
                    pass
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            # client gone (stalled stream): the manager's watchdog kills
            # this rtl_tcp and frees the port
            try:
                conn.close()
            except OSError:
                pass
            self.conn = None
            try:
                self.srv.close()
            except OSError:
                pass
            self.alive = False

    def drop_client(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass

    def shutdown(self):
        """Close the listener now (the old thread's accept may still
        hold the port; the next FakeRtlTcp must be able to rebind)."""
        self.alive = False
        self.drop_client()
        try:
            self.srv.close()
        except OSError:
            pass


manager = FakeManager()
radio.MANAGER_API_PORT = manager.port     # the helper resolves it per call

print("1. unit: unpublished dongle remembered on our port -> /publish")
radio._manager_republish(HOST, RTL_PORT)
check("publish called once with the dongle id and port",
      manager.publish_calls == [('test123', str(RTL_PORT))],
      str(manager.publish_calls))
check("manager now published", manager.published)

print("2. unit: already published on our port -> no call")
manager.publish_calls.clear()
radio._manager_republish(HOST, RTL_PORT)
check("no publish call", manager.publish_calls == [], str(manager.publish_calls))

print("3. unit: manager unreachable -> silent, no exception")
radio._manager_republish(HOST, 1)        # nothing listens on port 1
check("returned without raising", True)

print("4. unit: no matching dongle -> no call")
manager.publish_calls.clear()
manager.dongles_extra = [{"id": "other", "port": None, "published": False,
                          "remembered_port": 9999}]
radio._manager_republish(HOST, RTL_PORT)
check("no publish call", manager.publish_calls == [], str(manager.publish_calls))
manager.dongles_extra = []

print("5. unit: publish failure -> warning, no exception")
manager.publish_fails = True
manager.published = False
radio._manager_republish(HOST, RTL_PORT)
check("survived the failure", manager.publish_calls[-1] == ('test123', str(RTL_PORT)))
manager.publish_fails = False
manager.published = True

print("6. integration: capture thread self-heals a dead rtl_tcp")
# Nothing serves RTL_PORT yet and the manager says unpublished: the
# thread's first connect fails, the helper must republish, and the next
# retry (backoff <= 10 s) connects to the fake rtl_tcp the manager started.
manager.published = False
manager.publish_calls.clear()
if manager.rtl is not None:
    manager.rtl.shutdown()
    manager.rtl = None

did, created = radio.add_dongle(HOST, RTL_PORT)
assert did == DID and created
entry = state.sdrs[DID]
t = threading.Thread(target=radio.sdr_capture_thread, args=(DID,),
                     daemon=True)
t.start()


def wait_for(cond, secs, what):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


check("republished at startup", wait_for(
    lambda: len(manager.publish_calls) >= 1, 10,
    'publish'), str(manager.publish_calls))
check("connected after the republish", wait_for(
    lambda: entry.get('connected'), 20, 'connect'))
check("IQ flowing (signal updates)", wait_for(
    lambda: entry.get('signal', 0) > 0, 10, 'signal'))

print("7. integration: mid-run stall -> watchdog kill -> self-heal again")
# Realistic ordering: the stream drops first, the manager's watchdog
# kills the rtl_tcp a moment later. The thread's first post-drop failure
# can still see the dongle "published" (no publish); the next failed
# retry — after the kill freed the port and the backoff — must trigger
# the republish, and the retry after that connects (backoff <= 15 s).
n_calls = len(manager.publish_calls)
manager.rtl.drop_client()                # stream stalls...
time.sleep(0.5)
manager.mark_unpublished()               # ...watchdog kills the rtl_tcp
check("republished after the stall", wait_for(
    lambda: len(manager.publish_calls) > n_calls, 25, 'publish'),
    str(manager.publish_calls[n_calls:]))
check("reconnected after the stall", wait_for(
    lambda: entry.get('connected'), 45, 'reconnect'))

print("test_manager: all OK")
os._exit(0)
