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
# Raw recordings (WAV + IQ) are ALWAYS captured to the LOCAL disk first
# (RECORD_LOCAL_DIR): a network share can vanish mid-pass and kill the
# recording with it (the 2026-10-17 outage), and a SQLite-grade stall on
# the share must never sit under a 4.8 MB/s IQ stream. When
# PRAWN_RECORD_DIR points elsewhere (recommended: NFS, see AGENTS.md
# deployment notes - e.g. a systemd unit
# 'Environment=PRAWN_RECORD_DIR=/mnt/nfs/prawnceiver/recordings'), the
# archiver thread (noaa_receiver/archive.py) moves every finished,
# decoded recording family over once the share is really mounted; the
# dashboard/history pages serve both locations transparently. Without
# PRAWN_RECORD_DIR the archive IS the local dir: a single-directory
# setup where nothing ever moves (the notebook field station).
# PRAWN_RECORD_LOCAL_DIR can override the capture dir itself (e.g. to a
# second local disk).
RECORD_LOCAL_DIR = os.environ.get("PRAWN_RECORD_LOCAL_DIR") or "/var/log/noaa/recordings"
RECORD_DIR = os.environ.get("PRAWN_RECORD_DIR") or RECORD_LOCAL_DIR
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
# Meteor-M 2-3/2-4: Russian weather satellites, LRPT digital downlink.
# SatNOGS lists 137.9125 MHz, but the actual downlink is ~137.896-137.900 MHz
# (137.9000 alternate channel entry); the station previously tuned to 137.9125,
# placing the true signal at ~-72-76 kHz in baseband (outside the expected
# -60 kHz). A tune anywhere in 137.86-137.96 MHz captures the LRPT band:
# the recording spans +/-480 kHz and the decode measures the actual signal
# position. Decoded from the raw IQ with SatDump (meteor_m2-x_lrpt) -
# the FM-demod audio cannot.
METEOR_LRPT_HZ = 137_900_000   # actual downlink ~137.896-137.900 MHz
ISS_SSTV_HZ = 437_550_000      # ARISS SSTV (Robot 36) downlink
TRACKED_SATS = {
    25544: ("ISS (Zarya)", ISS_SSTV_HZ),
    57166: ("Meteor-M 2-3", METEOR_LRPT_HZ),
    59051: ("Meteor-M 2-4", METEOR_LRPT_HZ),
}


# Passes on these frequencies also record the RAW IQ stream (u8 complex,
# 2.4 MHz -> ~4.8 MB/s per dongle) next to the demod audio WAV:
# - Meteor LRPT is a digital mode the FM-demod audio cannot carry —
#   decoding (SatDump) needs the baseband. The IQ is deleted after a
#   successful decode (see decoding/decode.py).
# - ISS SSTV decodes from the audio WAV, but its raw IQ is recorded AND
#   KEPT — never deleted after a decode (decoding/decode._keep_raw_iq):
#   the baseband is the fallback when the SSTV decode has issues, and it
#   feeds the pass waterfall diagram (operator request 2026-10-23).
#   Costs ~3 GB per full pass at 2.4 Msps.
IQ_RECORD_FREQS = {
    METEOR_LRPT_HZ,    # Meteor-M 2-3 / 2-4 LRPT
    ISS_SSTV_HZ,       # ISS (Zarya) ARISS SSTV — kept for decode retries
}

# Site-specific interferer: a persistent land-mobile carrier at ~137.905 MHz
# (Region-1, 20-40x the noise floor) sits ~5-9 kHz above the LRPT downlink,
# INSIDE the ~72 kHz LRPT band. decode._measure_signal_offset notches it out
# of the baseband spectrum so it cannot skew the signal-position centroid.
# The baseband position is DERIVED from the tune (the dongle sits
# SDR_OFFSET_HZ above the target, so an absolute frequency f lands at
# f - (target + SDR_OFFSET_HZ)) - a retune cannot silently misplace the
# notch. With the 137.9000 tune the carrier sits at -55 kHz, inside the band.
LOCAL_CARRIER_NOTCH_HZ = 137_905_000     # absolute carrier frequency
LOCAL_CARRIER_BASEBAND_HZ = LOCAL_CARRIER_NOTCH_HZ - (METEOR_LRPT_HZ + SDR_OFFSET_HZ)

# Manual raw-IQ recording (dashboard Record button, see radio.py):
# the UNTOUCHED uint8 IQ stream of the current tune is captured for
# at most this many seconds, then finalized into a zip (raw IQ +
# description.json + waterfall.png) that the dashboard downloads
# automatically - sized so a forgotten recording cannot fill the
# disk (~4.8 MB/s at 2.4 Msps u8 IQ).
MANUAL_REC_MAX_SECS = 120

# ISS only transmits SSTV during ARISS events; outside events its passes would
# be recorded as empty WAVs (~350 MB/day). Set False to track ISS in the pass
# list without recording it.
RECORD_ISS = True

# Pass scheduling
PASS_MIN_ALT = 10.0  # Only care about passes above 10°
PASS_PREDICT_HOURS = 24  # Predict 24h ahead
PASS_MARGIN_SECS = 90  # Start recording 90s before rise, stop 90s after set
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
AIS_LOG_KEEP_HOURS = 72

# APRS (amateur packet radio; see noaa_receiver/decoding/aprs.py): the
# single European simplex channel - Bell 202 AFSK (1200/2200 Hz) at
# 1200 baud on narrowband FM, AX.25 UI frames. One dongle role like
# AIS/Orbcomm, switched from the dashboard.
APRS_FREQ_HZ = 144800000
# The ISS (Zarya) APRS digipeater: same Bell 202 AFSK/AX.25, reachable
# during passes. A per-dongle checkbox in APRS mode retunes the role
# from APRS_FREQ_HZ to this frequency (see decoding/aprs.py).
ISS_APRS_FREQ_HZ = 145825000
# LASARsat (Czech 1U CubeSat, OK0LSR, 510 km / 45 deg): an APRS
# digipeater on a DIFFERENT physical layer - GFSK 9600 bps AX.25 with
# G3RUH scrambling, not Bell 202 AFSK. The aprs_lasar checkbox retunes
# the APRS role here AND switches its demodulator to the 9600-baud
# chain (see decoding/aprs.py).
LASAR_FREQ_HZ = 436925000
# Rolling decoded-frame log (aprs_messages table), trimmed by ts
APRS_LOG_KEEP_HOURS = 72

# POCSAG paging (see noaa_receiver/decoding/pocsag.py): a dongle
# in the POCSAG role tunes here - one 2.4 MHz capture covers the
# whole local paging ecosystem (2026-10-16: idle-keyed channels
# from 465.69 to 466.25 MHz around a local site) and EVERY
# channel found is decoded in parallel.
PAGING_CENTER_HZ = 466_000_000
# Rolling decoded-page log (pocsag_messages table), trimmed by ts
POCSAG_LOG_KEEP_HOURS = 72
# Persistent registry of EVERY APRS station ever received
# (aprs_stations table, like the AIS ships registry), each with
# its latest data plus the last APRS_RECENT_MSGS decoded frames.
# Rewritten at most every APRS_STATIONS_SAVE_SECS (see aprs.py).
APRS_RECENT_MSGS = 10
APRS_STATIONS_SAVE_SECS = 60
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
ORBCOMM_LOG_KEEP_HOURS = 72

# ADS-B (aircraft squitters; see noaa_receiver/decoding/adsb.py): a
# dongle in the ADS-B role tunes to 1090 MHz and decodes Mode S
# extended squitters — identification, airborne/surface position
# (compact position reporting) and velocity. One dongle role like
# AIS/POCSAG, switched from the dashboard.
ADSB_CENTER_HZ = 1_090_000_000
# Rolling decoded-message log (adsb_messages table), trimmed by ts
ADSB_LOG_KEEP_HOURS = 72
# Persistent registry of every aircraft ever received (adsb_aircrafts
# table, like the AIS ships registry), each with its latest data plus
# the last ADSB_RECENT_MSGS decoded frames. Rewritten at most every
# ADSB_AIRCRAFTS_SAVE_SECS.
ADSB_RECENT_MSGS = 20
ADSB_AIRCRAFTS_SAVE_SECS = 60
# Aircraft stay on the live table this long after their last squitter
# (planes move fast; 10 min covers a whole overhead pass)
ADSB_AIRCRAFT_TTL_SECS = 600
# The registry page lists aircraft heard within this window. The
# adsb_aircrafts table (and the in-memory mirror restored from it at
# startup) keeps every aircraft ever — older ones just leave the page.
# 7 days (operator request 2026-10-25: more history to browse; the data
# is kept forever regardless, this is only the page window).
ADSB_REGISTRY_WINDOW_SECS = 7 * 24 * 3600
# Coverage hull plausibility gate: a ground station hears ADS-B to the
# radio horizon (~400 km for a plane at 40 kft), so a decoded position
# farther than this from the station is a bad CPR decode, not coverage
ADSB_COVERAGE_MAX_KM = 600

# Battery status (noaa_receiver/power.py): the dashboard's power
# indicator merges the receiver host's own batteries
# (/sys/class/power_supply — the notebook case) with the PiSugar HAT
# battery of the TCP dongle hosts — the Raspberry Pi running
# prawntenna. Its pisugar-server exposes a standalone WebSocket API on
# 8422 and a raw TCP command API on 8423 (docs.pisugar.com); both speak
# the same line commands ("get battery" -> "battery: 85"). Every TCP
# dongle host is probed in the background (power_refresh_thread).
PISUGAR_WS_PORT = 8422
PISUGAR_TCP_PORT = 8423
PISUGAR_PROBE_TIMEOUT = 2.0   # per transport; a dead host fails fast
POWER_REFRESH_SECS = 15       # background probe cadence (matches the WS push)
