"""Static configuration for the Meteor LRPT / ISS SSTV receiver.

Dongle-specific calibration (primary dongle, per-dongle tuning corrections)
lives in calibration.py, separate from this system config.
"""
import os

PORT = 8085
# LOGDIR holds station.db (the SQLite catalog of all persistent state:
# passes, dongles, TLEs, decode markers, AIS ships/messages - see
# noaa_receiver/db.py) plus the LEGACY JSON stores below, which are
# imported into the database once at startup and then retired to
# <name>.migrated
LOGDIR = "/var/log/noaa"
RECORD_DIR = "/var/log/noaa/recordings"
PASS_HISTORY_FILE = os.path.join(LOGDIR, "pass_history.json")
# Dongles added at runtime (dashboard "Add dongle") persist across restarts
# as a JSON list of {"host": ..., "port": ...} entries
DONGLES_FILE = os.path.join(LOGDIR, "dongles.json")
# The dashboard (index.html, images) is served straight from the project
# directory - the receiver is no longer tied to one deployment host
WEBDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFT_SIZE = 512
WATERFALL_ROWS = 120
# Gain: 0 = auto (RTL-SDR AGC), or fixed dB like 35. Auto adapts to signal
# strength during passes, which is better than a fixed value.
SDR_GAIN = 0  # auto-gain
SDR_RATE = 2400000
# Offset tuning: rtl_sdr tunes SDR_OFFSET_HZ above the target frequency and the
# signal is shifted back to baseband in software. This moves the dongle's
# inherent center DC spike off the satellite signal (it would otherwise sit
# exactly on the satellite signal).
SDR_OFFSET_HZ = 60000
AUDIO_RATE = 48000
IQ_BLOCK = FFT_SIZE * 2
DECIMATION = SDR_RATE // AUDIO_RATE

# Regensburg coordinates
LAT, LON = 49.013, 12.099
UTC_OFFSET = 2  # Germany UTC+2

# Tracked satellites: catalog number -> (name, frequency_hz)
# NOAA 15/18/19 (POES) are NOT tracked: the constellation was
# decommissioned Jun-Aug 2025 (NOAA-15 last, 2025-08-19) - APT and DSB
# transmitters are off for good, so passes would be dead spectrum.
# NOAA 15/18/19 (POES) are NOT tracked: the constellation was
# decommissioned Jun-Aug 2025 (NOAA-15 last, 2025-08-19) - APT and DSB
# transmitters are off for good, so passes would be dead spectrum.
# ISS: ARISS SSTV (Robot 36) on 437.550 MHz during active events
# Meteor-M 2-3/2-4: Russian weather satellites, LRPT digital downlink
# (137.9125 MHz family per SatNOGS; M2-3 was wrongly tracked at
# 137.1 — ~800 kHz below its LRPT, so every M2-3 recording tuned dead
# spectrum. A tune anywhere in 137.86-137.96 captures the LRPT band:
# the recording spans +/-480 kHz and the decode measures the actual
# signal position). Decoded from the raw IQ with SatDump (meteor_m2-x_lrpt,
# see decode.py) - the FM-demod audio cannot. Both Meteor LRPT transmitters have been
# dark in every pass received so far (verified 2026-10-10: no satellite
# signal in any Meteor IQ recording, just local land-mobile carriers).
TRACKED_SATS = {
    25544: ("ISS (Zarya)", 437550000),
    57166: ("Meteor-M 2-3", 137912500),
    59051: ("Meteor-M 2-4", 137912500),
}


# Passes on these frequencies also record the RAW IQ stream (u8 complex,
# 2.4 MHz -> ~4.8 MB/s per dongle) next to the demod audio WAV:
# Meteor LRPT is a digital mode the FM-demod audio cannot carry — decoding
# (SatDump) needs the baseband. SSTV passes stay audio-only.
IQ_RECORD_FREQS = {
    137912500,    # Meteor-M 2-3 / 2-4 LRPT
}

# ISS only transmits SSTV during ARISS events; outside events its passes would
# be recorded as empty WAVs (~350 MB/day). Set False to track ISS in the pass
# list without recording it.
RECORD_ISS = True

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 60  # Start recording 60s before rise, stop 60s after set
TLE_REFRESH_HOURS = 3  # Refresh TLE data every 3h
TLE_CACHE_FILE = os.path.join(LOGDIR, "tle_cache.json")  # last good TLEs, used when Celestrak is unreachable
# Celestrak rejects requests with generic bot user-agents (HTTP 403)
TLE_USER_AGENT = "NOAAh-CrabArk/1.0 (amateur weather-satellite ground station; https://github.com/Derusi/Prawnsceiver)"

# Doppler correction: a satellite's carrier drifts by up to ±3 kHz (Meteor,
# 137 MHz) / ±10 kHz (ISS, 437 MHz) over a pass. The scheduler computes the
# live range-rate Doppler during passes; capture threads rotate the baseband
# in software (rtl_sdr cannot be retuned without restarting it, which would
# gap the recording). Applied only when a dongle's target is within
# DOPPLER_APPLY_RANGE_HZ of the tracked satellite frequency.
DOPPLER_APPLY_RANGE_HZ = 500_000
MANUAL_TUNE_LOCKOUT_MINS = 5  # /tune rejected this close to a predicted pass
DOPPLER_UPDATE_SECS = 10  # scheduler tick cadence; steps stay < ~1 kHz on ISS

# AIS (ship traffic on the Danube; see noaa_receiver/ais.py): both AIS
# channels (A: 161.975 MHz, B: 162.025 MHz) sit at +/-25 kHz around this
# center, inside one 2.4 MHz capture - a dongle dedicated to AIS (pinned
# by calibration.AIS_DONGLE) demodulates both from the same IQ stream
# and is excluded from satellite tracking.
AIS_CENTER_HZ = 162000000
AIS_CHANNEL_HZ = (161975000, 162025000)
# Ships stay on the traffic table / map for a full hour after their
# last transmission (map fades ships not heard for 10+ min)
AIS_SHIP_TTL_SECS = 3600
# Persistent per-frame message log for the AIS tracking page (jsonl):
# always keeps the last AIS_LOG_KEEP_HOURS of decoded frames (trimmed
# in ais.py, atomically)
AIS_LOG_FILE = os.path.join(LOGDIR, "ais_log.jsonl")
AIS_LOG_KEEP_HOURS = 24

# APRS (amateur packet radio; see noaa_receiver/decoding/aprs.py): the
# single European simplex channel - Bell 202 AFSK (1200/2200 Hz) at
# 1200 baud on narrowband FM, AX.25 UI frames. One dongle role like
# AIS/Orbcomm, switched from the dashboard.
APRS_FREQ_HZ = 144800000
# The ISS (Zarya) APRS digipeater: same Bell 202 AFSK/AX.25, reachable
# during passes. A per-dongle checkbox in APRS mode retunes the role
# from APRS_FREQ_HZ to this frequency (see decoding/aprs.py).
ISS_APRS_FREQ_HZ = 145825000
# Rolling decoded-frame log (aprs_messages table), trimmed by ts
APRS_LOG_KEEP_HOURS = 24
# Persistent registry of EVERY ship ever received (one JSON file), each
# with its latest data plus the last AIS_RECENT_MSGS decoded messages.
# Rewritten at most every AIS_SHIPS_SAVE_SECS (see ais.py).
AIS_SHIPS_FILE = os.path.join(LOGDIR, "ais_ships_persist.json")
AIS_RECENT_MSGS = 10
AIS_SHIPS_SAVE_SECS = 60

# Orbcomm (satellite IoT downlink; see noaa_receiver/decoding/orbcomm.py): the
# satellites transmit a continuous 4800 bps SDPSK stream somewhere in
# 137.0-138.0 MHz (2.5 kHz raster + up to +/-2.5 kHz Doppler) while
# above the horizon. One 2.4 MHz capture centered here covers the whole
# band, and the receiver finds each active satellite's channel in the
# spectrum, so no fixed channel list is needed.
ORBCOMM_CENTER_HZ = 137500000
ORBCOMM_BAND_HZ = (137000000, 138000000)
ORBCOMM_SYMRATE = 4800
# Channels whose carrier disappears for this long are dropped (a pass
# has ended, or the cluster was noise)
ORBCOMM_CHANNEL_TIMEOUT_SECS = 10
# Rolling decoded-packet log (orbcomm_messages table), trimmed by ts
ORBCOMM_LOG_KEEP_HOURS = 24
