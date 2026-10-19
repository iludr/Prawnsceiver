"""Prawntuner: receive-antenna tuning sessions on registered dongles."""
import json
import math
import threading
import time

import numpy as np

from . import db, state
from .calibration import SDR_DONGLE_GAIN
from .config import (ADSB_CENTER_HZ, AIS_CENTER_HZ, APRS_FREQ_HZ,
                     ORBCOMM_CENTER_HZ, PAGING_CENTER_HZ, SDR_GAIN,
                     SDR_OFFSET_HZ, SDR_RATE, FFT_SIZE)
from .sdr import radio
from .sdr.scan import SCAN_SAMPLE_EVERY, SCAN_SETTLE_SECS, SPIKE_GUARD_HZ


INACTIVITY_SECS = 30 * 60
_ROLES = ("ais", "orbcomm", "aprs", "pocsag", "adsb")
_ROLE_SETTERS = {
    "ais": radio.set_dongle_ais,
    "orbcomm": radio.set_dongle_orbcomm,
    "aprs": radio.set_dongle_aprs,
    "pocsag": radio.set_dongle_pocsag,
    "adsb": radio.set_dongle_adsb,
}
_LOG_TABLES = {
    "ais": "ais_messages",
    "orbcomm": "orbcomm_messages",
    "aprs": "aprs_messages",
    "pocsag": "pocsag_messages",
    "adsb": "adsb_messages",
}
SERVICES = {
    "ais": {"label": "AIS", "frequency_hz": AIS_CENTER_HZ,
            "half_band_hz": 100_000, "dwell_secs": 30},
    "orbcomm": {"label": "Orbcomm", "frequency_hz": ORBCOMM_CENTER_HZ,
                "half_band_hz": 500_000, "dwell_secs": 30},
    "aprs": {"label": "APRS", "frequency_hz": APRS_FREQ_HZ,
             "half_band_hz": 30_000, "dwell_secs": 30},
    "pocsag": {"label": "POCSAG", "frequency_hz": PAGING_CENTER_HZ,
               "half_band_hz": 600_000, "dwell_secs": 30},
    "adsb": {"label": "ADS-B", "frequency_hz": ADSB_CENTER_HZ,
             "half_band_hz": 1_100_000, "dwell_secs": 10},
    "satellite": {"label": "Satellites (LRPT)",
                  "frequency_hz": 137_900_000,
                  "half_band_hz": 200_000, "dwell_secs": 30},
}

_lock = threading.RLock()
_sessions = {}
_latest = {}
_watchdog = None


def antenna_length_cm(frequency_hz, antenna, wave_factor=0.25):
    """Element/leg length in cm = wave_factor * wavelength * 0.95 VC.
    Wavelength_cm = 30000 / f_MHz (speed of light = 3e10 cm/s).
    For whip (monopole): use wave_factor=0.25 (quarter-wave monopole).
    For V-dipole legs: wave_factor=0.25 (half-wave dipole - each leg is lambda/4),
    wave_factor=0.5 (full-wave total length - each leg lambda/2),
    wave_factor=1.0 (each leg lambda).
    VC 0.95 implicit: lambda_cm = 28500 / f_MHz.
    """
    if antenna not in ("whip", "vdipole"):
        raise ValueError("Antenna must be whip or vdipole")
    if float(frequency_hz) <= 0:
        raise ValueError("Frequency must be positive")
    if wave_factor not in (0.25, 0.5, 1.0):
        raise ValueError("wave_factor must be 0.25, 0.5, or 1.0")
    f_mhz = float(frequency_hz) / 1e6
    return round(28500.0 * wave_factor / f_mhz, 2)


def initial_lengths(frequency_hz, antenna, wave_factor=0.25):
    """Return nominal and 5%-long starting element/leg lengths in cm.
    For whip: wave_factor must be 0.25.
    For V-dipole: recommended wave_factor 0.25, 0.5 or 1.0.
    """
    nominal = antenna_length_cm(frequency_hz, antenna, wave_factor)
    return {"nominal_cm": nominal,
            "start_cm": round(nominal * 1.05, 2)}


def _metric_better(a, b):
    if a["ratio_db"] > b["ratio_db"] + 0.25:
        return True
    if b["ratio_db"] > a["ratio_db"] + 0.25:
        return False
    if a.get("decodes") is not None and b.get("decodes") is not None:
        return a["decodes"] > b["decodes"]
    return False


def _metric_worse(a, b):
    return _metric_better(b, a)


def _quality_groups(points):
    groups = {}
    for point in points:
        if not point.get("valid") or point.get("ratio_db") is None:
            continue
        length = round(float(point["length_cm"]), 3)
        group = groups.setdefault(length, {"length_cm": length, "ratios": [],
                                           "decodes": []})
        group["ratios"].append(float(point["ratio_db"]))
        if point.get("decodes") is not None:
            group["decodes"].append(float(point["decodes"]))
    out = {}
    for length, group in groups.items():
        out[length] = {
            "length_cm": length,
            "ratio_db": sum(group["ratios"]) / len(group["ratios"]),
            "decodes": (sum(group["decodes"]) / len(group["decodes"])
                        if group["decodes"] else None),
        }
    return out


def hill_climb(points, step_cm=2.0, direction=1):
    """Choose the next length instruction from completed measurement points."""
    groups = _quality_groups(points)
    valid = [p for p in points if p.get("valid") and p.get("ratio_db") is not None]
    if not valid:
        return {"status": "awaiting-user", "step_cm": step_cm,
                "direction": direction,
                "instruction": "🦀 Hold; wait for a valid measurement."}

    current = valid[-1]
    current_len = round(float(current["length_cm"]), 3)
    earlier = next((p for p in reversed(valid[:-1])
                    if round(float(p["length_cm"]), 3) != current_len), None)
    if earlier is None and len(valid) > 1:
        return {"status": "awaiting-user", "step_cm": step_cm,
                "direction": direction,
                "instruction": "🦀 Hold; measure again at the same length."}

    best = max(groups.values(), key=lambda p: (p["ratio_db"],
                p["decodes"] if p["decodes"] is not None else -1))
    left = groups.get(round(best["length_cm"] - 0.5, 3))
    right = groups.get(round(best["length_cm"] + 0.5, 3))
    if left and right and _metric_worse(left, best) and _metric_worse(right, best):
        return {"status": "converged", "step_cm": 0.5,
                "direction": direction,
                "converged_length_cm": best["length_cm"],
                "instruction": ("🦀 Tuned length found: %.2f cm" %
                                best["length_cm"])}

    if earlier is None:
        next_direction, next_step = 1, 2.0
    else:
        previous_len = round(float(earlier["length_cm"]), 3)
        previous = groups[previous_len]
        delta = current_len - previous_len
        if abs(delta) < 0.001:
            return {"status": "awaiting-user", "step_cm": step_cm,
                    "direction": direction,
                    "instruction": "🦀 Hold; measure again at the same length."}
        if _metric_better(groups[current_len], previous):
            next_direction = 1 if delta > 0 else -1
            next_step = min(2.0, max(0.5, abs(delta)))
        else:
            next_direction = -1 if delta > 0 else 1
            next_step = max(0.5, min(2.0, abs(delta) / 2.0))

    action = "LENGTHEN" if next_direction > 0 else "SHORTEN"
    return {"status": "awaiting-user", "step_cm": next_step,
            "direction": next_direction,
            "instruction": "🦀 %s by %g cm" % (action, next_step)}


def role_restore_plan(previous_role, temporary_role):
    """Role setter calls needed to undo a temporary dedicated role."""
    plan = []
    if temporary_role and temporary_role != previous_role:
        plan.append((temporary_role, False))
    if previous_role in _ROLES and previous_role != temporary_role:
        plan.append((previous_role, True))
    return plan


def apply_measurement(session, point):
    """Advance the UI state machine after one persisted measurement."""
    session["points"].append(point)
    if not point.get("valid"):
        session["status"] = "awaiting-user"
        session["retry_only"] = True
        session["instruction"] = "🦀 INVALID: %s. Wait, then measure again." % (
            point.get("invalid_reason") or "measurement unavailable")
        return
    session["retry_only"] = False
    decision = hill_climb(session["points"], session["step_cm"],
                          session["direction"])
    session["status"] = decision["status"]
    session["step_cm"] = decision["step_cm"]
    session["direction"] = decision["direction"]
    session["converged_length_cm"] = decision.get("converged_length_cm")
    session["instruction"] = decision["instruction"]


def _active_role(entry):
    active = [role for role in _ROLES if entry.get(role)]
    if len(active) > 1:
        raise ValueError("Dongle has multiple active receiver roles")
    return active[0] if active else None


def _pass_target_hz():
    with state.status_lock:
        if not state.is_pass_active or not state.current_pass:
            return None
        current_pass = dict(state.current_pass)
    freq = current_pass.get("frequency") or current_pass.get("frequency_hz")
    if freq is None:
        return None
    freq = int(freq)
    return freq if 137_000_000 <= freq <= 138_000_000 else None


def _set_manual_frequency(did, frequency_hz):
    with state.status_lock:
        if frequency_hz is None:
            state.manual_dongle_freq.pop(did, None)
        else:
            state.manual_dongle_freq[did] = int(frequency_hz)


def _role_snapshot(did, entry):
    with state.status_lock:
        manual_hz = state.manual_dongle_freq.get(did)
    role = _active_role(entry)
    return {"role": role or ("manual" if manual_hz is not None else "sat"),
            "manual_frequency_hz": manual_hz,
            "aprs_iss": bool(entry.get("aprs_iss")),
            "aprs_lasar": bool(entry.get("aprs_lasar"))}


def _restore_hardware(session):
    did = session["dongle"]
    entry = state.sdrs.get(did)
    if entry is None:
        state.log_console(f"🦀 Prawntuner: dongle {did} vanished before restore", "warn")
        return
    previous = session["previous"]
    for role, on in role_restore_plan(previous["role"],
                                      session.get("temporary_role")):
        _ROLE_SETTERS[role](did, on)
    if session.get("aprs_preset_changed"):
        radio.set_dongle_aprs_iss(did, previous["aprs_iss"])
        radio.set_dongle_aprs_lasar(did, previous["aprs_lasar"])
    _set_manual_frequency(did, previous["manual_frequency_hz"])


def _latest_id(table):
    row = db.query_one(f"SELECT COALESCE(MAX(id), 0) AS id FROM {table}")
    return int(row["id"]) if row else 0


def _decode_rows(table, after_id, did):
    rows = db.query(f"SELECT id, entry FROM {table} WHERE id > ? ORDER BY id",
                    (after_id,))
    found = []
    for row in rows:
        try:
            entry = json.loads(row["entry"])
        except (TypeError, ValueError):
            continue
        source = entry.get("dongle") or entry.get("dng")
        if source == did:
            found.append(entry)
    return found


def _measure_row(magnitude, half_band_hz):
    row = np.asarray(magnitude, dtype=np.float64)
    n = len(row)
    bin_hz = SDR_RATE / FFT_SIZE
    center = n // 2
    half = max(1, int(half_band_hz / bin_hz))
    lo, hi = max(0, center - half), min(n, center + half + 1)
    bins = row[lo:hi]
    offsets = (np.arange(lo, hi) - center) * bin_hz
    dc = np.abs(offsets - SDR_OFFSET_HZ) <= SPIKE_GUARD_HZ
    usable = bins[~dc]
    if not len(usable):
        return None
    floor = max(float(np.median(usable)), 1e-9)
    peak = float(np.max(usable))
    ratio = max(peak / floor, 1e-9)
    scale = 31.875 * FFT_SIZE
    return {"peak_dbfs": round(20.0 * math.log10(max(peak, 1e-9) / scale), 2),
            "floor_dbfs": round(20.0 * math.log10(floor / scale), 2),
            "ratio": round(ratio, 3),
            "ratio_db": round(20.0 * math.log10(ratio), 2)}


def _sample_spectrum(session, started):
    entry = state.sdrs.get(session["dongle"])
    if entry is None:
        return [], "Dongle is no longer registered"
    service = SERVICES[session["service"]]
    settle_until = started + SCAN_SETTLE_SECS
    finish_at = settle_until + service["dwell_secs"]
    hard_deadline = finish_at + service["dwell_secs"] * 3
    rows, seen = [], None
    while time.time() < hard_deadline:
        if session["stop_event"].is_set():
            return [], "Measurement stopped"
        now = time.time()
        if session["service"] == "satellite" and _pass_target_hz() is None:
            return rows, "LRPT pass ended during measurement"
        if now >= settle_until:
            with entry["lock"]:
                mag = entry.get("last_mag")
                mag_ts = entry.get("last_mag_ts", 0.0)
            if mag is not None and mag is not seen and mag_ts >= settle_until:
                seen = mag
                clean = np.asarray(mag, dtype=np.float64).copy()
                median = float(np.median(clean))
                if median > 0:
                    np.clip(clean, 0.0, 8.0 * median, out=clean)
                rows.append(clean)
        if now >= finish_at and len(rows) >= 5:
            break
        session["measurement_remaining"] = max(0, round(finish_at - now))
        session["stop_event"].wait(SCAN_SAMPLE_EVERY)
    if len(rows) < 5:
        return rows, "Not enough fresh spectrum rows from this dongle"
    return rows, None


def _log_point(session, point):
    with db.write() as cur:
        cur.execute("INSERT INTO tuner_points"
                    " (session_id, ts, length_cm, peak_dbfs, floor_dbfs,"
                    " ratio, decodes, valid, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (session["id"], point["ts"], point["length_cm"],
                     point.get("peak_dbfs"), point.get("floor_dbfs"),
                     point.get("ratio"), point.get("decodes"),
                     1 if point["valid"] else 0, point.get("note")))


def _update_session_row(session):
    best = [point for point in session["points"]
        if point.get("valid") and point.get("ratio_db") is not None]
    best_point = max(best, key=lambda p: (p["ratio_db"],
              p["decodes"] if p.get("decodes") is not None else -1)) \
    if best else None
    with db.write() as cur:
        cur.execute("UPDATE tuner_sessions SET converged_length_cm=?, best_dbfs=?"
                    " WHERE id=?",
                    (session.get("converged_length_cm"),
                     best_point.get("peak_dbfs") if best_point else None,
                     session["id"]))


def _finish_measurement(session, started, after_id, rows, sample_error):
    ended = time.time()
    service = session["service"]
    decoded = None
    events = []
    if service in _LOG_TABLES:
        events = _decode_rows(_LOG_TABLES[service], after_id, session["dongle"])
        decoded = len(events)
    valid = sample_error is None
    invalid_reason = sample_error
    if service == "satellite" and _pass_target_hz() is None:
        valid = False
        invalid_reason = "LRPT pass ended during measurement"
    if service == "orbcomm":
        if not events or ended - float(events[-1].get("ts", 0)) > 5.0:
            valid = False
            invalid_reason = "Orbcomm traffic disappeared during the dwell"

    measurement = None
    if rows:
        averaged = np.mean(rows, axis=0)
        measurement = _measure_row(averaged,
                                   SERVICES[service]["half_band_hz"])
    if measurement is None:
        valid = False
        invalid_reason = invalid_reason or "No measurable spectrum"
    point = {
        "ts": round(ended, 2),
        "length_cm": session["length_cm"],
        "peak_dbfs": measurement["peak_dbfs"] if measurement else None,
        "floor_dbfs": measurement["floor_dbfs"] if measurement else None,
        "ratio": measurement["ratio"] if measurement else None,
        "ratio_db": measurement["ratio_db"] if measurement else None,
        "decodes": decoded,
        "valid": valid,
        "note": session.pop("pending_note", ""),
        "invalid_reason": invalid_reason,
        "rows": len(rows),
    }
    _log_point(session, point)
    session["measurement_remaining"] = 0
    point["invalid_reason"] = invalid_reason
    apply_measurement(session, point)
    if session["status"] == "converged":
        _update_session_row(session)
    _update_session_row(session)
    state.log_console("🦀 Prawntuner %s on %s at %.2f cm: %s%s" %
                      ("measurement" if valid else "INVALID measurement",
                       session["service"], session["length_cm"],
                       point["ratio_db"] if point["ratio_db"] is not None else "no signal",
                       " dB over floor; %s decodes" % decoded
                       if decoded is not None else " dB over floor"))


def _measurement_worker(session):
    started = time.time()
    session["measurement_started"] = started
    session["retry_only"] = False
    after_id = (_latest_id(_LOG_TABLES[session["service"]])
                if session["service"] in _LOG_TABLES else 0)
    if session["service"] == "satellite":
        target = _pass_target_hz()
        if target is None:
            with _lock:
                if not session.get("finished") and not session["stop_event"].is_set():
                    _finish_measurement(session, started, after_id, [],
                                        "No active LRPT pass")
            return
        session["target_hz"] = target
        _set_manual_frequency(session["dongle"], target)
    rows, error = _sample_spectrum(session, started)
    if session["stop_event"].is_set():
        return
    with _lock:
        if session.get("finished"):
            return
        _finish_measurement(session, started, after_id, rows, error)


def _snapshot(session):
    if session is None:
        return {"status": "idle", "session": None, "points": [],
                "instruction": "🦀 Pick a dongle and service to tune your feelers."}
    return {
        "status": session["status"], "session": session["id"],
        "dongle": session["dongle"], "service": session["service"],
        "antenna": session["antenna"], "target_hz": session["target_hz"],
        "wave_factor": session.get("wave_factor", 0.25),
        "nominal_cm": session["nominal_cm"],
        "initial_length_cm": session["initial_length_cm"],
        "length_cm": session["length_cm"],
        "step_cm": session["step_cm"], "direction": session["direction"],
        "dwell_secs": SERVICES[session["service"]]["dwell_secs"],
        "measurement_remaining": session.get("measurement_remaining", 0),
        "instruction": session["instruction"],
        "converged_length_cm": session.get("converged_length_cm"),
        "points": [dict(point) for point in session["points"]],
        "retry_only": bool(session.get("retry_only")),
        "error": session.get("error"),
    }


def tuner_snapshot(did=None):
    with _lock:
        if did:
            session = _sessions.get(did) or _latest.get(did)
        else:
            candidates = list(_sessions.values()) or list(_latest.values())
            session = max(candidates, key=lambda s: s["started"]) if candidates else None
        if session is not None and not session.get("finished"):
            session["last_activity"] = time.time()
        return _snapshot(session)


def _measurement_thread(session):
    thread = threading.Thread(target=_measurement_worker, args=(session,),
                              daemon=True, name="prawntuner-%s" % session["dongle"])
    session["thread"] = thread
    thread.start()


def _ensure_watchdog():
    global _watchdog
    with _lock:
        if _watchdog is None or not _watchdog.is_alive():
            _watchdog = threading.Thread(target=_watch_sessions, daemon=True,
                                          name="prawntuner-watchdog")
            _watchdog.start()


def _watch_sessions():
    while True:
        time.sleep(5)
        now = time.time()
        with _lock:
            expired = [did for did, session in _sessions.items()
                       if now - session["last_activity"] > INACTIVITY_SECS
                       or did not in state.sdrs]
        for did in expired:
            stop_session(did, reason="inactive")


def start_session(did, service, antenna, wave_factor="0.25"):
    """Claim one free dongle, park it and begin its first measurement."""
    did = str(did or "").strip()
    if service not in SERVICES:
        return {"success": False, "error": "Unknown service"}, 400
    if antenna not in ("whip", "vdipole"):
        return {"success": False, "error": "Choose Whip or V-Dipole"}, 400
    entry = state.sdrs.get(did)
    if entry is None:
        return {"success": False, "error": "Unknown dongle"}, 404
    gain = SDR_DONGLE_GAIN.get(did, SDR_GAIN)
    if gain is None or float(gain) <= 0:
        return {"success": False,
                "error": "This dongle has no fixed manual gain; configure SDR_DONGLE_GAIN first"}, 409
    target = _pass_target_hz() if service == "satellite" else SERVICES[service]["frequency_hz"]
    if service == "satellite" and target is None:
        return {"success": False, "error": "Wait for a live LRPT pass before tuning satellites"}, 409
    if entry.get("primary") and state.is_pass_active:
        return {"success": False,
                "error": "The primary satellite dongle is protected during a pass"}, 409
    if entry.get("paused") or not entry.get("connected"):
        return {"success": False, "error": "Dongle is paused or disconnected"}, 409
    if entry.get("is_recording") or did in state.manual_recording:
        return {"success": False, "error": "Stop recording before tuning this dongle"}, 409
    scan_state = state.scans.get(did)
    if scan_state and scan_state.get("active"):
        return {"success": False, "error": "Stop the frequency scan before tuning"}, 409
    try:
        previous = _role_snapshot(did, entry)
    except ValueError as e:
        return {"success": False, "error": str(e)}, 409
    wanted_role = service if service in _ROLES else None
    if previous["role"] in _ROLES and previous["role"] != wanted_role:
        return {"success": False,
                "error": "Dongle is already dedicated to %s" % previous["role"]}, 409
    if service == "satellite" and previous["role"] in _ROLES:
        return {"success": False, "error": "Choose a satellite or manual dongle for LRPT"}, 409
    if service == "aprs" and previous["role"] == "aprs" and (
            previous["aprs_iss"] or previous["aprs_lasar"]):
        return {"success": False,
                "error": "Switch APRS to the 144.800 MHz simplex channel first"}, 409

    lengths = initial_lengths(target, antenna, float(wave_factor))
    session = {
        "id": None, "dongle": did, "service": service, "antenna": antenna,
        "wave_factor": float(wave_factor),
        "target_hz": int(target), "nominal_cm": lengths["nominal_cm"],
        "initial_length_cm": lengths["start_cm"],
        "length_cm": lengths["start_cm"], "step_cm": 2.0,
        "direction": 1, "status": "measuring",
        "instruction": "🦀 Measuring your feelers; hold still for the dwell.",
        "points": [], "started": time.time(), "last_activity": time.time(),
        "previous": previous, "temporary_role": None,
        "aprs_preset_changed": False, "retry_only": False,
        "stop_event": threading.Event(), "thread": None, "finished": False,
        "converged_length_cm": None,
    }
    with _lock:
        if did in _sessions:
            return {"success": False, "error": "Prawntuner already owns this dongle"}, 409
        try:
            if service == "aprs" and not entry.get("aprs") and (
                    entry.get("aprs_iss") or entry.get("aprs_lasar")):
                radio.set_dongle_aprs_iss(did, False)
                radio.set_dongle_aprs_lasar(did, False)
                session["aprs_preset_changed"] = True
            if wanted_role and previous["role"] != wanted_role:
                if not _ROLE_SETTERS[wanted_role](did, True):
                    raise RuntimeError("Could not switch dongle role")
                session["temporary_role"] = wanted_role
            elif service == "satellite":
                _set_manual_frequency(did, target)
            with db.write() as cur:
                cur.execute("INSERT INTO tuner_sessions"
                            " (dongle, service, antenna, wave_factor, started, target_hz,"
                            " nominal_cm, initial_length_cm, restore_json)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (did, service, antenna, session["started"], target,
                             session["nominal_cm"], session["initial_length_cm"],
                             json.dumps(previous, separators=(",", ":"))))
                session["id"] = cur.lastrowid
            _sessions[did] = session
            _latest[did] = session
            _ensure_watchdog()
            _measurement_thread(session)
        except Exception as e:
            _restore_hardware(session)
            return {"success": False, "error": str(e)}, 500
    state.log_console("🦀 Prawntuner started on %s for %s feelers" % (did, service))
    result = _snapshot(session)
    result.update({"success": True, "nominal_cm": lengths["nominal_cm"],
                   "initial_length_cm": lengths["start_cm"]})
    return result, 200


def _start_measurement(session):
    if session["status"] == "measuring":
        return False, "A measurement is already running"
    if session.get("finished"):
        return False, "Prawntuner session has ended"
    if session["service"] == "satellite":
        target = _pass_target_hz()
        if target is None:
            return False, "Wait for the next live LRPT pass"
        session["target_hz"] = target
        _set_manual_frequency(session["dongle"], target)
    session["last_activity"] = time.time()
    session["status"] = "measuring"
    session["instruction"] = "🦀 Measuring your feelers; hold still for the dwell."
    session["stop_event"] = threading.Event()
    _measurement_thread(session)
    return True, None


def report_length(did, length_cm, note=""):
    try:
        length = float(length_cm)
    except (TypeError, ValueError):
        return {"success": False, "error": "Enter a valid length in cm"}, 400
    if not math.isfinite(length) or not 0.1 <= length <= 1000:
        return {"success": False, "error": "Length must be between 0.1 and 1000 cm"}, 400
    with _lock:
        session = _sessions.get(did)
        if session is None:
            return {"success": False, "error": "No active Prawntuner session"}, 404
        if session["status"] == "measuring":
            return {"success": False, "error": "Wait for this measurement to finish"}, 409
        if session.get("retry_only"):
            return {"success": False,
                    "error": "This point was invalid; wait for traffic and measure again"}, 409
        session["length_cm"] = round(length, 2)
        session["pending_note"] = str(note or "")[:200]
        session["last_activity"] = time.time()
        ok, error = _start_measurement(session)
        if not ok:
            return {"success": False, "error": error}, 409
        return {"success": True, **_snapshot(session)}, 200


def measure_again(did):
    with _lock:
        session = _sessions.get(did)
        if session is None:
            return {"success": False, "error": "No active Prawntuner session"}, 404
        ok, error = _start_measurement(session)
        if not ok:
            return {"success": False, "error": error}, 409
        return {"success": True, **_snapshot(session)}, 200


def stop_session(did, reason="user"):
    with _lock:
        session = _sessions.get(did)
        if session is None:
            return {"success": True, "status": "idle"}, 200
        session["stop_event"].set()
        thread = session.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=5)
    with _lock:
        if session.get("finished"):
            return {"success": True, **_snapshot(session)}, 200
        session["finished"] = True
        session["status"] = "idle"
        session["instruction"] = "🦀 Session ended; your dongle is back in its previous role."
        session["last_activity"] = time.time()
        try:
            _restore_hardware(session)
        except Exception as e:
            session["error"] = str(e)
            state.log_console(f"🦀 Prawntuner could not restore dongle {did}: {e}", "error")
        with db.write() as cur:
            cur.execute("UPDATE tuner_sessions SET ended=?, converged_length_cm=?,"
                        " best_dbfs=? WHERE id=?",
                        (time.time(), session.get("converged_length_cm"),
                            max((p["peak_dbfs"] for p in session["points"]
                                if p.get("valid") and p.get("peak_dbfs") is not None),
                             default=None), session["id"]))
        _sessions.pop(did, None)
    state.log_console("🦀 Prawntuner session %s on %s (%s)" %
                      ("ended" if reason == "user" else "released after inactivity",
                       did, session["service"]))
    return {"success": True, **_snapshot(session)}, 200
