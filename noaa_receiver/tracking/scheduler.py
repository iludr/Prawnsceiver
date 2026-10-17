"""Pass scheduler: TLE refresh, pass windows, auto-decode, pass logging."""
import glob
import os
import time
from datetime import datetime, timedelta, timezone

from .. import state
from ..config import LAT, LON, PASS_MARGIN_SECS, PASS_PREDICT_HOURS, RECORD_ISS, RECORD_LOCAL_DIR, TLE_REFRESH_HOURS, TRACKED_SATS, UTC_OFFSET

from ..decoding.decode import decode_recording, read_decode_marker, sat_short_name
from ..web.history import log_pass
from .passes import HAS_SKYFIELD, load, load_tles_from_cache, predict_passes, refresh_tles, wgs84
from .satnogs import satellite_info
from . import txstatus

def _doppler_hz(sat, freq_hz):
    """Live Doppler shift of the satellite's carrier at the site (Hz).

    f_observed = f * (1 - v_los/c) with v_los the range rate (positive when
    receding), so the correction the receiver applies is -v_los/c * f.
    """
    ts = load.timescale()
    site = wgs84.latlon(LAT, LON)
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    t0 = ts.from_datetime(now)
    t1 = ts.from_datetime(now + timedelta(seconds=2))
    r0 = (sat - site).at(t0).distance().m
    r1 = (sat - site).at(t1).distance().m
    v_los = (r1 - r0) / 2.0
    return int(round(-v_los / 299792458.0 * freq_hz))

def _reception_score(p):
    """Heuristic reception quality of a pass at this station.

    Elevation dominates (path loss, horizon obstructions on a balcony). Low
    passes culminating in the V-dipole's null sectors (east/west — its
    figure-0 pattern favors north-zenith-south, measured weak here) get a
    penalty, so a decent meridian pass is preferred over the same-elevation
    pass on the wrong side of the sky.
    """
    alt = p["max_alt"]
    az = p.get("culm_az")
    if alt < 35 and az is not None and (60 <= az <= 120 or 240 <= az <= 300):
        return alt * 0.6
    return float(alt)

def _same_pass(p, q):
    """Two pass dicts describe the same physical pass?

    Rise times are compared with a small tolerance: every re-prediction
    recomputes them and the last microseconds can differ between runs, so
    exact equality would make the scheduler treat the running pass as a
    NEW pass after each 30-min re-predict (observed live: four PASS START
    lines for one 04:29 pass, each ending/decoding it prematurely). Two
    passes of one satellite never rise within a minute of each other, so
    30 s is a safe margin.
    """
    if p is None or q is None or p["sat_name"] != q["sat_name"]:
        return False
    return abs((p["rise_utc"] - q["rise_utc"]).total_seconds()) <= 30

def scheduler_thread():
    """Background thread: refresh TLEs, predict passes, trigger frequency switches."""
    # Warm the home-grown transmitter ledger at boot: the pass rows
    # (tx_home dark-streak chips) read a non-rebuilding cache that the
    # first finished pass would otherwise only fill hours later
    for _name, _freq in TRACKED_SATS.values():
        txstatus.ledger(_name)
    sats = {}
    while True:
        try:
            # Refresh TLEs only when stale (or on the first run); between
            # refreshes the pass prediction reuses the loaded satellites.
            # A refresh that yields nothing (Celestrak down, no cache)
            # clears last_tle_refresh so the next outer-loop pass (~30 min)
            # retries instead of leaving the receiver blind for hours.
            if state.last_tle_refresh == 0:
                # First run after a restart: cached TLEs only — an online
                # fetch can take minutes when the sources are slow/down and
                # would leave the receiver blind right when a pass triggers.
                # Freshness comes from the periodic refresh below and the
                # manual sync button (/sync_tle).
                sats = load_tles_from_cache()
                state.last_tle_refresh = time.time()
            elif state.tle_sync_requested:
                # Manual sync (dashboard button): one fetch per click
                state.tle_sync_requested = False
                state.log_console("Manual TLE sync: fetching fresh elements")
                new_sats = refresh_tles()
                if new_sats:
                    sats = new_sats
            elif time.time() - state.last_tle_refresh > TLE_REFRESH_HOURS * 3600:
                new_sats = refresh_tles()
                if new_sats:
                    sats = new_sats
                else:
                    state.last_tle_refresh = 0
            
            # Predict passes
            passes = predict_passes(sats, PASS_PREDICT_HOURS)
            predicted_at = time.time()
            with state.status_lock:
                state.upcoming_passes = passes
            
            state.log_console(f"Predicted {len(passes)} passes in next {PASS_PREDICT_HOURS}h")
            for p in passes[:5]:
                local_rise = p["rise_utc"] + timedelta(hours=UTC_OFFSET)
                state.log_console(f"  {p['sat_name']} {p['max_alt']:.0f}° at {local_rise.strftime('%H:%M')} ({round(p['frequency']/1e6,4)} MHz)")
            
            # Check every 10 seconds if we need to switch for an upcoming pass
            while True:
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                # All passes whose recording window (rise/set ± margin) is open now
                candidates = []
                for p in passes:
                    if not RECORD_ISS and p["sat_name"].startswith("ISS"):
                        continue
                    start_time = p["rise_utc"] - timedelta(seconds=PASS_MARGIN_SECS)
                    end_time = p["set_utc"] + timedelta(seconds=PASS_MARGIN_SECS)
                    if start_time <= now <= end_time:
                        candidates.append(p)
                
                triggered = None
                if candidates:
                    # Prefer the pass with the best reception chances:
                    # pick the highest-scoring candidate (elevation, with a
                    # penalty for low passes in the antenna's null sectors).
                    # While a pass is running, stick with it unless another
                    # candidate is clearly better (hysteresis) — overlapping
                    # passes must not flip the receiver back and forth.
                    with state.status_lock:
                        cur = state.current_pass
                    best = max(candidates, key=_reception_score)
                    ongoing = None
                    if cur is not None:
                        ongoing = next((c for c in candidates if _same_pass(c, cur)), None)
                    if ongoing is not None:
                        if _reception_score(best) > _reception_score(ongoing) + 20:
                            triggered = best
                        else:
                            triggered = ongoing
                    else:
                        triggered = best
                
                # Manual tune mode (FM radio test): the operator controls the
                # frequency — no satellite switching, no recording. An ongoing
                # pass is finished cleanly by the branch below.
                with state.status_lock:
                    manual = state.manual_frequency
                if manual is not None:
                    triggered = None
                
                finished_pass = None
                pass_peak = 0.0
                with state.status_lock:
                    if triggered and not _same_pass(state.current_pass, triggered):
                        if state.current_pass is not None:
                            # Direct switch between overlapping passes: close
                            # out the old pass (log + decode) before moving on
                            finished_pass = state.current_pass
                            with state.signal_lock:
                                pass_peak = state.pass_signal_peak
                                state.pass_signal_peak = 0.0
                        # Effective tune with the SatNOGS dead-transmitter
                        # fallback (txstatus): fb_note is set when the
                        # tracked frequency's transmitter is flagged dead
                        # and an alternate alive one was chosen instead
                        tune, fb_note = txstatus.effective_freq(
                            triggered["catnr"], triggered["sat_name"],
                            triggered["frequency"])
                        # Reflect the actually-tuned frequency in the pass
                        # dict so banner, Doppler block and history logging
                        # all agree (Doppler follows automatically:
                        # the block below reads this field)
                        triggered["frequency"] = tune
                        state.current_pass = triggered
                        state.current_frequency = tune
                        state.current_sat_name = triggered["sat_name"]
                        state.is_pass_active = True
                        with state.signal_lock:
                            state.pass_signal_peak = 0.0
                        local_rise = triggered["rise_utc"] + timedelta(hours=UTC_OFFSET)
                        state.log_console(f"🔴 PASS START: {triggered['sat_name']} {round(tune/1e6,4)} MHz, max {triggered['max_alt']:.0f}° at {local_rise.strftime('%H:%M')}")
                        if fb_note:
                            state.log_console(f"🛰 {fb_note}")
                    elif not triggered and state.current_pass is not None:
                        finished_pass = state.current_pass
                        local_set = finished_pass["set_utc"] + timedelta(hours=UTC_OFFSET)
                        state.log_console(f"✅ PASS END: {finished_pass['sat_name']} finished at {local_set.strftime('%H:%M')}")
                        state.current_pass = None
                        state.is_pass_active = False
                        # Return to the idle park frequency — or, in manual
                        # tune mode, stay on the operator's frequency
                        if manual is not None:
                            state.current_frequency = manual
                            state.current_sat_name = f"Manual {manual/1e6:.4f} MHz"
                        else:
                            state.current_frequency = 137912500
                            state.current_sat_name = "idle"
                        with state.signal_lock:
                            pass_peak = state.pass_signal_peak
                            state.pass_signal_peak = 0.0

                # Doppler correction for the active pass: range-rate from the
                # same TLEs, applied in software by the capture threads
                # (steps stay < ~1 kHz between ticks even on ISS passes)
                with state.status_lock:
                    cur = state.current_pass
                if cur is not None and HAS_SKYFIELD and cur.get("sat") is not None:
                    try:
                        dop = _doppler_hz(cur["sat"], cur["frequency"])
                        with state.status_lock:
                            state.doppler_freq_hz = cur["frequency"]
                            state.doppler_hz = dop
                    except Exception as e:
                        state.log_console(f"Doppler computation failed: {e}", "error")
                elif state.doppler_hz:
                    with state.status_lock:
                        state.doppler_hz = 0
                        state.doppler_freq_hz = 0

                if finished_pass is not None:
                    # Wait for the capture threads to finalize their WAVs, then
                    # auto-decode every recording of this pass — one per dongle.
                    # The pass history tracks the primary dongle's recording.
                    time.sleep(2)
                    decoded = False
                    png_file = None
                    wav_name = None
                    quality = None
                    pass_start_ts = finished_pass["rise_utc"].timestamp() - PASS_MARGIN_SECS
                    # History attribution matches by filename: overlapping
                    # same-frequency passes interleave neighbor-satellite
                    # WAVs in the same time window
                    attrib_prefix = sat_short_name(finished_pass["sat_name"]) + "_"
                    # Fresh pass recordings sit in the LOCAL capture dir until the
                    # archiver moves them (see archive.py)
                    recordings = sorted(glob.glob(os.path.join(RECORD_LOCAL_DIR, "*.wav")), key=os.path.getmtime, reverse=True)
                    # Never pick WAVs that are still being written
                    with state.status_lock:
                        active_wav = state.current_wav_path
                    if active_wav:
                        recordings = [r for r in recordings if os.path.abspath(r) != os.path.abspath(active_wav)]
                    dongle_suffixes = tuple(f"_{sn}.wav" for sn in list(state.sdrs))
                    for latest in recordings:
                        if os.path.getmtime(latest) < pass_start_ts:
                            break  # sorted newest-first: older files belong to earlier passes
                        wav_base = os.path.basename(latest)
                        latest_png = latest.replace('.wav', '.png')
                        png_path = latest_png if os.path.exists(latest_png) else None
                        rec_decoded = os.path.exists(latest_png)
                        if not rec_decoded:
                            marker = read_decode_marker(latest)
                            if marker is not None and not marker.get('success'):
                                # Previous attempt failed (dark transmitter,
                                # no raw IQ, decoder error) — the marker
                                # short-circuits re-decodes; say so once
                                state.log_console(f"Auto-decode skipped, previous attempt failed: {wav_base}")
                            else:
                                state.log_console(f"Auto-decoding: {wav_base}")
                                rec_decoded, png_path, err = decode_recording(latest)
                                if rec_decoded:
                                    state.log_console(f"Auto-decode successful: {os.path.basename(png_path) if png_path else 'background decode started'}")
                                else:
                                    state.log_console(f"Auto-decode failed for {wav_base}: {err}", "error")
                        # Comparison-dongle recordings (serial-suffixed) are
                        # decoded but not part of the pass history
                        if wav_base.endswith(dongle_suffixes):
                            continue
                        # Attribute only recordings of THIS pass's satellite,
                        # and only the newest primary fragment (newest-first
                        # order — the most complete after a mid-pass restart)
                        if not wav_base.startswith(attrib_prefix):
                            continue
                        if wav_name is None:
                            decoded = bool(rec_decoded)
                            png_file = os.path.basename(png_path) if (png_path and os.path.exists(png_path)) else None
                            wav_name = wav_base
                    # Snapshot the SatNOGS DB record with the pass: the
                    # history page shows what was tracked (names, launch,
                    # transmitters) even long after the satellite changes
                    satnogs = None
                    try:
                        satnogs = satellite_info(finished_pass["catnr"])[0]
                    except Exception:
                        pass
                    log_pass(finished_pass["sat_name"], finished_pass["frequency"],
                             finished_pass["max_alt"], finished_pass["duration_min"],
                             finished_pass["rise_utc"], finished_pass["set_utc"],
                             pass_peak, decoded, png_file, wav_name, quality, satnogs)
                    # Home-grown transmitter ledger: this pass's lit/dark
                    # result rolls into the per-satellite activity stats,
                    # and a state change raises the console alert
                    txstatus.note_pass_result(finished_pass["sat_name"],
                                              png_file)
                
                # Manual TLE sync requested: break to the outer loop, which
                # refetches and re-predicts
                if state.tle_sync_requested:
                    break
                # Re-predict every ~30 min — but at most ONCE: the old
                # wall-clock test (minute % 30 == 0, second < 10) matched on
                # every 10-s tick inside that window, so the outer loop
                # re-predicted in a tight spin and flooded the console
                # buffer (100 lines of "Predicted N passes" per half hour,
                # hiding every decode diagnostic — observed 2026-10-17).
                if time.time() - predicted_at >= 29 * 60:
                    break
                
                time.sleep(10)
        except Exception as e:
            state.log_console(f"Scheduler error: {e}", "error")
            time.sleep(60)
