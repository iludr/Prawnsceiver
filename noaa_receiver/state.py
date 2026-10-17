"""Shared mutable state, locks, and ring buffers.

Other modules access this state as `state.foo` (attribute access on this
module object), which avoids `global` declarations entirely.
"""
import threading
from collections import deque
from datetime import datetime

from .config import WATERFALL_ROWS

# State
waterfall_buffer = deque(maxlen=WATERFALL_ROWS)
waterfall_lock = threading.Lock()
# Per-dongle state, keyed by dongle id "host:port" (a remote rtl_tcp
# server; see radio.py). Filled by radio.add_dongle. The primary dongle's
# entry aliases the legacy globals above; the others get their own buffers.
# All dongles tune the same frequency.
sdrs = {}
# Id ("host:port") of the primary dongle (live audio, WAV recording),
# decided once at startup from calibration.PRIMARY_DONGLE.
primary_dongle = None
signal_strength = 0.0
pass_signal_peak = 0.0
signal_lock = threading.Lock()
signal_history = deque(maxlen=300)  # 5 min at 1 Hz

# Live audio (FM demodulated): every dongle entry owns a ring
# {'data': [...], 'base': int, 'total': int, 'cond': Condition} created by
# radio.add_dongle, fed by its capture thread and served by
# /live.wav?d=<dongle id>.
LIVE_AUDIO_CHUNKS = 2048  # ~4s of live audio retained

def push_live_audio(entry, pcm):
    """Append a demodulated PCM block to a dongle entry's live audio ring."""
    ring = entry['la']
    with ring['cond']:
        ring['data'].append(pcm)
        ring['total'] += 1
        if len(ring['data']) > LIVE_AUDIO_CHUNKS:
            drop = len(ring['data']) - LIVE_AUDIO_CHUNKS
            del ring['data'][:drop]
            ring['base'] += drop
        ring['cond'].notify_all()

# Debug console ring buffer (served at /console)
console_buffer = deque(maxlen=100)
console_lock = threading.Lock()

def log_console(msg, level="info"):
    """Log a message to the /console debug page (and stdout)."""
    line = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "msg": str(msg),
    }
    with console_lock:
        console_buffer.append(line)
    line_str = f"[{line['time']}] [{level}] {msg}"
    try:
        print(line_str)
    except UnicodeEncodeError:
        # Console can't render the message (e.g. emoji on a non-UTF-8 terminal)
        print(line_str.encode('ascii', 'backslashreplace').decode('ascii'))
# Manual tune (FM radio test): while set, the scheduler must not retune or
# record - the operator controls the frequency from the dashboard.
manual_frequency = None
# Per-dongle manual frequency override (dongle id -> Hz): a dongle with an
# entry tunes there instead of the shared current_frequency (e.g. to compare
# receive quality at a slightly different center). Cleared per dongle.
manual_dongle_freq = {}
# Per-dongle manual demod bandwidth override (dongle id -> Hz, the IQ low-pass
# cutoff ahead of the FM discriminator): defines what ends up in the WAV and
# the live audio. None/auto = mode default (full band on broadcast FM,
# 22 kHz on satellite modes). Cleared per dongle.
manual_dongle_bw = {}
# Per-dongle manual demod mode override (dongle id -> 'am'/'usb'/
# 'lsb'/'cw'/'fm'): what the capture thread demodulates into the
# live audio and WAV. No entry = auto: FM everywhere except the
# airband voice segment (118-137 MHz), which demodulates AM.
manual_dongle_mode = {}
# Manual recording (dashboard Record button on a dongle card, dongle id ->
# True): while set, that dongle's capture thread records its current tune
# to WAV outside passes too. Named after the tune ("Manual_<freq>_MHz"), so
# the file is never attributed to a satellite pass. Cleared per dongle.
manual_recording = {}
# Live Doppler correction for the active pass's satellite: computed by the
# scheduler (range-rate via skyfield), applied in software by the capture
# threads. doppler_freq_hz is the satellite's nominal frequency the value
# applies to; doppler_hz is 0 outside passes.
doppler_hz = 0
doppler_freq_hz = 0
current_frequency = 137912500  # idle park: the Meteor LRPT band
current_sat_name = "idle"
is_recording = False
is_pass_active = False
# Global recording pause (dashboard switch): while set, the capture threads
# do not open WAV/IQ files during passes — an already-running WAV is closed
# within one IQ block. Tracking, waterfall, Doppler and live audio keep
# running, so reception quality can be judged without collecting garbage.
recordings_paused = False
# Per-dongle frequency scan (dongle id -> state dict, see scan.py): set while a
# scan sweeps a dongle's override and after it parks on a find, so the
# dashboard can show live progress and the result.
scans = {}
current_wav = None
current_wav_path = None
# Bases (recording filename without extension) with a decode running
# right now - synchronous decodes (SSTV) and background SatDump runs
# register here (see decoding/decode.py). The recording archiver
# (archive.py) never moves a family while its base is in this set.
decodes_active = set()
upcoming_passes = []
current_pass = None
last_tle_refresh = 0
# Manual TLE sync requested via /sync_tle: the scheduler's outer loop picks
# this up within its 10 s tick and refetches + re-predicts passes
tle_sync_requested = False
# TLE fetch progress, exposed via status.json for the dashboard progress bar
tle_progress = {"active": False, "done": 0, "total": 0, "current": None}
status_lock = threading.Lock()

# AIS ship traffic (Danube vessels; see noaa_receiver/ais.py): filled by
# the AIS capture thread when a dongle is dedicated via
# calibration.AIS_DONGLE. ais_ships maps MMSI -> ship dict (name,
# position, speed, course, ... last_seen); ais_nmea keeps the most recent
# raw !AIVDM sentences for the dashboard's raw feed.
ais_ships = {}
# Every ship EVER received (persistent across restarts, saved to
# AIS_SHIPS_FILE by ais.py; each entry carries the last
# AIS_RECENT_MSGS decoded messages in "recent"). Never pruned.
ais_ships_all = {}
ais_lock = threading.Lock()
ais_nmea = deque(maxlen=60)
ais_channels = {}    # 'A'/'B' -> per-channel stats (frames, bad, floor)
ais_enabled = False
ais_dongle = None

# Orbcomm satellite traffic (see noaa_receiver/decoding/orbcomm.py): a dongle
# switched to the Orbcomm mode tunes to config.ORBCOMM_CENTER_HZ and
# decodes every satellite it finds in the 137.0-138.0 MHz band.
# orbcomm_channels maps a per-dongle channel key -> stats dict (frequency,
# Doppler, packet counters, satellite ID and position from its Sync/
# Ephemeris packets); orbcomm_packets keeps the most recent decoded
# packets for the dashboard's live feed.
orbcomm_lock = threading.Lock()
orbcomm_enabled = False
orbcomm_dongle = None
orbcomm_channels = {}
orbcomm_packets = deque(maxlen=60)

# APRS ham packet traffic (see noaa_receiver/decoding/aprs.py): a dongle
# switched to the APRS role tunes to config.APRS_FREQ_HZ (144.800 MHz),
# or to config.ISS_APRS_FREQ_HZ (145.825 MHz, the ISS digipeater) when
# its aprs_iss flag is set, and decodes Bell 202 AFSK AX.25 UI frames.
# aprs_stations maps callsign -> newest info (this-run registry);
# aprs_stations_all is the persistent every-station-ever registry
# (aprs_stations table); aprs_packets keeps the most recent frames
# for the dashboard's live feed; aprs_freq_hz is the frequency the
# running demodulator is tuned to.
aprs_lock = threading.Lock()
aprs_enabled = False
aprs_dongle = None
aprs_freq_hz = 0
aprs_stations = {}
aprs_stations_all = {}
aprs_packets = deque(maxlen=60)
aprs_stats = {}       # frames / bad / floor of the running demodulator

# POCSAG paging (see noaa_receiver/decoding/pocsag.py): a dongle in
# the POCSAG role tunes to config.PAGING_CENTER_HZ and decodes every
# paging channel in the 2.4 MHz capture. pocsag_channels maps kHz ->
# live channel status, pocsag_pages keeps the most recent decoded
# pages (the newest 100 are restored from the pocsag_messages log at
# startup).
pocsag_lock = threading.Lock()
pocsag_enabled = False
pocsag_dongle = None
pocsag_channels = {}
pocsag_pages = deque(maxlen=100)

# ADS-B aircraft traffic (see noaa_receiver/decoding/adsb.py): a dongle
# in the ADS-B role tunes to 1090 MHz and decodes Mode S extended
# squitters. adsb_aircrafts maps ICAO hex -> aircraft dict (callsign,
# position, altitude, speed, ...); adsb_aircrafts_all is the persistent
# every-aircraft-ever registry (adsb_aircrafts table); adsb_raw keeps
# the most recent frame hex strings for the raw feed.
adsb_lock = threading.Lock()
adsb_enabled = False
adsb_dongle = None
adsb_aircrafts = {}
adsb_aircrafts_all = {}
adsb_raw = deque(maxlen=60)
adsb_stats = {}       # frames / bad / floor of the running demodulator
