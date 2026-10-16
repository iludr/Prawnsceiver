"""Satellite TLE handling and pass prediction (skyfield)."""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import db
from .. import state
from .plan import receive_plan, transmitter_status
from ..config import LAT, LON, TRACKED_SATS, PASS_MIN_ALT, TLE_USER_AGENT, UTC_OFFSET

try:
    from skyfield.api import load, wgs84, EarthSatellite
    HAS_SKYFIELD = True
except ImportError:
    load = wgs84 = EarthSatellite = None
    HAS_SKYFIELD = False

def _fetch_tle_celestrak(catnr):
    """Fetch raw TLE lines from Celestrak."""
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"
    req = urllib.request.Request(url, headers={"User-Agent": TLE_USER_AGENT})
    text = urllib.request.urlopen(req, timeout=15).read().decode()
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if len(lines) >= 3:
        return lines[:3]
    return None


def _fetch_tle_satnogs(catnr):
    """Fetch raw TLE lines from the SatNOGS DB API (fallback source)."""
    url = f"https://db.satnogs.org/api/tle/?norad_cat_id={catnr}"
    req = urllib.request.Request(url, headers={"User-Agent": TLE_USER_AGENT, "Accept": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    if data:
        entry = data[0]
        return [entry["tle0"].strip(), entry["tle1"].strip(), entry["tle2"].strip()]
    return None


def fetch_tle_lines(catnr):
    """Fetch raw TLE lines; SatNOGS DB first (primary), Celestrak as fallback."""
    last_err = None
    for source in (_fetch_tle_satnogs, _fetch_tle_celestrak):
        try:
            lines = source(catnr)
            if lines:
                return lines
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise last_err
    return None


def _sat_from_lines(lines, ts):
    """Build an EarthSatellite from raw TLE lines."""
    return EarthSatellite(lines[1], lines[2], lines[0], ts)


def load_tles_from_cache():
    """Load TLEs from the station database only - no network, seconds
    not minutes. Used at startup: when the online sources are slow or
    down, an eager fetch leaves the receiver blind for minutes right
    after every restart, exactly when a pass may need triggering.
    Freshness is kept by the periodic refresh and the manual sync
    (/sync_tle) instead."""
    if not HAS_SKYFIELD:
        state.log_console("Skyfield not available, cannot predict passes", "warn")
        return {}
    rows = db.query("SELECT catnr, name, l1, l2 FROM tle_cache")
    if not rows:
        state.log_console("No TLE cache yet - start a sync from the dashboard or wait for the periodic refresh", "warn")
        return {}
    try:
        ts = load.timescale()
        sats = {}
        for r in rows:
            if r["catnr"] not in TRACKED_SATS:
                continue
            name, freq = TRACKED_SATS[r["catnr"]]
            sats[r["catnr"]] = (_sat_from_lines([r["name"], r["l1"], r["l2"]], ts),
                                name, freq)
        state.log_console(f"Loaded {len(sats)} TLEs from cache (no network fetch)")
        return sats
    except Exception as e:
        state.log_console(f"TLE cache read failed: {e}", "error")
        return {}


def refresh_tles():
    """Refresh TLE data from SatNOGS (Celestrak fallback), with an on-disk
    cache as last resort."""
    if not HAS_SKYFIELD:
        state.log_console("Skyfield not available, cannot predict passes", "warn")
        return {}
    ts = load.timescale()
    sats = {}
    tle_data = {}
    with state.status_lock:
        state.tle_progress = {"active": True, "done": 0,
                              "total": len(TRACKED_SATS), "current": None}
    try:
        for catnr, (name, freq) in TRACKED_SATS.items():
            with state.status_lock:
                state.tle_progress["current"] = name
            try:
                lines = fetch_tle_lines(catnr)
                if lines:
                    sats[catnr] = (_sat_from_lines(lines, ts), name, freq)
                    tle_data[str(catnr)] = lines
                    state.log_console(f"TLE loaded: {name} (cat #{catnr}), epoch={sats[catnr][0].epoch.utc_datetime()}")
            except Exception as e:
                state.log_console(f"TLE fetch failed for {name} (cat #{catnr}): {e}", "error")
            finally:
                with state.status_lock:
                    state.tle_progress["done"] += 1
        if sats:
            # Persist for future outages
            try:
                with db.write() as cur:
                    for catnr, lines in tle_data.items():
                        cur.execute("INSERT OR REPLACE INTO tle_cache"
                                    " VALUES (?,?,?,?,?)",
                                    (catnr, lines[0], lines[1], lines[2],
                                     time.time()))
            except Exception as e:
                state.log_console(f"TLE cache write failed: {e}", "warn")
        else:
            # Celestrak unreachable: reuse the last good TLEs
            state.log_console("TLE fetch failed for all satellites, using cached TLEs", "warn")
            try:
                sats = load_tles_from_cache()
            except Exception as e:
                state.log_console(f"TLE cache read failed: {e}", "error")
    finally:
        with state.status_lock:
            state.tle_progress["active"] = False
            state.tle_progress["current"] = None
    state.last_tle_refresh = time.time()
    return sats

def predict_passes(sats, hours=24):
    """Predict satellite passes over the next `hours` hours."""
    if not sats or not HAS_SKYFIELD:
        return []
    ts = load.timescale()
    site = wgs84.latlon(LAT, LON)
    now = ts.now()
    # Start the search before "now" so a pass that already rose but has not
    # set yet (e.g. the receiver was restarted mid-pass) is still predicted;
    # fully-past passes are dropped below.
    start = ts.tt_jd(now.tt - 1.0 / 24.0)
    end = ts.tt_jd(now.tt + hours / 24.0)
    
    passes = []
    for catnr, (sat, name, freq) in sats.items():
        try:
            t, events = sat.find_events(site, start, end, altitude_degrees=PASS_MIN_ALT)
            for i, (ti, event) in enumerate(zip(t, events)):
                if event == 0:  # rise
                    rise_time = ti.utc_datetime()
                    max_alt = 0
                    culm_time = rise_time
                    set_time = rise_time
                    duration_min = 0
                    if i + 1 < len(events) and events[i + 1] == 1:
                        culm_time = t[i + 1].utc_datetime()
                        topocentric = (sat - site).at(t[i + 1])
                        alt, az, dist = topocentric.altaz()
                        max_alt = alt.degrees
                        culm_az = az.degrees
                        if i + 2 < len(events) and events[i + 2] == 2:
                            set_time = t[i + 2].utc_datetime()
                            duration_min = (set_time - rise_time).total_seconds() / 60
                    passes.append({
                        "sat": sat,
                        "catnr": catnr,
                        "sat_name": name,
                        "frequency": freq,
                        "rise_utc": rise_time,
                        "culm_utc": culm_time,
                        "set_utc": set_time,
                        "max_alt": round(max_alt, 1),
                        "culm_az": round(culm_az, 1),
                        "duration_min": round(duration_min, 1),
                    })
        except Exception as e:
            state.log_console(f"Pass prediction failed for {name}: {e}", "error")
    
    # Drop passes that have fully set (lookback window above)
    passes = [p for p in passes if p["set_utc"] > datetime.now(timezone.utc)]
    passes.sort(key=lambda p: p["rise_utc"])
    return passes

def passes_to_json(passes):
    """Convert pass list to JSON-serializable format for the API."""
    result = []
    for p in passes:
        tuned = p["frequency"]
        mode, plan, demod_bw_khz, iq = receive_plan(p["catnr"], p["sat_name"])
        entry = {
            "sat_name": p["sat_name"],
            "catnr": p["catnr"],
            "frequency_mhz": round(tuned / 1e6, 4),
            "rise_local": (p["rise_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M"),
            "culm_local": (p["culm_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "set_local": (p["set_utc"] + timedelta(hours=UTC_OFFSET)).strftime("%H:%M"),
            "max_alt": p["max_alt"],
            "duration_min": p["duration_min"],
            "quality": "high" if p["max_alt"] >= 35 else ("medium" if p["max_alt"] >= 15 else "low"),
            "rise_timestamp": p["rise_utc"].timestamp(),
            "set_timestamp": p["set_utc"].timestamp(),
            # What to listen to and how it will be decoded (receive plan)
            "mode": mode,
            "plan": plan,
            "iq_recording": iq,
        }
        if demod_bw_khz:
            entry["demod_bw_khz"] = demod_bw_khz
        tx = transmitter_status(p["catnr"])
        if tx:
            entry["tx"] = {
                "description": tx.get("description"),
                "mode": tx.get("mode"),
                "alive": tx.get("alive"),
                "downlink_mhz": round(tx["downlink_hz"] / 1e6, 4),
            }
        result.append(entry)
    return result
