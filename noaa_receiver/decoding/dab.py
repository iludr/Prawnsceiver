"""DAB+ broadcast radio — a dongle role like AIS/ISM.

A dongle in the DAB+ role runs welle-cli (welle.io's command line
receiver) in web mode against the dongle's rtl_tcp stream: welle-cli
demodulates the DAB+ ensemble, decodes every service's audio (MP3
re-encode) and the MOT slideshow, and serves them on a small HTTP
interface (localhost:9090) that this module polls and proxies to the
dashboard.

welle-cli web endpoints used here:
- /mux.json      ensemble + services + DLS + MOT flags (polled)
- /mp3/<sid>     live MP3 audio of one service (proxied /dab_audio/)
- /slide/<sid>   MOT slideshow image (proxied /dab_slide)

Verified live on the station's own hardware (2026-10-27): channel 5C =
178.352 MHz, ensemble "DR Deutschland", 15 services, 1.1 MB of MSC
decoded in 100 s.
"""
import json
import shutil
import subprocess
import threading
import time
import urllib.request

from .. import state

DAB_WEB_PORT = 9090
DAB_CHANNEL_DEFAULT = '5C'

# Band III (VHF) channel table: channel -> center frequency Hz (CCIR)
DAB_CHANNELS = {
    '5A': 174928000, '5B': 176640000, '5C': 178352000,
    '6A': 181936000, '6B': 183648000, '6C': 185360000,
    '7A': 188928000, '7B': 190640000, '7C': 192352000, '7D': 194064000,
    '8A': 195936000, '8B': 197648000, '8C': 199360000, '8D': 201072000,
    '9A': 202992000, '9B': 204640000, '9C': 206352000, '9D': 208064000,
    '10A': 209936000, '10B': 211648000, '10C': 213360000, '10D': 215072000,
    '11A': 216928000, '11B': 218640000, '11C': 220352000, '11D': 222064000,
    '12A': 223936000, '12B': 225648000, '12C': 227360000, '12D': 229072000,
    '13A': 230494000, '13B': 231494000, '13C': 232494000, '13D': 233494000,
    '13E': 234494000, '13F': 235494000,
}

dab_lock = threading.Lock()

# Decoded state (module-level, like ism.py's state.ism_*): the last
# /mux.json from welle-cli and the MOT slideshow cache
_data = {}       # last /mux.json from welle-cli
_slide = {}      # sid -> (bytes, content_type)


def _fetch_json(port, path, timeout=3):
    """GET a JSON document from the local welle-cli web server; None on
    any failure (the poller treats that as 'no new data')."""
    try:
        with urllib.request.urlopen(
                f'http://127.0.0.1:{port}{path}', timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return None


def dab_capture_thread(did):
    """DAB+ decoding for a dongle switched to the DAB+ role: runs
    welle-cli (welle.io's command line receiver) in web mode against the
    dongle's rtl_tcp stream and mirrors its status into the module state
    the dashboard polls. Mirrors the ISM capture thread's lifecycle
    (restart with backoff, role/pause/closed handover); the IQ stream is
    decoded by welle-cli, not here — this thread only manages the
    welle-cli process and relays its status."""
    entry = state.sdrs[did]
    with state.dab_lock:
        state.dab_enabled = True
        state.dab_dongle = did
        state.dab_channel = entry.get('dab_channel') or DAB_CHANNEL_DEFAULT
    channel = entry.get('dab_channel') or DAB_CHANNEL_DEFAULT
    state.log_console(f"📻 DAB+ receiver on dongle {did}: welle-cli "
                      f"decoding channel {channel} "
                      f"(web UI 127.0.0.1:{DAB_WEB_PORT})")
    restart_backoff = 5.0
    while True:
        if entry.get("closed") or not entry.get("dab") or entry.get("paused"):
            _reception_off()
            return
        run_started = time.time()
        proc = None
        try:
            exe = shutil.which("welle-cli")
            if not exe:
                raise RuntimeError("welle-cli not installed (welle.io package)")
            channel = entry.get("dab_channel") or DAB_CHANNEL_DEFAULT
            cmd = [exe, "-c", channel, "-w", str(DAB_WEB_PORT),
                   "-F", "rtl_tcp,%s:%d" % (entry["host"], entry["port"]),
                   "-g", "-1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT)
            with state.status_lock:
                entry["connected"] = True
            state.log_console(f"📻 DAB+ receiver on dongle {did}: welle-cli "
                              f"decoding channel {channel} "
                              f"(web UI 127.0.0.1:{DAB_WEB_PORT})")
            while True:
                if entry.get("closed") or not entry.get("dab") \
                        or entry.get("paused"):
                    break
                # a channel change (set_dab_channel) restarts welle-cli
                if (entry.get('dab_channel') or DAB_CHANNEL_DEFAULT) != channel:
                    break
                data = _fetch_json(DAB_WEB_PORT, "/mux.json")
                if data is not None:
                    with state.dab_lock:
                        _data.clear()
                        _data.update(data)
                    entry["last_data"] = time.time()
                    try:
                        snr = float(data.get("demodulator", {}).get("snr", 0.0))
                        entry["signal"] = max(0.0, snr) * 100.0
                    except (TypeError, ValueError):
                        pass
                if proc.poll() is not None:
                    raise RuntimeError("welle-cli exited (rc %s)"
                                       % proc.returncode)
                time.sleep(2.0)
        except Exception as e:
            if not entry.get("closed") and entry.get("dab") \
                    and not entry.get("paused"):
                state.log_console(f"DAB thread error (dongle {did}): {e}",
                                  "error")
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            with state.status_lock:
                entry["connected"] = False
        if entry.get("closed") or not entry.get("dab") or entry.get("paused"):
            _reception_off()
            return
        if time.time() - run_started >= 30:
            restart_backoff = 5.0
        else:
            restart_backoff = min(restart_backoff * 2, 15.0)
        time.sleep(restart_backoff)


def _reception_off():
    """Clear the module state when the DAB+ role ends (the ISM
    reception_off pattern — one DAB+ receiver at a time, the role is
    exclusive anyway)."""
    with state.dab_lock:
        state.dab_enabled = False
        state.dab_dongle = None


def dab_status():
    """Snapshot for /dab.json and the WS 'dab' topic."""
    with state.dab_lock:
        enabled = state.dab_enabled
        dongle = state.dab_dongle
        channel = state.dab_channel
        data = dict(_data)
    demod = data.get("demodulator", {})
    ensemble = data.get("ensemble", {})
    label = ensemble.get("label", {})
    if not isinstance(label, dict):
        label = {"label": label}
    services = []
    for s in data.get("services", []):
        # welle-cli's label fields are dicts ({label, shortlabel,
        # fig2charset, ...}) — flatten them; a plain string is accepted too
        def _lbl(v):
            if isinstance(v, dict):
                v = v.get("label") or ""
            return str(v or "").strip()
        services.append({
            "sid": s.get("sid"),
            "label": _lbl(s.get("label")),
            "shortlabel": _lbl(s.get("shortlabel")),
            "dls": (s.get("dls") or {}).get("label", "") if isinstance(s.get("dls"), dict) else str(s.get("dls") or ""),
            "mot": bool((s.get("mot") or {}).get("time")),
            "mot_time": (s.get("mot") or {}).get("time"),
        })
    return {
        "enabled": bool(enabled),
        "dongle": dongle,
        "channel": channel,
        "snr": demod.get("snr", 0.0),
        "freq_corr": demod.get("frequencycorrection", 0.0),
        "ensemble": {
            "id": ensemble.get("id"),
            "label": (label.get("label") or "").strip(),
            "shortlabel": (label.get("shortlabel") or "").strip(),
            "ecc": ensemble.get("ecc"),
        },
        "services": services,
    }


def set_dab_channel(did, channel):
    """Retune the DAB+ receiver to another ensemble channel: stored in
    the entry (and persisted) and in state.dab_channel, the capture
    thread notices the change and restarts welle-cli with the new -c on
    its next poll."""
    channel = (channel or '').strip().upper()
    if channel not in DAB_CHANNELS:
        return False
    entry = state.sdrs.get(did)
    if entry is None or not entry.get('dab'):
        return False
    with state.status_lock:
        entry['dab_channel'] = channel
    with state.dab_lock:
        state.dab_channel = channel
    state.log_console(f"📻 DAB+ tuning to channel {channel} ...")
    from ..sdr.radio import _persist_dongles
    _persist_dongles()
    return True


def dab_slide(sid):
    """MOT slideshow image for one service id, fetched from welle-cli on
    demand and cached (bytes, content_type) — None when there is none."""
    now = time.time()
    cached = _slide.get(sid)
    if cached and now - cached[2] < 10.0:
        return cached[0], cached[1]
    try:
        with urllib.request.urlopen(
                f'http://127.0.0.1:{DAB_WEB_PORT}/slide/{sid}',
                timeout=5) as r:
            data = r.read()
            ctype = r.headers.get('Content-type', 'image/png')
    except Exception:
        return (cached[0], cached[1]) if cached else None
    with state.dab_lock:
        _slide[sid] = (data, ctype, now)
    return data, ctype
