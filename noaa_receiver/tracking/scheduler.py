"""Pass scheduler: TLE refresh, pass windows, auto-decode, pass logging."""
import glob
import os
import time
from datetime import datetime, timedelta, timezone

from .. import state
from ..config import (ISS_SSTV_HZ, LAT, LON, METEOR_LRPT_HZ, PASS_MARGIN_SECS,
                      PASS_PREDICT_HOURS, RECORD_ISS, RECORD_LOCAL_DIR,
                      TLE_REFRESH_HOURS, TRACKED_SATS, UTC_OFFSET)

from ..decoding.decode import (decode_recording, read_decode_marker,
                               sat_short_name, satellite_from_filename)
from ..sdr import radio
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

# Idle park frequency per mode: the tune a mode's track returns to (and
# a dongle of that mode holds) between its passes.
MODE_PARK_HZ = {'meteor': METEOR_LRPT_HZ, 'iss': ISS_SSTV_HZ}

def _pass_mode(p):
    """The tracking mode ('meteor'/'iss') a predicted pass belongs to."""
    return 'iss' if p["sat_name"].startswith("ISS") else 'meteor'

def _finish_pass(finished_pass, pass_peak):
    """Close out one finished pass: wait for the capture threads to
    finalize their WAVs, auto-decode every recording of the pass (one
    per dongle), render the waterfall, log the pass history and roll
    the lit/dark result into the transmitter ledger. Extracted from the
    scheduler tick so the two independent mode tracks (meteor / iss)
    share it — they can finish in the same tick."""
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
    # Waterfall diagram of the pass recording — EVERY pass
    # leaves one, even when nothing was decoded (operator
    # request 2026-10-23): the history shows what the
    # receiver actually saw. Rendered from the raw IQ when
    # the pass recorded one, else from the WAV audio. The
    # pass entry prefers a real decode image; the waterfall
    # is the fallback. It must NOT feed the transmitter
    # ledger below — a waterfall is not a heard transmitter.
    pass_png = png_file
    if wav_name is not None:
        try:
            from ..analysis.waterfall import pass_waterfall
            wf = pass_waterfall(os.path.join(RECORD_LOCAL_DIR,
                                             wav_name))
            if wf:
                state.log_console(f"🌊 Pass waterfall: {os.path.basename(wf)}")
                if pass_png is None:
                    pass_png = os.path.basename(wf)
        except Exception as e:
            state.log_console(f"Pass waterfall failed for {wav_name}: {e}", "warn")
    # ISS passes KEEP their raw IQ (operator request
    # 2026-10-23): the SSTV decode runs on the WAV audio
    # and never touches the baseband, so the keep is
    # otherwise invisible — say so in the console. The
    # IQ is the fallback when that decode has issues.
    rec_sat = satellite_from_filename(wav_name) \
        if wav_name is not None else None
    if rec_sat is not None and rec_sat.startswith("ISS"):
        iq_path = os.path.join(
            RECORD_LOCAL_DIR,
            wav_name[:-len('.wav')] + '.iq.u8')
        if os.path.exists(iq_path):
            state.log_console(
                f"🛰 Raw IQ kept (ISS pass, decode fallback): "
                f"{os.path.basename(iq_path)} "
                f"({os.path.getsize(iq_path) / (1024 * 1024):.0f} MB)")
    log_pass(finished_pass["sat_name"], finished_pass["frequency"],
             finished_pass["max_alt"], finished_pass["duration_min"],
             finished_pass["rise_utc"], finished_pass["set_utc"],
             pass_peak, decoded, pass_png, wav_name, quality, satnogs)
    # Home-grown transmitter ledger: this pass's lit/dark
    # result rolls into the per-satellite activity stats,
    # and a state change raises the console alert
    txstatus.note_pass_result(finished_pass["sat_name"],
                              png_file)

def scheduler_thread():
    """Background thread: refresh TLEs, predict passes, trigger frequency switches.

    Two independent tracking modes (2026-10-25, they replaced the single
    auto-tracking mode): a METEOR track following the Meteor-M LRPT
    passes and an ISS track following the ISS passes. Each mode keeps
    its own running pass, frequency, Doppler and signal peak in
    state.mode_*; dongles follow the track of their configured sat mode
    (radio.set_dongle_sat_mode), so both families can be received at
    once on different dongles. The legacy single-track globals
    (state.current_pass, current_frequency, ...) are maintained as a
    summary of the two tracks for status.json, /track.json and the
    manual-tune endpoints."""
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
                # Open recording windows now, split by tracking mode: each
                # mode's track picks from its own candidates only
                candidates = {m: [] for m in state.SAT_MODES}
                for p in passes:
                    if not RECORD_ISS and p["sat_name"].startswith("ISS"):
                        continue
                    start_time = p["rise_utc"] - timedelta(seconds=PASS_MARGIN_SECS)
                    end_time = p["set_utc"] + timedelta(seconds=PASS_MARGIN_SECS)
                    if start_time <= now <= end_time:
                        candidates[_pass_mode(p)].append(p)

                # Manual tune mode (FM radio test): the operator controls the
                # frequency — no satellite switching, no recording. Ongoing
                # passes are finished cleanly by the branch below.
                with state.status_lock:
                    manual = state.manual_frequency

                finished = []   # (pass dict, signal peak) to log + decode
                started = []    # modes whose track started a pass this tick
                ended = []      # modes whose track ended a pass this tick
                with state.status_lock:
                    for mode in state.SAT_MODES:
                        cands = candidates[mode]
                        # Prefer the pass with the best reception chances:
                        # pick the highest-scoring candidate (elevation, with
                        # a penalty for low passes in the antenna's null
                        # sectors). While a pass is running, stick with it
                        # unless another candidate is clearly better
                        # (hysteresis) — overlapping passes must not flip
                        # the track back and forth.
                        triggered = None
                        if manual is None and cands:
                            best = max(cands, key=_reception_score)
                            cur = state.mode_pass[mode]
                            ongoing = next(
                                (c for c in cands if _same_pass(c, cur)),
                                None) if cur is not None else None
                            if ongoing is not None:
                                if _reception_score(best) > _reception_score(ongoing) + 20:
                                    triggered = best
                                else:
                                    triggered = ongoing
                            else:
                                triggered = best

                        if triggered is not None and not _same_pass(state.mode_pass[mode], triggered):
                            if state.mode_pass[mode] is not None:
                                # Direct switch between overlapping passes of
                                # the same family: close out the old pass
                                # (log + decode) before moving on
                                with state.signal_lock:
                                    finished.append((state.mode_pass[mode],
                                                     state.mode_pass_peak[mode]))
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
                            state.mode_pass[mode] = triggered
                            state.mode_freq[mode] = tune
                            state.mode_sat_name[mode] = triggered["sat_name"]
                            with state.signal_lock:
                                state.mode_pass_peak[mode] = 0.0
                            local_rise = triggered["rise_utc"] + timedelta(hours=UTC_OFFSET)
                            state.log_console(f"🔴 {mode.upper()} PASS START: {triggered['sat_name']} {round(tune/1e6,4)} MHz, max {triggered['max_alt']:.0f}° at {local_rise.strftime('%H:%M')}")
                            if fb_note:
                                state.log_console(f"🛰 {fb_note}")
                            started.append(mode)
                        elif triggered is None and state.mode_pass[mode] is not None:
                            fp = state.mode_pass[mode]
                            local_set = fp["set_utc"] + timedelta(hours=UTC_OFFSET)
                            state.log_console(f"✅ {mode.upper()} PASS END: {fp['sat_name']} finished at {local_set.strftime('%H:%M')}")
                            with state.signal_lock:
                                finished.append((fp, state.mode_pass_peak[mode]))
                                state.mode_pass_peak[mode] = 0.0
                            state.mode_pass[mode] = None
                            # Return the track to its idle park frequency
                            state.mode_freq[mode] = MODE_PARK_HZ[mode]
                            state.mode_sat_name[mode] = "idle"
                            ended.append(mode)

                    # Legacy single-track summary (see the module docstring):
                    # the Meteor track when active, else the ISS track, else
                    # idle; a manual station tune always wins.
                    summary = next((m for m in state.SAT_MODES
                                    if state.mode_pass[m] is not None), None)
                    if manual is not None:
                        state.current_frequency = manual
                        state.current_sat_name = f"Manual {manual/1e6:.4f} MHz"
                        state.current_pass = None
                        state.is_pass_active = False
                    elif summary is not None:
                        state.current_pass = state.mode_pass[summary]
                        state.current_frequency = state.mode_freq[summary]
                        state.current_sat_name = state.mode_sat_name[summary]
                        state.is_pass_active = True
                    else:
                        state.current_pass = None
                        state.is_pass_active = False
                        state.current_frequency = METEOR_LRPT_HZ
                        state.current_sat_name = "idle"

                # Auto-join (the checkbox on the dongle card): dongles in a
                # dedicated decode role with auto-join enabled leave that
                # role for a pass OF THEIR SAT MODE and return to it after
                # set. Called OUTSIDE status_lock — the join/leave take it
                # themselves.
                for mode in started:
                    radio.auto_sat_join_all(mode)
                for mode in ended:
                    radio.auto_sat_leave_all(mode)

                # Doppler correction per active track: range-rate from the
                # same TLEs, applied in software by the capture threads of
                # the dongles tracking that mode (steps stay < ~1 kHz
                # between ticks even on ISS passes)
                with state.status_lock:
                    active_tracks = {m: state.mode_pass[m] for m in state.SAT_MODES}
                for mode, cur in active_tracks.items():
                    if cur is not None and HAS_SKYFIELD and cur.get("sat") is not None:
                        try:
                            dop = _doppler_hz(cur["sat"], cur["frequency"])
                            with state.status_lock:
                                state.mode_doppler_hz[mode] = dop
                                state.mode_doppler_freq_hz[mode] = cur["frequency"]
                        except Exception as e:
                            state.log_console(f"Doppler computation failed: {e}", "error")
                    elif state.mode_doppler_hz[mode]:
                        with state.status_lock:
                            state.mode_doppler_hz[mode] = 0
                            state.mode_doppler_freq_hz[mode] = 0
                # Mirror the summary track's Doppler + signal peak into the
                # legacy globals (status.json / dashboard header)
                with state.status_lock:
                    if manual is not None:
                        state.doppler_hz = 0
                        state.doppler_freq_hz = 0
                    elif summary is not None:
                        state.doppler_hz = state.mode_doppler_hz[summary]
                        state.doppler_freq_hz = state.mode_doppler_freq_hz[summary]
                        state.pass_signal_peak = state.mode_pass_peak[summary]
                    else:
                        state.doppler_hz = 0
                        state.doppler_freq_hz = 0
                        state.pass_signal_peak = 0.0

                for fp, peak in finished:
                    _finish_pass(fp, peak)

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
