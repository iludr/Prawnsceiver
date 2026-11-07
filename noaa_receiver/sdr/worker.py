"""Per-dongle worker process.

Run as: python -m noaa_receiver.sdr.worker <did> <ctrl-sock-path>

One worker owns ONE dongle's whole capture pipeline (rtl_tcp/rtl_sdr read,
waterfall FFT, demod, live audio, decode, recording). The master process
keeps the web API, scheduler and DB; it spawns one worker per dongle and
mirrors their reports back into state.sdrs, so the dashboard is unchanged.

Control channel: a Unix socket, frames are [u32be len][u8 type][payload]:
  master -> worker, type 1: JSON command
      {"cmd":"cfg", ...}      role/pause/gain/sat_mode/manual overrides
      {"cmd":"sched", ...}    scheduler state (mode_freq/mode_pass/...)
      {"cmd":"shutdown"}
  worker -> master:
      type 2: JSON status     {"t":"status", ...entry fields...}
      type 3: binary wf row   u32be seq + row bytes
      type 4: JSON decode     {"t":"decode","mod":"adsb","d":{...}}
      type 5: JSON log        {"t":"log","msg":...,"level":...}
      type 6: binary audio    PCM bytes
"""
import json
import os
import socket
import sys
import threading
import time


def _send_framed(sock, ftype, payload):
    try:
        sock.sendall(len(payload).to_bytes(4, 'big') + bytes([ftype]) + payload)
    except OSError:
        pass


def _send_json(sock, obj):
    # worker->master JSON events all ride type 2 (type 1 is master->worker
    # commands only)
    _send_framed(sock, 2, json.dumps(obj).encode())


def main():
    from .. import state
    from ..config import DONGLE_WORKER_PROCESSES  # noqa: F401 (guard)
    from . import radio

    did = sys.argv[1]
    sock_path = sys.argv[2]

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):
        try:
            sock.connect(sock_path)
            break
        except OSError:
            time.sleep(0.1)
    else:
        os._exit(1)
    _send_json(sock, {"t": "hello", "did": did})

    # ---- build this worker's local dongle entry from the persisted row ----
    row = None
    from .. import db
    try:
        rows = db.query("SELECT * FROM dongles WHERE host=? AND port=?",
                        (did.rsplit(':', 1)[0], int(did.rsplit(':', 1)[1])))
        row = rows[0] if rows else None
    except Exception:
        row = None
    host, port = did.rsplit(':', 1)
    entry = radio._new_entry(did, host, int(port), primary=False)
    if row:
        for flag in ('ais', 'orbcomm', 'aprs', 'aprs_iss', 'aprs_lasar',
                     'pocsag', 'adsb', 'ism', 'dab', 'auto_sat', 'gain_auto'):
            entry[flag] = bool(row.get(flag))
        if row.get('sat_mode') in state.SAT_MODES:
            entry['sat_mode'] = row['sat_mode']
        if row.get('dab_channel'):
            entry['dab_channel'] = row['dab_channel']
    state.sdrs[did] = entry

    # ---- forwarding hooks: ship everything the master needs ----
    radio._wf_row_hook = lambda seq, row: _send_framed(
        sock, 3, seq.to_bytes(4, 'big') + row)
    state.log_hook = lambda msg, level: _send_json(
        sock, {"t": "log", "msg": msg, "level": level})
    state.audio_hook = lambda pcm: _send_framed(sock, 6, pcm)

    # Decode results go to the MASTER (single DB writer + live registries
    # the web API serves): patch the module-level handlers to forward.
    from ..decoding import adsb, ais, aprs, orbcomm, pocsag, ism

    def _fwd(mod_name, fn):
        def wrapper(*args, **kwargs):
            # keep the worker-local demod counters alive (the real handler
            # is not called here, it runs in the master)
            for a in args:
                if isinstance(a, dict) and 'frames' in a:
                    a['frames'] = a.get('frames', 0) + 1
                    a['last_frame'] = time.time()
            try:
                _send_json(sock, {"t": "decode", "mod": mod_name,
                                  "args": [json.loads(json.dumps(a, default=str))
                                           for a in args]})
            except Exception:
                pass
        return wrapper

    adsb.handle_message = _fwd('adsb', adsb.handle_message)
    ais.handle_frames = _fwd('ais', ais.handle_frames)
    aprs.handle_frames = _fwd('aprs', aprs.handle_frames)
    orbcomm.handle_packets = _fwd('orbcomm', orbcomm.handle_packets)
    pocsag._log_page = _fwd('pocsag', pocsag._log_page)
    ism._register = _fwd('ism', ism._register)

    # ---- control loop: apply master commands to local state ----
    def control_loop():
        buf = b''
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                os._exit(0)
            if not chunk:
                os._exit(0)
            buf += chunk
            while len(buf) >= 5:
                ln = int.from_bytes(buf[:4], 'big')
                if len(buf) < 5 + ln:
                    break
                ftype = buf[4]
                payload = buf[5:5 + ln]
                buf = buf[5 + ln:]
                if ftype != 1:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                cmd = msg.get('cmd')
                if cmd == 'shutdown':
                    os._exit(0)
                elif cmd == 'cfg':
                    was_paused = bool(entry.get('paused'))
                    with state.status_lock:
                        for flag in ('ais', 'orbcomm', 'aprs', 'aprs_iss',
                                     'aprs_lasar', 'pocsag', 'adsb', 'ism',
                                     'dab', 'auto_sat', 'paused', 'gain_auto'):
                            if flag in msg:
                                entry[flag] = bool(msg[flag])
                        if 'sat_mode' in msg and msg['sat_mode'] in state.SAT_MODES:
                            entry['sat_mode'] = msg['sat_mode']
                        if 'dab_channel' in msg:
                            entry['dab_channel'] = msg['dab_channel']
                        if 'manual_freq_hz' in msg:
                            if msg['manual_freq_hz']:
                                state.manual_dongle_freq[did] = int(msg['manual_freq_hz'])
                            else:
                                state.manual_dongle_freq.pop(did, None)
                        if 'manual_bw_hz' in msg:
                            if msg['manual_bw_hz']:
                                state.manual_dongle_bw[did] = int(msg['manual_bw_hz'])
                            else:
                                state.manual_dongle_bw.pop(did, None)
                        if 'manual_mode' in msg:
                            if msg['manual_mode']:
                                state.manual_dongle_mode[did] = msg['manual_mode']
                            else:
                                state.manual_dongle_mode.pop(did, None)
                    if msg.get('paused') and not was_paused:
                        # Pause landed while a capture thread is streaming:
                        # the master cannot reach this process's rtl_tcp
                        # socket — close it here so the blocked read ends
                        # and the thread's paused check stops the run.
                        # (_workers is empty in this process, so
                        # radio._stop_capture takes the local close path.)
                        radio._stop_capture(entry)
                elif cmd == 'sched':
                    with state.status_lock:
                        for m, v in msg.get('mode_freq', {}).items():
                            state.mode_freq[m] = v
                        for m, v in msg.get('mode_sat_name', {}).items():
                            state.mode_sat_name[m] = v
                        for m, v in msg.get('mode_doppler_hz', {}).items():
                            state.mode_doppler_hz[m] = v
                        for m, v in msg.get('mode_doppler_freq_hz', {}).items():
                            state.mode_doppler_freq_hz[m] = v
                        if 'recordings_paused' in msg:
                            state.recordings_paused = bool(msg['recordings_paused'])
                        for m, p in msg.get('mode_pass', {}).items():
                            state.mode_pass[m] = {"sat_name": p} if p else None
                        man = msg.get('manual', {}).get(did) or {}
                        if man.get('freq'):
                            state.manual_dongle_freq[did] = int(man['freq'])
                        else:
                            state.manual_dongle_freq.pop(did, None)
                        if man.get('bw'):
                            state.manual_dongle_bw[did] = int(man['bw'])
                        else:
                            state.manual_dongle_bw.pop(did, None)
                        if man.get('mode'):
                            state.manual_dongle_mode[did] = man['mode']
                        else:
                            state.manual_dongle_mode.pop(did, None)

    threading.Thread(target=control_loop, daemon=True).start()

    # ---- status reporter: mirror entry fields + decode-module state ----
    def status_loop():
        while True:
            time.sleep(1.0)
            try:
                with entry['lock']:
                    st = {
                        "t": "status",
                        "connected": bool(entry.get('connected')),
                        "tuner": entry.get('tuner'),
                        "signal": entry.get('signal', 0.0),
                        "last_data": entry.get('last_data', 0.0),
                        "correction": entry.get('correction', 0),
                        "correction_src": entry.get('correction_src'),
                        "is_recording": bool(entry.get('is_recording')),
                        "wav_path": entry.get('wav_path'),
                        "manual_rec_start": entry.get('manual_rec_start'),
                        "manual_zip": entry.get('manual_zip'),
                    }
                extra = {}
                if entry.get('adsb'):
                    extra['adsb_stats'] = dict(state.adsb_stats or {})
                if entry.get('pocsag'):
                    with state.pocsag_lock:
                        extra['pocsag_channels'] = list(state.pocsag_channels.values())
                if entry.get('ism'):
                    with state.ism_lock:
                        extra['ism_sensors'] = list(state.ism_sensors.values())
                if extra:
                    st.update(extra)
                _send_json(sock, st)
            except Exception:
                pass

    threading.Thread(target=status_loop, daemon=True).start()

    # ---- single-dongle supervisor: same role -> capture-thread mapping ----
    t = None
    while True:
        time.sleep(1.0)
        if entry.get('closed'):
            return
        if t is not None and t.is_alive():
            continue
        if entry.get('paused'):
            t = None
            continue
        if entry.get('ais'):
            from ..decoding.ais import ais_capture_thread
            t = threading.Thread(target=ais_capture_thread, args=(did,), daemon=True)
        elif entry.get('orbcomm'):
            from ..decoding.orbcomm import orbcomm_capture_thread
            t = threading.Thread(target=orbcomm_capture_thread, args=(did,), daemon=True)
        elif entry.get('aprs'):
            from ..decoding.aprs import aprs_capture_thread
            t = threading.Thread(target=aprs_capture_thread, args=(did,), daemon=True)
        elif entry.get('pocsag'):
            from ..decoding.pocsag import pocsag_capture_thread
            t = threading.Thread(target=pocsag_capture_thread, args=(did,), daemon=True)
        elif entry.get('adsb'):
            from ..decoding import adsb_readsb
            if adsb_readsb.is_readsb_dongle(did):
                # readsb owns this dongle's rtl_tcp feed — leave the slot
                # free; the master's readsb consumer decodes its output
                t = None
            else:
                from ..decoding.adsb import adsb_capture_thread
                t = threading.Thread(target=adsb_capture_thread, args=(did,), daemon=True)
        elif entry.get('dab'):
            from ..decoding.dab import dab_capture_thread
            t = threading.Thread(target=dab_capture_thread, args=(did,), daemon=True)
        elif entry.get('ism'):
            from ..decoding.ism import ism_capture_thread
            t = threading.Thread(target=ism_capture_thread, args=(did,), daemon=True)
        else:
            t = threading.Thread(target=radio.sdr_capture_thread, args=(did,), daemon=True)
        if t is not None:
            t.start()


if __name__ == '__main__':
    main()
