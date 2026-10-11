"""Static configuration for the NOAA APT / ISS SSTV receiver.

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
# SatDump (APT / DSB / LRPT decoder; apt-installed on the receiver):
# timeout for the synchronous APT decode of one recording. The map
# projection solve dominates (~2.5 min for a 12-min pass on the
# container, measured 2026-10-10). The digital LRPT/DSB decodes run
# detached with their own longer timeout in decode.py.
SATDUMP_TIMEOUT_SECS = 600
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
# exactly on the APT carrier).
SDR_OFFSET_HZ = 60000
AUDIO_RATE = 48000
IQ_BLOCK = FFT_SIZE * 2
DECIMATION = SDR_RATE // AUDIO_RATE

# Regensburg coordinates
LAT, LON = 49.013, 12.099
UTC_OFFSET = 2  # Germany UTC+2

# Tracked satellites: catalog number -> (name, frequency_hz)
# ISS: ARISS SSTV (Robot 36) on 437.550 MHz during active events
# Meteor-M 2-3/2-4: Russian weather satellites, LRPT digital downlink
# (137.9125 MHz family per SatNOGS; M2-3 was wrongly tracked at
# 137.1 — ~800 kHz below its LRPT, so every M2-3 recording tuned dead
# spectrum. A tune anywhere in 137.86-137.96 captures the LRPT band:
# the recording spans +/-480 kHz and the decode measures the actual
# signal position). Decoded from the raw IQ with SatDump (meteor_m2-x_lrpt,
# see decode.py) - the APT pipeline cannot. Both Meteor LRPT transmitters have been
# dark in every pass received so far (verified 2026-10-10: no satellite
# signal in any Meteor IQ recording, just local land-mobile carriers).
TRACKED_SATS = {
    25338: ("NOAA 15", 137620000),
    28654: ("NOAA 18", 137912500),
    33591: ("NOAA 19", 137100000),
    25544: ("ISS (Zarya)", 437550000),
    57166: ("Meteor-M 2-3", 137912500),
    59051: ("Meteor-M 2-4", 137912500),
}

# Satellites whose APT transmitter is off (NOAA 18 and NOAA 19 confirmed dark
# Oct 2026; SatNOGS flags their APT 'inactive') are received via their DSB
# (Direct Sounder Broadcast) instead: a public narrowband digital instrument
# data downlink in the same VHF band. Passes of these satellites tune and
# record at the DSB frequency; decode refuses (not an image signal). Remove
# an entry to return a satellite to its APT frequency.
SAT_DSB_FREQ = {
    28654: 137350000,   # NOAA 18 DSB
    33591: 137770000,   # NOAA 19 DSB
}
# Auto demod width on DSB receive frequencies: the DSB stream is a narrow
# (~2-3 kHz) digital signal, so auto records 6 kHz instead of the 22 kHz
# APT default (manual per-dongle overrides still win)
SAT_DSB_DEMOD_BW_HZ = 6000

# Passes on these frequencies also record the RAW IQ stream (u8 complex,
# 2.4 MHz -> ~4.8 MB/s per dongle) next to the demod audio WAV: DSB and
# Meteor LRPT are digital modes the FM-demod audio cannot carry — decoding
# (SatDump) needs the baseband. APT/SSTV passes stay audio-only.
IQ_RECORD_FREQS = {
    137350000,    # NOAA 18 DSB
    137770000,    # NOAA 19 DSB
    137100000,    # Meteor-M 2-3 LRPT
    137912500,    # Meteor-M 2-4 LRPT
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
TLE_USER_AGENT = "NOAAh-CrabArk/1.0 (amateur NOAA APT ground station; https://github.com/Derusi/Prawnsceiver)"

# Doppler correction: a satellite's carrier drifts by up to ±3 kHz (NOAA,
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
# Persistent registry of EVERY ship ever received (one JSON file), each
# with its latest data plus the last AIS_RECENT_MSGS decoded messages.
# Rewritten at most every AIS_SHIPS_SAVE_SECS (see ais.py).
AIS_SHIPS_FILE = os.path.join(LOGDIR, "ais_ships_persist.json")
AIS_RECENT_MSGS = 10
AIS_SHIPS_SAVE_SECS = 60
