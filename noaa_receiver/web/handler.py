"""HTTP API and dashboard handler."""
import http.server
import json
import os
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .. import db
from .. import power
from .. import state
from .. import tuner
from ..config import (AUDIO_RATE, LOGDIR, LAT, LON, MANUAL_TUNE_LOCKOUT_MINS,
                   METEOR_LRPT_HZ, RECORD_DIR, RECORD_LOCAL_DIR, SDR_RATE,
                   UTC_OFFSET, WEBDIR)

from ..decoding import adsb, ais, aprs, orbcomm, pocsag
from ..sdr import scan
from ..decoding.decode import decode_recording, forget_decode_marker
from .history import (_load_history, get_recordings, quality_map,
                        recording_path, set_recording_quality)
from .pages import (ADSB_HTML, AIS_HTML, APRS_HTML, CONSOLE_HTML,
                    HISTORY_HTML, ORBCOMM_HTML, POCSAG_HTML, TUNER_HTML)
from .thumbs import THUMB_SUFFIX, ensure_thumb
from ..tracking.passes import HAS_SKYFIELD, load, passes_to_json, wgs84
from ..tracking.satnogs import satellite_info
from ..sdr.radio import (add_dongle, remove_dongle, set_dongle_adsb,
                        set_dongle_ais, set_dongle_aprs,
                        set_dongle_aprs_iss, set_dongle_aprs_lasar,
                        set_dongle_auto_sat, set_dongle_orbcomm,
                        set_dongle_pocsag, set_dongle_paused,
                        set_dongle_sat_mode, usb_dongles)

def _dongle_list():
    """Dongle descriptors for status/dongles.json: primary first, then stable
    display order. Dongles are remote rtl_tcp servers keyed by their
    "host:port" id everywhere (add/remove via /add_dongle, /remove_dongle)."""
    keys = list(state.sdrs)
    keys.sort(key=lambda s: (s != state.primary_dongle, s))
    dongles = []
    for i, did in enumerate(keys):
        e = state.sdrs[did]
        with e['lock']:
            dongles.append({
                "index": i,
                "id": did,
                "host": e["host"],
                "port": e["port"],
                "label": e["label"],
                "tuner": e["tuner"],
                "usb_name": e.get("usb_name"),
                "usb_serial": e.get("usb_serial"),
                "primary": e["primary"],
                "ais": bool(e.get("ais")),
                "orbcomm": bool(e.get("orbcomm")),
                "aprs": bool(e.get("aprs")),
                "aprs_iss": bool(e.get("aprs_iss")),
                "aprs_lasar": bool(e.get("aprs_lasar")),
                "pocsag": bool(e.get("pocsag")),
                "adsb": bool(e.get("adsb")),
                "auto_sat": bool(e.get("auto_sat")),
                "sat_mode": e.get("sat_mode") or "meteor",
                "paused": bool(e.get("paused")),
                "signal": round(e["signal"], 2),
                "connected": bool(e.get("connected")),
                "recording": e["is_recording"],
                "manual_recording": did in state.manual_recording,
                "manual_rec_since": e.get("manual_rec_start"),
                "manual_zip": e.get("manual_zip"),
                "wav": os.path.basename(e["wav_path"]) if e["wav_path"] else None,
                "correction_hz": e.get("correction", 0),
                "correction_src": e.get("correction_src", "none"),
                "manual_frequency_mhz": round(state.manual_dongle_freq[did] / 1e6, 4) if did in state.manual_dongle_freq else None,
                "manual_bw_khz": round(state.manual_dongle_bw[did] / 1000.0, 3) if did in state.manual_dongle_bw else None,
                "manual_mode": state.manual_dongle_mode.get(did),
                "scan": scan.scan_snapshot(did),
            })
    return dongles

def get_status():
    with state.status_lock:
        freq = state.current_frequency
        sat = state.current_sat_name
        passing = state.is_pass_active
        manual = state.manual_frequency
        rec_paused = state.recordings_paused
        passes = state.upcoming_passes
        cur_pass = state.current_pass
        tle = dict(state.tle_progress)
    
    dongles = _dongle_list()
    status = {
        # Capture span for the waterfall geometry (kHz the 512-bin FFT
        # covers = SDR_RATE): the dashboard scales its zoom/axis/tune
        # mapping from this instead of hardcoding the old 240 kHz
        "sdr_khz": SDR_RATE // 1000,
        # aggregate liveness: True while at least one dongle streams
        "rtl_sdr_running": any(d.get("connected") for d in dongles),
        "frequency_mhz": round(freq / 1e6, 4),
        "doppler_hz": state.doppler_hz,
        "manual_frequency_mhz": round(manual / 1e6, 4) if manual else None,
        "dongles": dongles,
        "satellite": sat,
        "pass_active": passing,
        "recording": state.is_recording,
        "recordings_paused": rec_paused,
        "recording_count": len(get_recordings()),
        "signal_strength": 0,
        "rtl_log": "",
        "next_pass": None,
        "tle": tle,
        "tle_age_min": round((time.time() - state.last_tle_refresh) / 60, 1) if state.last_tle_refresh else None,
    }
    with state.signal_lock:
        status["signal_strength"] = round(state.signal_strength, 2)

    # The two tracking modes (meteor / iss): each mode's running pass for
    # the dashboard's per-mode display, and each mode's park frequency so
    # the dongle cards can show their dongle's effective tune even while
    # no pass of that mode is up.
    mode_passes = {}
    mode_freqs = {}
    with state.status_lock:
        for m in state.SAT_MODES:
            mode_freqs[m] = round(state.mode_freq[m] / 1e6, 4)
            mp = state.mode_pass[m]
            mode_passes[m] = {
                "sat_name": mp["sat_name"],
                "catnr": mp["catnr"],
                "frequency_mhz": round(state.mode_freq[m] / 1e6, 4),
                "max_alt": mp["max_alt"],
                "set_local": (mp["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            } if mp is not None else None
    status["mode_passes"] = mode_passes
    status["mode_freqs"] = mode_freqs

    if cur_pass:
        status["current_pass"] = {
            "sat_name": cur_pass["sat_name"],
            "catnr": cur_pass["catnr"],
            "frequency_mhz": round(cur_pass["frequency"] / 1e6, 4),
            "max_alt": cur_pass["max_alt"],
            "set_local": (cur_pass["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
        }
    else:
        status["current_pass"] = None
    
    # Find next upcoming pass
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    for p in passes:
        if p["rise_utc"] > now:
            status["next_pass"] = {
                "sat_name": p["sat_name"],
                "frequency_mhz": round(p["frequency"] / 1e6, 4),
                "rise_local": (p["rise_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
                "max_alt": p["max_alt"],
                "duration_min": p["duration_min"],
                "countdown_min": round((p["rise_utc"] - now).total_seconds() / 60),
            }
            break
    
    return status


def _passes_data():
    """Upcoming passes for /passes.json and the WS 'passes' topic."""
    with state.status_lock:
        return passes_to_json(state.upcoming_passes)


def _track_data():
    """Polar (az/el) track of the currently active pass for /track.json
    and the WS 'track' topic."""
    with state.status_lock:
        cur = state.current_pass
    result = {"active": False, "sat_name": None, "track": [], "now": None}
    if cur is not None:
        result["active"] = True
        result["sat_name"] = cur["sat_name"]
        if HAS_SKYFIELD and cur.get("sat") is not None:
            try:
                ts = load.timescale()
                site = wgs84.latlon(LAT, LON)
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                start, end = cur["rise_utc"], cur["set_utc"]
                span = (end - start).total_seconds()
                if span > 0:
                    steps = max(int(span // 30), 1)
                    times = [start + timedelta(seconds=span * k / steps) for k in range(steps + 1)]
                    t = ts.from_datetimes(times)
                    alt, az, _ = (cur["sat"] - site).at(t).altaz()
                    result["track"] = [
                        {"t": times[i].timestamp(),
                         "alt": round(alt.degrees[i], 1),
                         "az": round(az.degrees[i], 1)}
                        for i in range(len(times))
                    ]
                tn = ts.from_datetime(now)
                altn, azn, _ = (cur["sat"] - site).at(tn).altaz()
                result["now"] = {"t": now.timestamp(), "alt": round(altn.degrees, 1), "az": round(azn.degrees, 1)}
            except Exception as e:
                state.log_console(f"Track computation failed: {e}", "error")
    return result


def _power_data():
    """Power state for /power.json and the WS 'power' topic: the
    receiver host's own batteries (/sys/class/power_supply — the
    notebook case) plus the cached PiSugar batteries of the TCP dongle
    hosts (probed in the background by power.power_refresh_thread —
    see noaa_receiver/power.py). Machines without any battery return
    empty lists and the dashboard hides the display."""
    return power.power_data()


def _console_lines():
    """Console ring for /console.json and the WS 'console' topic."""
    with state.console_lock:
        return list(state.console_buffer)


def _aislog_count(param):
    """Row count requested for the WS 'aislog' topic ('aislog=200')."""
    try:
        return max(1, min(2000, int(param)))
    except (TypeError, ValueError):
        return 200


# WebSocket JSON topic push: one entry per pushable topic -
# (cadence in seconds, payload builder taking the subscription
# parameter). The builders are the same ones the HTTP endpoints serve,
# so a push and a fetch always agree.
_WS_TOPICS = {
    'dongles':      (1.0,  lambda p: _dongle_list()),
    'status':       (3.0,  lambda p: get_status()),
    'passes':       (30.0, lambda p: _passes_data()),
    'track':        (2.0,  lambda p: _track_data()),
    'ais':          (10.0, lambda p: ais.ais_status()),
    'orbcomm':      (5.0,  lambda p: orbcomm.orbcomm_status()),
    'aprs':         (5.0,  lambda p: aprs.aprs_status()),
    'power':        (15.0, lambda p: _power_data()),
    'recordings':   (10.0, lambda p: get_recordings()),
    'console':      (2.0,  lambda p: _console_lines()),
    'aislog':       (10.0, lambda p: ais.ais_log(_aislog_count(p))),
    'ais_ships':    (30.0, lambda p: ais.ships_registry()),
    'orbcommlog':   (10.0, lambda p: orbcomm.orbcomm_log(_aislog_count(p))),
    'aprslog':      (10.0, lambda p: aprs.aprs_log(_aislog_count(p))),
    'aprs_stations': (30.0, lambda p: aprs.stations_registry()),
    'aprsmsgs':     (10.0, lambda p: aprs.messages_view(_aislog_count(p))),
    'pocsag':       (5.0,  lambda p: pocsag.pocsag_status()),
    'pocsaglog':    (10.0, lambda p: pocsag.pocsag_log(_aislog_count(p))),
    'adsb':         (5.0,  lambda p: adsb.adsb_status()),
    'adsblog':      (10.0, lambda p: adsb.adsb_log(_aislog_count(p))),
    'adsb_aircrafts': (30.0, lambda p: adsb.aircrafts_registry()),
    'pass_history': (8.0,  lambda p: _load_history()),
}


class NOAAHandler(http.server.SimpleHTTPRequestHandler):
    _WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

    @staticmethod
    def _ws_frame(opcode, payload):
        """One RFC 6455 server frame: FIN + opcode, unmasked (server
        frames never are), MINIMAL length encoding - inline up to 125
        bytes, 16-bit for the dB rows (~1 kB), 64-bit for large JSON
        topic payloads (aislog/ais_ships/pass_history exceed 64 kB).
        The minimal form is not cosmetic: RFC 6455 5.2 requires it and
        Chromium/Edge reject non-minimal lengths as "Invalid frame
        header", dropping the connection mid-stream."""
        ln = len(payload)
        if ln <= 125:
            return bytes([0x80 | opcode, ln]) + payload
        if ln <= 0xFFFF:
            return bytes([0x80 | opcode, 126]) + ln.to_bytes(2, 'big') + payload
        return bytes([0x80 | opcode, 127]) + ln.to_bytes(8, 'big') + payload

    def _waterfall_websocket(self):
        """WebSocket push of dBFS spectrum rows: replaces the dashboard's
        200 ms /waterfall.json polling with a live stream at up to the
        capture rate (~234 rows/s at 2.4 Msps; the client sets its own
        rate with a text frame like 'rate=60').

        Protocol: one binary frame per dongle per tick - [type=1][u16be
        id_len][id utf8][512 x int16le, dB*10]. The 512 int16 replace
        the JSON number arrays of the polling endpoint (bytes, not
        text; ~1 kB vs ~5 kB per row, and no JSON parsing either side).
        Per-connection thread (this handler thread lives until the
        socket closes); a slow client is simply skipped rows (the tick
        always sends the newest one - no backlog can build).

        JSON topics: a client subscribes with a text frame
        'sub=topic,topic=param' and receives
        [type=2][u16be topic_len][topic utf8][json utf8] frames - each
        topic immediately after subscribing, then on its own cadence
        (see _WS_TOPICS). This moves ALL recurring page communication
        (status, dongles, passes, track, AIS, power, recordings,
        console, history) onto the socket; the HTTP endpoints remain
        as the fallback while the socket is down.
        """
        import base64
        import hashlib
        key = self.headers.get('Sec-WebSocket-Key')
        if not key or 'websocket' not in (self.headers.get('Upgrade') or '').lower():
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'expected a websocket upgrade')
            return
        accept = base64.b64encode(hashlib.sha1(
            (key + self._WS_GUID).encode()).digest()).decode()
        self.send_response(101, 'Switching Protocols')
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        state.log_console(f"Waterfall WebSocket client connected ({self.client_address[0]})")
        conn = self.connection
        try:
            conn.setblocking(False)
        except OSError:
            self.close_connection = True
            return
        rate = 60.0          # rows/s per dongle until the client says otherwise
        last_seq = {}
        subs = {}            # topic -> subscription parameter (or None)
        last_topic = {}      # topic -> last push time (0 = push now)
        buf = bytearray()
        tick = 0.0
        why = 'loop exit'          # disconnect diagnosis, logged in finally
        try:
            while True:
                # client frames, non-blocking: 'rate=N' control, ping,
                # close, or a dropped connection (recv() == b'')
                try:
                    data = conn.recv(4096)
                    if not data:
                        why = 'client EOF (nginx/browser closed first)'
                        break
                    buf += data
                    while len(buf) >= 2:
                        op = buf[0] & 0x0f
                        masked = buf[1] & 0x80   # client frames MUST mask
                        ln = buf[1] & 0x7f
                        off = 2
                        if ln == 126:
                            if len(buf) < 4:
                                break
                            ln = int.from_bytes(buf[2:4], 'big'); off = 4
                        elif ln == 127:
                            if len(buf) < 10:
                                break
                            ln = int.from_bytes(buf[2:10], 'big'); off = 10
                        if masked:
                            if len(buf) < off + 4:
                                break
                            mask = bytes(buf[off:off + 4])
                            off += 4
                        if len(buf) < off + ln:
                            break
                        if masked:
                            payload = bytes(b ^ mask[i % 4]
                                            for i, b in enumerate(buf[off:off + ln]))
                        else:
                            payload = bytes(buf[off:off + ln])
                        del buf[:off + ln]
                        if op == 8:          # close
                            code = int.from_bytes(payload[:2], 'big') if len(payload) >= 2 else 0
                            why = f'close frame code {code}'
                            break
                        elif op == 9:        # ping -> pong
                            conn.sendall(self._ws_frame(10, payload))
                        elif op == 1:        # text: 'rate=N' / 'sub=a,b=c'
                            txt = payload.decode(errors='replace').strip()
                            if txt.startswith('sub='):
                                # (re)subscribe: the whole set at once;
                                # last_topic = 0 pushes each immediately,
                                # then on its own cadence
                                subs.clear()
                                for part in txt[4:].split(','):
                                    part = part.strip()
                                    if not part:
                                        continue
                                    topic, _, param = part.partition('=')
                                    if topic in _WS_TOPICS:
                                        subs[topic] = param or None
                                        last_topic[topic] = 0.0
                            else:
                                try:
                                    rate = max(5.0, min(234.0,
                                            float(txt.split('=')[-1])))
                                except ValueError:
                                    pass
                except BlockingIOError:
                    pass
                now = time.time()
                # JSON topic pushes: every subscribed topic on its own
                # cadence, checked on each loop pass (~500 Hz) so they
                # are independent of the row rate
                for topic in list(subs):
                    interval, build = _WS_TOPICS[topic]
                    if now - last_topic.get(topic, 0.0) < interval:
                        continue
                    last_topic[topic] = now
                    try:
                        payload = build(subs[topic])
                    except Exception:
                        continue          # one bad topic never kills the socket
                    tb = topic.encode()
                    frame = (bytes([2]) + len(tb).to_bytes(2, 'big')
                             + tb + json.dumps(payload).encode())
                    try:
                        conn.sendall(self._ws_frame(2, frame))
                    except (BlockingIOError, OSError):
                        break
                if now - tick >= 1.0 / rate:
                    tick = now
                    for did, e in list(state.sdrs.items()):
                        marks = []
                        with e['lock']:
                            seq = e.get('wf_db_seq', 0)
                            row = e['wf_db'][-1] if len(e['wf_db']) else None
                            if e.get('wf_marks'):
                                # AIS frame markers: drained with the row
                                # tick so the client can bake the red box
                                # into the canvas top, on the matching line
                                marks = list(e['wf_marks'])
                                e['wf_marks'].clear()
                        if marks:
                            did_m = did.encode()
                            for khz in marks:
                                try:
                                    conn.sendall(self._ws_frame(2,
                                        bytes([3]) + len(did_m).to_bytes(2, 'big')
                                        + did_m + int(round(khz * 10)).to_bytes(2,
                                            'little', signed=True)))
                                except (BlockingIOError, OSError):
                                    break
                        if row is None or seq <= last_seq.get(did, 0):
                            continue
                        last_seq[did] = seq
                        did_b = did.encode()
                        payload = (bytes([1]) + len(did_b).to_bytes(2, 'big')
                                   + did_b + row)
                        try:
                            conn.sendall(self._ws_frame(2, payload))
                        except (BlockingIOError, OSError):
                            # client stalled: drop this tick's rows - the
                            # next tick continues with the newest one
                            break
                else:
                    # no tick fired: do not busy-spin the recv loop
                    time.sleep(0.002)
        except OSError as e:
            why = f'socket error {e!r}'
        finally:
            self.close_connection = True
            state.log_console(f"Waterfall WebSocket client disconnected ({self.client_address[0]}, {why})")

    def _send_json(self, obj, code=200):
        """The one JSON response shape every API route answers in:
        status code, content-type, CORS (the dashboard pages sit behind
        the notebook's prefix proxy, so cross-origin reads stay allowed),
        then the encoded payload."""
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _query(self):
        """Parsed query string of the current request."""
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        if self.path == '/status.json':
            self._send_json(get_status(), code=200)
        elif self.path == '/usb_dongles.json':
            # USB dongles on this host as rtl_sdr itself sees them
            # (device index + name + serial) - feeds the header's
            # add-dongle picker and its refresh button
            self._send_json({'dongles': usb_dongles()}, code=200)
        elif self.path == '/power.json':
            # Host power state (own batteries + the dongle hosts'
            # PiSugar batteries) - see _power_data()
            self._send_json(_power_data(), code=200)
        elif self.path.split('?')[0] == '/waterfall.ws':
            # WebSocket push of dBFS spectrum rows (see _waterfall_websocket)
            self._waterfall_websocket()
        elif self.path.startswith('/waterfall.json'):
            # Optional ?d=<dongle serial> (default: primary) and last=1 for
            # the newest row only (the dashboard scrolls client-side)
            query = self._query()
            dev = (query.get('d') or [''])[0] or (state.primary_dongle or '')
            entry = state.sdrs.get(dev)
            if entry is None:
                data = []
            elif (query.get('last') or [''])[0] == '1':
                with entry['lock']:
                    # deque supports indexing but not slicing
                    data = [entry['waterfall'][-1]] if entry['waterfall'] else []
            else:
                with entry['lock']:
                    data = list(entry['waterfall'])
            self._send_json(data, code=200)
        elif self.path == '/dongles.json':
            self._send_json(_dongle_list(), code=200)
        elif self.path == '/passes.json':
            passes = _passes_data()
            self._send_json(passes, code=200)
        elif self.path == '/recordings.json':
            self._send_json(get_recordings(), code=200)
        elif self.path == '/signal_history.json':
            with state.signal_lock:
                history = list(state.signal_history)
            self._send_json(history, code=200)
        elif self.path == '/pass_history.json':
            history = _load_history()
            self._send_json(history, code=200)
        elif self.path == '/track.json':
            # Polar (az/el) track of the currently active pass - see
            # _track_data() (shared with the WS 'track' topic)
            result = _track_data()
            self._send_json(result, code=200)
        elif self.path == '/ais.json':
            # Danube ship traffic: ship table + per-channel stats + raw feed
            self._send_json(ais.ais_status(), code=200)
        elif self.path == '/orbcomm.json':
            # Orbcomm satellite downlink: tracked channels (satellite ID,
            # live position from the Ephemeris packets) + recent packets
            self._send_json(orbcomm.orbcomm_status(), code=200)
        elif self.path.startswith('/orbcommlog.json'):
            # Tail of the persistent decoded-packet log (?count=N, max 2000)
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(orbcomm.orbcomm_log(count), code=200)
        elif self.path == '/aprs.json':
            # APRS: heard-station registry (callsigns, positions) +
            # recent frames
            self._send_json(aprs.aprs_status(), code=200)
        elif self.path == '/pocsag.json':
            # POCSAG: tracked paging channels + recent pages
            self._send_json(pocsag.pocsag_status(), code=200)
        elif self.path == '/adsb.json':
            # ADS-B: live aircraft table + receiver stats + raw frames
            self._send_json(adsb.adsb_status(), code=200)
        elif self.path.startswith('/tuner.json'):
            query = self._query()
            dev = (query.get('d') or [''])[0]
            self._send_json(tuner.tuner_snapshot(dev), code=200)
        elif self.path == '/adsb_aircrafts.json':
            # ADS-B: aircraft heard in the last 24 h (persistent
            # registry restored from the adsb_aircrafts table)
            self._send_json(adsb.aircrafts_registry(), code=200)
        elif self.path.startswith('/aprslog.json'):
            # Tail of the persistent decoded-frame log (?count=N, max 2000)
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(aprs.aprs_log(count), code=200)
        elif self.path.startswith('/pocsaglog.json'):
            # Newest-first tail of the persistent decoded-page log
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(pocsag.pocsag_log(count), code=200)
        elif self.path.startswith('/adsblog.json'):
            # Newest-first tail of the persistent decoded-frame log
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(adsb.adsb_log(count), code=200)
        elif self.path.startswith('/aprsmsgs.json'):
            # Person-to-person APRS text messages (':ADDRESSEE:text'
            # frames from the persistent log, telemetry definitions
            # filtered out, acks flagged)
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(aprs.messages_view(count), code=200)
        elif self.path == '/aprs_stations.json':
            # Persistent registry: every APRS station ever received,
            # each with its latest data and the last APRS_RECENT_MSGS
            # frames
            self._send_json(aprs.stations_registry(), code=200)
        elif self.path == '/ais_ships.json':
            # Persistent registry: every ship ever received, each with
            # its latest data and the last AIS_RECENT_MSGS messages
            self._send_json(ais.ships_registry(), code=200)
        elif self.path == '/console.json':
            lines = _console_lines()
            self._send_json(lines, code=200)
        elif self.path == '/console' or self.path == '/console.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(CONSOLE_HTML.encode())
        elif self.path == '/ais' or self.path == '/ais.html':
            # AIS tracking page: every received frame, persistent log
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(AIS_HTML.encode())
        elif self.path == '/orbcomm' or self.path == '/orbcomm.html':
            # Orbcomm log page: every decoded satellite packet from the
            # persistent log, plus the live channel registry
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(ORBCOMM_HTML.encode())
        elif self.path == '/aprs' or self.path == '/aprs.html':
            # APRS log page: every decoded frame from the persistent
            # log, plus the live station registry
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(APRS_HTML.encode())
        elif self.path == '/pocsag' or self.path == '/pocsag.html':
            # POCSAG page: tracked paging channels + decoded pages
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(POCSAG_HTML.encode())
        elif self.path == '/tuner' or self.path == '/tuner.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(TUNER_HTML.encode())
        elif self.path == '/adsb' or self.path == '/adsb.html':
            # ADS-B page: live aircraft map + aircraft table + frame log
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(ADSB_HTML.encode())
        elif self.path.split('?')[0] == '/ship_tracks.json':
            # 24 h position tracks for the AIS page's 'path on map'
            # multi-select: ?mmsi=123,456&hours=24
            query = self._query()
            mmsis = [m.strip() for m in (query.get('mmsi') or [''])[0].split(',')
                      if m.strip()][:50]
            try:
                hours = float((query.get('hours') or ['24'])[0])
            except ValueError:
                hours = 24.0
            tracks = ais.ship_tracks(mmsis, hours)
            self._send_json(tracks, code=200)

        elif self.path.split('?')[0] == '/adsb_tracks.json':
            # 24 h position tracks for the ADS-B page's 'path on map'
            # multi-select: ?icao=4CA251,3C6DD0&hours=24
            query = self._query()
            icaos = [i.strip() for i in (query.get('icao') or [''])[0].split(',')
                      if i.strip()][:50]
            try:
                hours = float((query.get('hours') or ['24'])[0])
            except ValueError:
                hours = 24.0
            tracks = adsb.aircraft_tracks(icaos, hours)
            self._send_json(tracks, code=200)

        elif self.path.split('?')[0] == '/adsb_coverage.json':
            # Convex hull of every aircraft position in the persistent
            # registry (adsb_aircrafts, all-time) — the ADS-B map's
            # coverage shape
            self._send_json(adsb.coverage_hull(), code=200)

        elif self.path.startswith('/aislog.json'):
            # Tail of the persistent AIS message log (?count=N, max 2000)
            query = self._query()
            try:
                count = int((query.get('count') or ['200'])[0])
            except ValueError:
                count = 200
            self._send_json(ais.ais_log(count), code=200)
        elif self.path == '/history' or self.path == '/history.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HISTORY_HTML.encode())
        elif self.path.startswith('/manual/'):
            # Manual raw-IQ recording zips (dashboard Record button):
            # the finished analysis packages (raw IQ + description.json
            # + waterfall.png), auto-downloaded by the dashboard when a
            # recording stops. One sanitized path segment, no traversal.
            from urllib.parse import unquote
            filename = unquote(self.path[8:]).split('?', 1)[0]
            if '..' in filename or '/' in filename or not filename.endswith('.zip'):
                self.send_response(400)
                self.end_headers()
                return
            zpath = recording_path('manual', filename)
            if not os.path.isfile(zpath):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Recording zip not found')
                return
            self.send_response(200)
            self.send_header('Content-type', 'application/zip')
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % filename)
            self.send_header('Content-Length', str(os.path.getsize(zpath)))
            self.end_headers()
            with open(zpath, 'rb') as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        elif self.path.startswith('/audio/'):
            filename = self.path[7:]
            if '..' in filename or '/' in filename:
                self.send_response(400)
                self.end_headers()
                return
            wav_path = recording_path(filename)
            if not os.path.exists(wav_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Audio not found')
                return
            # Never serve a recording that is still being written: its WAV
            # header is stale the moment we read it, which makes browsers
            # play a few seconds and show a full progress bar.
            with state.status_lock:
                recording_now = state.is_recording and state.current_wav_path == wav_path
            if recording_now:
                self._send_json({"success": False, "error": "Recording in progress"}, code=409)
                return
            size = os.path.getsize(wav_path)
            # Audio elements need Content-Length + range support to seek
            range_header = self.headers.get('Range')
            start, end = 0, size - 1
            partial = False
            if range_header and range_header.startswith('bytes='):
                try:
                    r = range_header[6:].split('-')
                    start = int(r[0]) if r[0] else 0
                    end = int(r[1]) if r[1] else size - 1
                    if start >= size or start > end:
                        raise ValueError
                    end = min(end, size - 1)
                    partial = True
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self.send_header('Content-type', 'audio/wav')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            with open(wav_path, 'rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        elif self.path.split('?')[0] == '/add_dongle':
            # Register a dongle by its rtl_tcp server address (the daemon
            # runs on the machine the dongle is plugged into):
            # /add_dongle?host=192.168.3.245&port=1234
            # A dongle plugged into the receiver host itself is registered
            # by its rtl_sdr device index instead: /add_dongle?host=usb&port=0
            query = self._query()
            host = (query.get('host') or query.get('ip') or [''])[0].strip()
            port = (query.get('port') or [''])[0].strip()
            try:
                did, created = add_dongle(host, port)
            except ValueError as e:
                self._send_json({'success': False, 'error': str(e)}, code=400)
                return
            self._send_json({'success': True, 'id': did, 'created': created}, code=200)
        elif self.path.split('?')[0] == '/remove_dongle':
            # Unregister a dongle: /remove_dongle?d=192.168.3.245:1234
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            removed = remove_dongle(dev)
            self._send_json({'success': removed}, code=200 if removed else 404)
        elif self.path.split('?')[0] == '/ais_dongle':
            # Switch one dongle between satellite tracking and AIS ship
            # traffic (dashboard button on the dongle card — with a single
            # dongle this is how it listens for AIS): /ais_dongle?d=<id>&on=1
            # dedicates it to 161.975/162.025 MHz, on=0 returns it to the
            # satellites. The capture thread restarts in the new role.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_ais(dev, on)
            self._send_json({'success': True, 'ais': bool(entry.get('ais'))}, code=200)
        elif self.path.split('?')[0] == '/orbcomm_dongle':
            # Switch one dongle between satellite tracking and Orbcomm
            # satellite-IoT downlink reception (dashboard button on the
            # dongle card, mirroring the AIS button):
            # /orbcomm_dongle?d=<id>&on=1 parks it on 137.5 MHz — one
            # 2.4 MHz capture covers the whole 137.0-138.0 MHz downlink
            # band — on=0 returns it to the satellites. The capture
            # thread restarts in the new role.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_orbcomm(dev, on)
            self._send_json({'success': True, 'orbcomm': bool(entry.get('orbcomm'))}, code=200)
        elif self.path.split('?')[0] == '/aprs_dongle':
            # Switch one dongle between satellite tracking and the APRS
            # role (dashboard button, mirroring the AIS/Orbcomm ones):
            # /aprs_dongle?d=<id>&on=1 parks it on 144.800 MHz and
            # decodes Bell 202 AFSK AX.25 frames; on=0 returns it to
            # the satellites. The capture thread restarts in the new role.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_aprs(dev, on)
            self._send_json({'success': True, 'aprs': bool(entry.get('aprs'))}, code=200)
        elif self.path.split('?')[0] == '/aprs_iss':
            # Flip one dongle's APRS channel between the DL simplex
            # channel (144.800 MHz) and the ISS digipeater
            # (145.825 MHz) — the checkbox below the APRS button.
            # /aprs_iss?d=<id>&on=1 selects the ISS channel and
            # retunes a running APRS capture; on=0 returns to 144.800.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_aprs_iss(dev, on)
            self._send_json({'success': True, 'aprs_iss': bool(entry.get('aprs_iss'))}, code=200)
        elif self.path.split('?')[0] == '/aprs_lasar':
            # Flip one dongle's APRS channel to the LASARsat digipeater
            # (436.925 MHz, GFSK 9600 AX.25 G3RUH) — the second checkbox
            # below the APRS button, mutually exclusive with the ISS
            # preset. /aprs_lasar?d=<id>&on=1 retunes a running APRS
            # capture AND swaps its demodulator (Bell 202 AFSK 1200 ->
            # GFSK 9600); on=0 returns to 144.800 MHz.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_aprs_lasar(dev, on)
            self._send_json({'success': True, 'aprs_lasar': bool(entry.get('aprs_lasar'))}, code=200)
        elif self.path.split('?')[0] == '/pause_dongle':
            # Temporarily stop one dongle's capture (dashboard
            # pause/play button): two dongles recording at the same
            # time overload the dongle host's shared USB bus.
            # /pause_dongle?d=<id>&on=1 closes the stream and any
            # recording (the supervisor restarts the capture on
            # on=0, within ~2 s). The dongle stays registered.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_paused(dev, on)
            self._send_json({'success': True, 'paused': bool(entry.get('paused'))}, code=200)
        elif self.path.split('?')[0] == '/auto_sat_dongle':
            # Toggle one dongle's auto-join satellite passes (the checkbox
            # under the role dropdown on the dongle card):
            # /auto_sat_dongle?d=<id>&on=1 — while the dongle listens in a
            # dedicated role (AIS/ADS-B/...), it leaves that role when a
            # pass starts and returns to it after set. The flag persists
            # in the dongles table.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            on = (query.get('on') or [''])[0].strip().lower() not in ('', '0', 'false', 'off', 'no')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_auto_sat(dev, on)
            self._send_json({'success': True, 'auto_sat': bool(entry.get('auto_sat'))}, code=200)
        elif self.path.split('?')[0] == '/sat_mode':
            # Set one dongle's satellite tracking mode (the role dropdown's
            # Meteor/ISS options on the dongle card):
            # /sat_mode?d=<id>&mode=meteor|iss — which pass family this
            # dongle follows (the two modes replaced the single
            # auto-tracking mode). The mode persists in the dongles table.
            query = self._query()
            dev = (query.get('d') or [''])[0].strip()
            mode = (query.get('mode') or [''])[0].strip().lower()
            if mode not in state.SAT_MODES:
                self._send_json({'success': False,
                                 'error': "mode must be one of: " + ", ".join(state.SAT_MODES)}, code=400)
                return
            if dev not in state.sdrs:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            set_dongle_sat_mode(dev, mode)
            self._send_json({'success': True, 'sat_mode': mode}, code=200)
        elif self.path.split('?')[0] == '/tune':
            # Manual tune (dongle reception test, e.g. FM broadcast radio):
            # /tune?f=89.7 parks the dongle on a frequency and pauses the
            # satellite scheduler; /tune?f=auto hands control back to it.
            query = self._query()
            f = (query.get('f') or [''])[0].strip().lower()
            with state.status_lock:
                pass_active = state.is_pass_active
            if f in ('', 'auto'):
                with state.status_lock:
                    state.manual_frequency = None
                    # During a pass the scheduler owns the frequency: parking
                    # here would record the idle band under the pass's name
                    if not state.is_pass_active:
                        state.current_frequency = METEOR_LRPT_HZ
                        state.current_sat_name = "idle"
                state.log_console("🛰 Manual tune off — satellite tracking resumed")
                self._send_json({"success": True, "mode": "auto"}, code=200)
                return
            try:
                mhz = float(f)
            except ValueError:
                self._send_json({"success": False, "error": "Invalid frequency"}, code=400)
                return
            if not 24.0 <= mhz <= 1766.0:
                self._send_json({"success": False, "error": "Frequency out of R820T range (24-1766 MHz)"}, code=400)
                return
            if pass_active:
                state.log_console("Tune rejected: satellite pass in progress", "warn")
                self._send_json({"success": False, "error": "Satellite pass in progress — try again after it ends"}, code=409)
                return
            # Auto-reject near a predicted pass too: a manual tune pauses the
            # scheduler, so a tune that sticks right before a rise silently
            # skips that pass (f=auto above is never blocked)
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            lockout = MANUAL_TUNE_LOCKOUT_MINS * 60
            for p in state.upcoming_passes:
                rise = p.get("rise_utc")
                if rise is None or rise <= now:
                    continue
                if (rise - now).total_seconds() <= lockout:
                    mins = max(1, round((rise - now).total_seconds() / 60))
                    state.log_console(f"Tune rejected: next pass {p.get('sat_name')} rises in {mins} min", "warn")
                    self._send_json({"success": False, "error": f"Next pass {p.get('sat_name')} rises in {mins} min — tune again after it ends (Auto is still available)"}, code=409)
                    return
                break  # passes are sorted: the first future one decides
            freq_hz = int(round(mhz * 1e6))
            with state.status_lock:
                state.manual_frequency = freq_hz
                state.current_frequency = freq_hz
                state.current_sat_name = f"Manual {mhz:.4f} MHz"
            state.log_console(f"📻 Manual tune: {mhz:.4f} MHz — satellite tracking paused")
            self._send_json({"success": True, "mode": "manual", "frequency_mhz": mhz}, code=200)
        elif self.path.split('?')[0] == '/sync_tle':
            # Manual TLE refresh: sets a flag; the scheduler picks it up
            # within its 10 s tick and refetches + re-predicts. The network
            # fetch can take minutes, so it must never block this handler.
            if state.tle_sync_requested or (state.tle_progress or {}).get("active"):
                self._send_json({"success": False, "error": "A TLE sync is already running"}, code=409)
                return
            state.tle_sync_requested = True
            state.log_console("🔄 Manual TLE sync requested — fetching fresh elements")
            self._send_json({"success": True, "mode": "queued"}, code=200)
        elif self.path.split('?')[0] == '/record_pause':
            # Global recording pause (dashboard switch): paused=1 stops the
            # capture threads from opening WAV/IQ files during passes (a
            # running WAV is closed within ~one IQ block); paused=0 resumes.
            # Tracking, waterfall, Doppler and live audio keep running either
            # way, so reception quality stays observable while paused.
            query = self._query()
            p = (query.get('paused') or [''])[0].strip().lower()
            paused = p in ('1', 'true', 'on', 'yes')
            with state.status_lock:
                state.recordings_paused = paused
            if paused:
                state.log_console("⏸ Automatic recordings paused — passes are received but not written to disk")
            else:
                state.log_console("▶ Automatic recordings resumed")
            self._send_json({"success": True, "recordings_paused": paused}, code=200)
        elif self.path.startswith('/tune_dongle'):
            # Per-dongle manual frequency override:
            # /tune_dongle?d=<serial>&f=<mhz> tunes just that dongle (e.g.
            # to compare receive quality around a signal); f=auto clears
            # the override and the dongle rejoins the shared frequency.
            query = self._query()
            dev = (query.get('d') or [''])[0]
            f = (query.get('f') or [''])[0].strip().lower()
            bw = (query.get('bw') or [''])[0].strip().lower()
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({"success": False, "error": "Unknown dongle"}, code=404)
                return
            # Optional demod (recorded) bandwidth override in kHz — the IQ
            # low-pass cutoff ahead of the FM discriminator. bw=auto clears.
            # A bw-only request (no f) leaves the frequency untouched.
            if bw:
                bw_hz = None
                if bw not in ('auto', 'sync'):
                    try:
                        khz = float(bw)
                    except ValueError:
                        self._send_json({"success": False, "error": "Invalid bandwidth"}, code=400)
                        return
                    if not 1.0 <= khz <= 120.0:
                        self._send_json({"success": False, "error": "Bandwidth out of range (1-120 kHz)"}, code=400)
                        return
                    bw_hz = int(khz * 1000)
                with state.status_lock:
                    if bw_hz is None:
                        state.manual_dongle_bw.pop(dev, None)
                    else:
                        state.manual_dongle_bw[dev] = bw_hz
            # Optional demod mode override: am/usb/lsb/cw/fm, auto clears
            # (auto = FM everywhere except the airband, which is AM).
            # A mode-only request leaves frequency and bandwidth untouched.
            mode = (query.get('mode') or [''])[0].strip().lower()
            if mode:
                if mode not in ('auto', 'am', 'usb', 'lsb', 'cw', 'fm'):
                    self._send_json({'success': False, 'error': 'Unknown mode (auto/am/usb/lsb/cw/fm)'}, code=400)
                    return
                with state.status_lock:
                    if mode == 'auto':
                        state.manual_dongle_mode.pop(dev, None)
                    else:
                        state.manual_dongle_mode[dev] = mode
                state.log_console(f"Dongle {dev} demod mode: {mode}")
                if not f:
                    self._send_json({'success': True, 'mode': 'demod', 'demod_mode': mode}, code=200)
                    return
                state.log_console(f"🎚 Dongle {dev} demod bandwidth: {bw_hz / 1000:g} kHz" if bw_hz else f"🎚 Dongle {dev} demod bandwidth back to auto")
                if not f:
                    self._send_json({"success": True, "mode": "bw", "bandwidth_khz": bw_hz / 1000 if bw_hz else None}, code=200)
                    return
            if entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') \
                    or entry.get('pocsag') or entry.get('adsb'):
                # A dongle dedicated to AIS or Orbcomm reception is parked
                # on its mode's band - retuning it would stop reception
                self._send_json({'success': False, 'error': 'Dongle is dedicated to AIS'}, code=400)
                return
            if f in ('', 'auto', 'sync'):
                with state.status_lock:
                    state.manual_dongle_freq.pop(dev, None)
                state.log_console(f"🎛 Dongle {dev} frequency override cleared — back to the shared frequency")
                self._send_json({"success": True, "mode": "sync"}, code=200)
                return
            try:
                mhz = float(f)
            except ValueError:
                self._send_json({"success": False, "error": "Invalid frequency"}, code=400)
                return
            if not 24.0 <= mhz <= 1766.0:
                self._send_json({"success": False, "error": "Frequency out of R820T range (24-1766 MHz)"}, code=400)
                return
            with state.status_lock:
                state.manual_dongle_freq[dev] = int(round(mhz * 1e6))
            state.log_console(f"🎛 Dongle {dev} frequency override: {mhz:.4f} MHz")
            self._send_json({"success": True, "mode": "manual", "frequency_mhz": mhz}, code=200)
        elif self.path.split('?')[0] == '/scan_dongle':
            # Frequency scan for one dongle (dashboard card Scan button):
            # /scan_dongle?d=<serial>&start=<mhz>&end=<mhz>&step=<khz>&ratio=<n>
            # sweeps the dongle's override from start to end and parks it on
            # the next strong signal (see scan.py); &stop=1 stops a running
            # scan at its current frequency (Sync on the card rejoins).
            query = self._query()
            dev = (query.get('d') or [''])[0]
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            if entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') \
                    or entry.get('pocsag') or entry.get('adsb'):
                # A dongle dedicated to AIS or Orbcomm reception is parked
                # on its mode's band - scanning it would stop reception
                self._send_json({'success': False, 'error': 'Dongle is dedicated to AIS'}, code=400)
                return
            if (query.get('stop') or [''])[0].strip().lower() in ('1', 'true', 'on', 'yes'):
                stopped = scan.stop_scan(dev)
                if stopped:
                    state.log_console(f'Scan stop requested (dongle {dev})')
                self._send_json({'success': True, 'stopped': stopped}, code=200)
                return
            f_start = (query.get('start') or [''])[0].strip()
            f_end = (query.get('end') or [''])[0].strip()
            f_step = (query.get('step') or ['200'])[0].strip()
            f_ratio = (query.get('ratio') or [''])[0].strip()
            try:
                start_mhz = float(f_start)
                end_mhz = float(f_end)
                step_khz = float(f_step)
                ratio = float(f_ratio) if f_ratio else scan.SCAN_RATIO
            except ValueError:
                self._send_json({'success': False, 'error': 'Invalid scan parameters (start/end/step/ratio)'}, code=400)
                return
            if not (24.0 <= start_mhz <= 1766.0 and 24.0 <= end_mhz <= 1766.0):
                self._send_json({'success': False, 'error': 'Frequencies out of R820T range (24-1766 MHz)'}, code=400)
                return
            if start_mhz == end_mhz:
                self._send_json({'success': False, 'error': 'Scan start and end must differ'}, code=400)
                return
            if not scan.STEP_MIN_HZ / 1000.0 <= step_khz <= scan.STEP_MAX_HZ / 1000.0:
                self._send_json({'success': False, 'error': f'Step out of range ({scan.STEP_MIN_HZ // 1000}-{scan.STEP_MAX_HZ // 1000} kHz)'}, code=400)
                return
            if not 1.5 <= ratio <= 20.0:
                self._send_json({'success': False, 'error': 'Ratio out of range (1.5-20)'}, code=400)
                return
            with state.status_lock:
                pass_active = state.is_pass_active
            if entry['primary'] and pass_active:
                # Scanning the primary suppresses its pass recording - the
                # same protection /tune has (reject while a pass is up; the
                # scan thread also aborts itself when a pass rises mid-scan)
                self._send_json({'success': False, 'error': 'Satellite pass in progress - scan again after it ends'}, code=409)
                return
            if (state.scans.get(dev) or {}).get('active'):
                self._send_json({'success': False, 'error': 'This dongle is already scanning'}, code=409)
                return
            scan.start_scan(dev, start_mhz, end_mhz, step_khz, ratio)
            self._send_json({'success': True, 'from_mhz': start_mhz, 'to_mhz': end_mhz, 'step_khz': step_khz}, code=200)
        elif self.path.split('?')[0] == '/record_dongle':
            # Manual recording (dashboard Record button on a dongle card):
            # /record_dongle?d=<id> starts this dongle's manual raw-IQ
            # capture (the Record button): the untouched uint8 IQ of the
            # current tune until stopped, capped at MANUAL_REC_MAX_SECS,
            # finalized into an analysis zip the dashboard downloads.
            # &stop=1 stops it. An automatic pass recording is
            # unaffected — the flag only adds.
            query = self._query()
            dev = (query.get('d') or [''])[0]
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            if entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') \
                    or entry.get('pocsag') or entry.get('adsb'):
                # A dedicated capture thread (AIS/Orbcomm/APRS/POCSAG/
                # ADS-B) does not record raw IQ
                self._send_json({'success': False, 'error': 'Dongle is in a dedicated role (AIS/Orbcomm/APRS/POCSAG)'}, code=400)
                return
            stop = (query.get('stop') or [''])[0].strip().lower() in ('1', 'true', 'on', 'yes')
            with state.status_lock:
                if stop:
                    state.manual_recording.pop(dev, None)
                else:
                    state.manual_recording[dev] = True
            if stop:
                state.log_console(f"⏹ Manual recording stop requested (dongle {dev})")
            else:
                state.log_console(f"⏺ Manual recording requested (dongle {dev}) — records the current tune until stopped")
            self._send_json({'success': True, 'manual_recording': not stop}, code=200)
        elif self.path.split('?')[0] == '/adsb_dongle':
            # Switch one dongle between satellite tracking and the
            # ADS-B role: /adsb_dongle?d=<id>&on=1 parks it on
            # 1090 MHz and decodes Mode S extended squitters;
            # on=0 returns to satellites.
            query = self._query()
            dev = (query.get('d') or [''])[0]
            on = (query.get('on') or [''])[0].strip().lower() in ('1', 'true', 'on', 'yes')
            ok = set_dongle_adsb(dev, on)
            self._send_json({'success': ok}, code=200 if ok else 404)
        elif self.path.split('?')[0] == '/pocsag_dongle':
            # Switch one dongle between satellite tracking and the
            # POCSAG paging role: /pocsag_dongle?d=<id>&on=1 parks it
            # on the paging band and decodes every paging channel
            # found in the 2.4 MHz capture; on=0 returns to satellites.
            query = self._query()
            dev = (query.get('d') or [''])[0]
            on = (query.get('on') or [''])[0].strip().lower() in ('1', 'true', 'on', 'yes')
            ok = set_dongle_pocsag(dev, on)
            self._send_json({'success': ok}, code=200 if ok else 404)
        elif self.path.split('?')[0] == '/fit_bw':
            # Fit the recorded (demod) bandwidth of one dongle to the signal
            # currently on its tune: measures the live spectrum and sets the
            # per-dongle demod low-pass cutoff (scan.fit_bandwidth).
            query = self._query()
            dev = (query.get('d') or [''])[0]
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({'success': False, 'error': 'Unknown dongle'}, code=404)
                return
            if entry.get('ais') or entry.get('orbcomm') or entry.get('aprs') \
                    or entry.get('pocsag') or entry.get('adsb'):
                self._send_json({'success': False, 'error': 'Dongle is dedicated to AIS/Orbcomm'}, code=400)
                return
            result = scan.fit_bandwidth(dev)
            self._send_json(result, code=200 if result.get('success') else 409)
        elif self.path.split('?')[0] == '/live.wav':
            # Endless WAV stream of the live FM-demodulated audio of one
            # dongle (default: the primary). WAV header with a maxed-out
            # size; browsers play it progressively.
            query = self._query()
            dev = (query.get('d') or [''])[0] or (state.primary_dongle or '')
            entry = state.sdrs.get(dev)
            if entry is None:
                self._send_json({"error": "Unknown dongle"}, code=404)
                return
            state.log_console(f"🔊 Live audio listener connected ({self.client_address[0]}, dongle {dev})")
            self.send_response(200)
            self.send_header('Content-type', 'audio/wav')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            data_size = 0xFFFFFFFF
            header = (b'RIFF' + struct.pack('<I', data_size) + b'WAVE'
                      + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, AUDIO_RATE, AUDIO_RATE * 2, 2, 16)
                      + b'data' + struct.pack('<I', data_size))
            ring = entry['la']
            try:
                self.wfile.write(header)
                with ring['cond']:
                    pos = ring['total']  # start at the live edge for low latency
                while True:
                    data = None
                    with ring['cond']:
                        if pos >= ring['total']:
                            ring['cond'].wait(timeout=5)
                        if pos >= ring['total']:
                            # No new audio (SDR idle/restarting): send 100 ms of
                            # silence to keep the connection alive and notice a
                            # disconnected client on write
                            data = b'\x00\x00' * (AUDIO_RATE // 10)
                        else:
                            if pos < ring['base']:
                                pos = ring['base']  # fell behind, skip dropped chunks
                            data = b''.join(ring['data'][pos - ring['base']:])
                            pos = ring['total']
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # listener disconnected
            state.log_console(f"🔊 Live audio listener disconnected ({self.client_address[0]}, dongle {dev})")
            return
        elif self.path.startswith('/decode/'):
            filename = self.path[8:].split('?')[0]
            # ?force=1 re-runs a decode whose attempt marker says it failed
            force = (self._query().get('force') or [''])[0] == '1'
            if '..' in filename or '/' in filename:
                state.log_console(f"Decode rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = recording_path(filename)
            if not os.path.exists(wav_path):
                state.log_console(f"Decode requested missing recording: {filename}", "warn")
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Recording not found')
                return
            decoded, png_path, err = decode_recording(wav_path, force=force)
            # Reception quality: only the stored pass-end value remains
            # (the APT sync-quality scorer was retired with APT)
            quality = quality_map().get(filename)
            if quality is not None:
                set_recording_quality(filename, quality)
            # The pass waterfall (<base>.waterfall.png) when the pass left
            # one — the page shows it next to the result without a refresh
            wf_name = filename.replace('.wav', '.waterfall.png')
            waterfall = wf_name if os.path.exists(recording_path(wf_name)) \
                else None
            if decoded:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "png": filename.replace('.wav', '.png'),
                    "quality": quality,
                    "waterfall": waterfall,
                }).encode())
            else:
                state.log_console(f"Decode failed for {filename}: {err}", "error")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": err,
                    "quality": quality,
                    "waterfall": waterfall,
                }).encode())
        elif self.path.startswith('/delete/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename or not filename.endswith('.wav'):
                state.log_console(f"Delete rejected invalid filename: {filename!r}", "warn")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            wav_path = recording_path(filename)
            if not os.path.exists(wav_path):
                state.log_console(f"Delete requested missing recording: {filename}", "warn")
                self._send_json({"success": False, "error": "Recording not found"}, code=404)
                return
            # Never delete the file that is currently being written
            with state.status_lock:
                recording_now = state.is_recording and state.current_wav_path == wav_path
            if recording_now:
                state.log_console(f"Delete refused, recording in progress: {filename}", "warn")
                self._send_json({"success": False, "error": "Recording in progress"}, code=409)
                return
            deleted = []
            try:
                import glob
                import shutil
                # A family can straddle the LOCAL capture dir and the
                # archive mid-move (see archive.py) - sweep both roots
                for root in {RECORD_LOCAL_DIR, RECORD_DIR}:
                    base = os.path.join(root, filename[:-len('.wav')])
                    wav_p = base + '.wav'
                    if os.path.exists(wav_p):
                        os.remove(wav_p)
                        deleted.append(filename)
                    png_path = base + '.png'
                    if os.path.exists(png_path):
                        os.remove(png_path)
                        deleted.append(os.path.basename(png_path))
                    thumb = png_path[:-4] + THUMB_SUFFIX
                    if os.path.exists(thumb):
                        os.remove(thumb)
                        deleted.append(os.path.basename(thumb))
                    # The pass waterfall diagram (and its thumbnail)
                    wf_png = base + '.waterfall.png'
                    if os.path.exists(wf_png):
                        os.remove(wf_png)
                        deleted.append(os.path.basename(wf_png))
                        wf_thumb = wf_png[:-4] + THUMB_SUFFIX
                        if os.path.exists(wf_thumb):
                            os.remove(wf_thumb)
                            deleted.append(os.path.basename(wf_thumb))
                    # Raw IQ capture and the SatDump product directories
                    # belong to the recording too - deleting takes the full set
                    iq_path = base + '.iq.u8'
                    if os.path.exists(iq_path):
                        os.remove(iq_path)
                        deleted.append(os.path.basename(iq_path))
                    for d in glob.glob(base + '_*'):
                        if os.path.isdir(d):
                            shutil.rmtree(d)
                            deleted.append(os.path.basename(d) + '/')
                # The decode-attempt marker is a decodes-table row
                forget_decode_marker(wav_path)
                deleted = list(dict.fromkeys(deleted))
                state.log_console(f"🗑 Deleted: {', '.join(deleted)}")
                self._send_json({"success": True, "deleted": deleted}, code=200)
            except Exception as e:
                state.log_console(f"Delete error for {filename}: {e}", "error")
                self._send_json({"success": False, "error": str(e)}, code=500)
        elif self.path.startswith('/satnogs.json'):
            # SatNOGS DB metadata for the tracked satellite (proxied and
            # cached server-side — the DB has no CORS headers and the
            # dashboard would otherwise hammer it on every poll)
            query = self._query()
            try:
                catnr = int((query.get('catnr') or ['0'])[0])
            except ValueError:
                catnr = 0
            info, err = (satellite_info(catnr) if catnr else (None, 'missing catnr'))
            self.send_response(200 if info else 404)
            self.send_header('Content-type', 'application/json')
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            self.wfile.write(json.dumps({"info": info, "error": err}).encode())
        elif self.path.startswith('/images/'):
            filename = self.path[8:]
            if '..' in filename or '/' in filename:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            img_path = recording_path(filename)
            if os.path.exists(img_path):
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.end_headers()
                with open(img_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
        elif self.path.startswith('/thumbs/'):
            # Small JPEG preview of a decoded image, generated on first
            # request and cached next to the PNG (see thumbs.py). The pass
            # history page loads these instead of the ~10 MB originals.
            # One flat segment serves the composite PNG next to a WAV;
            # 2-3 segments serve decode products nested in their SatDump
            # directory (<base>_lrpt/MSU-MR/*.png - the history page's
            # slideshow previews).
            from urllib.parse import unquote
            parts = unquote(self.path[8:]).split('?', 1)[0].split('/')
            if not 1 <= len(parts) <= 3 or not all(parts) \
                    or any('..' in p for p in parts):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid filename')
                return
            if not parts[-1].lower().endswith('.png'):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
                return
            img_path = recording_path(*parts)
            if not os.path.exists(img_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Image not found')
                return
            tpath, err = ensure_thumb(img_path)
            if tpath is None:
                # Pillow missing or PNG unreadable — keep the page working
                # by falling back to the full image
                state.log_console(f"Thumbnail fallback for {parts[-1]}: {err}", "warn")
                self.send_response(302)
                fallback = ('/products/' + '/'.join(parts)) if len(parts) > 1 \
                    else ('/images/' + parts[0])
                self.send_header('Location', fallback)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-type', 'image/jpeg')
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            with open(tpath, 'rb') as f:
                self.wfile.write(f.read())
        elif self.path.startswith('/products/'):
            # Decode products live in subdirectories next to their
            # recording (e.g. <base>_lrpt/; NOAA-era decodes left _apt/_dsb
            # directories, still served). SatDump v1.2.0 nests instrument
            # images one level deeper (<base>_lrpt/MSU-MR/*.png), so one
            # or two file levels are served, sanitized: no empty segments,
            # no traversal.
            from urllib.parse import unquote
            parts = unquote(self.path[10:]).split('?', 1)[0].split('/')
            if not 2 <= len(parts) <= 3 or not all(parts) or any('..' in p for p in parts):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid product path')
                return
            ppath = recording_path(*parts)
            if not os.path.isfile(ppath):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Product not found')
                return
            # The FILE is the last segment (nested products have the
            # instrument subdir in between) - the content type must
            # follow it, or image/png files serve as octet-stream and
            # browsers download instead of displaying them.
            fname = parts[-1]
            ctype = ('image/png' if fname.lower().endswith('.png')
                     else 'application/json' if fname.lower().endswith('.json')
                     else 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-type', ctype)
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            with open(ppath, 'rb') as f:
                self.wfile.write(f.read())
        elif self.path.split('?')[0] == '/backup':
            # Full station backup: consistent SQLite snapshot plus the
            # whole recordings tree, as a downloadable zip. Stored, not
            # deflated - WAV/IQ/PNG payloads do not compress.
            import shutil
            dest = os.path.join(LOGDIR, 'station_backup_%s.zip'
                                % datetime.now().strftime('%Y%m%d_%H%M%S'))
            try:
                _, total = db.build_backup(dest, None)
            except Exception as e:
                state.log_console(f'Backup failed: {e}', 'error')
                self._send_json({'error': str(e)}, code=500)
                return
            state.log_console(f'\U0001f4be Station backup built: {os.path.basename(dest)}'
                              f' ({total / 1e6:.1f} MB)')
            self.send_response(200)
            self.send_header('Content-type', 'application/zip')
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % os.path.basename(dest))
            self.send_header('Content-Length', str(os.path.getsize(dest)))
            self.end_headers()
            with open(dest, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
            try:
                os.remove(dest)
            except OSError:
                pass
        else:
            super().do_GET()
    def do_POST(self):
        """Upload a station backup archive: /restore with the raw zip
        as the request body (the dashboard posts the chosen file)."""
        endpoint = self.path.split('?')[0]
        query = self._query()
        if endpoint == '/tuner_start':
            payload, code = tuner.start_session(
                (query.get('d') or [''])[0],
                (query.get('service') or [''])[0],
                (query.get('antenna') or [''])[0],
                (query.get('wave_factor') or ['0.25'])[0])
            self._send_json(payload, code=code)
            return
        if endpoint == '/tuner_point':
            payload, code = tuner.report_length(
                (query.get('d') or [''])[0],
                (query.get('length_cm') or [''])[0],
                (query.get('note') or [''])[0])
            self._send_json(payload, code=code)
            return
        if endpoint == '/tuner_measure':
            payload, code = tuner.measure_again((query.get('d') or [''])[0])
            self._send_json(payload, code=code)
            return
        if endpoint == '/tuner_stop':
            payload, code = tuner.stop_session((query.get('d') or [''])[0])
            self._send_json(payload, code=code)
            return
        if endpoint != '/restore':
            self.send_response(404)
            self.end_headers()
            return
        with state.status_lock:
            active = state.is_pass_active or state.is_recording
        if active:
            self.send_response(409)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(
                {'error': 'Recording in progress - restore after the pass'}).encode())
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Empty upload')
            return
        tmp = os.path.join(LOGDIR, 'restore_upload.zip')
        try:
            with open(tmp, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            summary = db.restore_backup(tmp, None)
        except ValueError as e:
            self._send_json({'error': str(e)}, code=400)
            return
        except Exception as e:
            state.log_console(f'Restore failed: {e}', 'error')
            self._send_json({'error': str(e)}, code=500)
            return
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        state.log_console(f'\U0001f4be Station restored from backup: '
                          f'{summary["passes"]} passes, {summary["ships"]} ships, '
                          f'{summary["recording_files"]} recording files')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            'success': True, 'summary': summary,
            'note': 'Restart the receiver to reload in-memory state '
                    '(AIS ship table, dongle registry)'}).encode())
    def send_header(self, keyword, value):
        # The dashboard and its sub-pages are single evolving HTML files
        # served with Last-Modified but no Cache-Control — browsers then
        # heuristic-cache them and miss updates on normal refreshes (seen
        # live: a deployed dashboard change stayed invisible until a hard
        # refresh). no-cache keeps revalidation cheap (304 via If-Modified-
        # Since) while the page always follows the deployed file.
        if keyword.lower() == 'content-type' and 'text/html' in str(value):
            super().send_header('Cache-Control', 'no-cache')
        super().send_header(keyword, value)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEBDIR, **kwargs)
    def log_message(self, *args): pass
