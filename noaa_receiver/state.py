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
