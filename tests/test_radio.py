"""Integration test: the real sdr_capture_thread against a fake rtl_tcp server.

Drives the recording state machine (pass start/end, same-frequency pass
switch, band switch mid-pass, per-dongle override, WAV open failure,
reconnect after a dropped stream) and checks the rtl_tcp commands and WAV
files it produces. The fake server speaks the real rtl_tcp protocol: a
12-byte RTL0 handshake followed by an endless IQ stream, recording the
5-byte control commands it receives.

Run: python3 -u tests/test_radio.py (needs numpy; ~20 s; uses TCP port 1299)
"""
import glob, json, socket, struct, tempfile, threading, time, wave
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Regression guard (2026-10-08: an import-line edit dropped SDR_DONGLE_GAIN
# and killed every satellite capture thread at runtime, unnoticed until
# deploy): every name the capture threads reference at module level must
# resolve. Updated 2026-10-09 for the network-dongle architecture.
import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
import noaa_receiver.sdr.radio as _radio
for _n in ("AIS_DONGLE", "FM_BAND", "PRIMARY_DONGLE", "DEFAULT_DONGLES",
            "SDR_DONGLE_GAIN", "correction_info", "tuning_correction",
            "add_dongle", "remove_dongle", "sdr_thread", "set_dongle_ais"):
    assert hasattr(_radio, _n), f"radio.py is missing {_n} — check the calibration/config imports"
print("radio namespace guard: ok")
import numpy as np
from collections import deque
from noaa_receiver.sdr import radio
from noaa_receiver import state
from noaa_receiver.config import AIS_CENTER_HZ, METEOR_LRPT_HZ, SDR_OFFSET_HZ, SDR_RATE, WATERFALL_ROWS
from noaa_receiver.calibration import tuning_correction

tmp = tempfile.mkdtemp(prefix='prawn_')
radio.LOGDIR = os.path.join(tmp, 'log'); radio.RECORD_LOCAL_DIR = os.path.join(tmp, 'rec')
from noaa_receiver import db   # dongle persistence lives in the station database
db.query("SELECT 1")           # schema ready against the isolated test DB
radio.WAV_RETRY_SECS = 0.5
HOST, PORT = '127.0.0.1', 1299
DID = f'{HOST}:{PORT}'

# ---- fake rtl_tcp: header + endless IQ, records control commands ----
commands = []
class FakeRtlTcp(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, PORT)); self.srv.listen(1)
        self.conn = None; self.sessions = 0
    def run(self):
        rng = np.random.default_rng(0)
        while True:
            conn, _ = self.srv.accept()
            self.sessions += 1; self.conn = conn
            try:
                conn.sendall(b'RTL0' + struct.pack('!II', 5, 29))
                conn.settimeout(0.01)
                chunk = rng.integers(100, 156, 65536, dtype=np.uint8).tobytes()
                while True:
                    conn.sendall(chunk)
                    try:
                        cmd = conn.recv(5)
                        if not cmd: break
                        while len(cmd) < 5:
                            more = conn.recv(5 - len(cmd))
                            if not more: break
                            cmd += more
                        commands.append(struct.unpack('!BI', cmd))
                    except socket.timeout:
                        pass
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                conn.close(); self.conn = None
    def drop_client(self):
        """Close the current client connection (simulate a network drop)."""
        if self.conn is not None:
            try: self.conn.close()
            except OSError: pass
srv = FakeRtlTcp(); srv.start()

import traceback
def _hook(*a):
    traceback.print_exception(*a)
    os._exit(1)
sys.excepthook = _hook

# ---- unit: dongle registry ----
try:
    radio.add_dongle('', 0)
    raise AssertionError('empty host must raise')
except ValueError:
    pass
try:
    radio.add_dongle('10.0.0.1', 'notaport')
    raise AssertionError('bad port must raise')
except ValueError:
    pass
did, created = radio.add_dongle(HOST, PORT)
assert did == DID and created, (did, created)
did2, created2 = radio.add_dongle(HOST, PORT)      # idempotent
assert did2 == DID and not created2
assert state.primary_dongle == DID                  # first non-AIS dongle becomes primary
assert [dict(r) for r in db.query("SELECT host, port, ais FROM dongles")] == [{"host": HOST, "port": PORT, "ais": 0}]
print("dongle registry: validation, idempotency, primary, persistence OK")

entry = state.sdrs[DID]
last_doppler = {'d': None}
real_doppler = radio.doppler_shift
def spy_doppler(c, d, fs, st):
    last_doppler['d'] = d; return real_doppler(c, d, fs, st)
radio.doppler_shift = spy_doppler

t = threading.Thread(target=radio.sdr_capture_thread, args=(DID,), daemon=True); t.start()

def wait_for(cond, secs=4.0, what=''):
    end = time.time() + secs
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    raise AssertionError(f'timeout waiting for {what}')

def set_sched(freq, sat, active):
    with state.status_lock:
        state.current_frequency = freq; state.current_sat_name = sat; state.is_pass_active = active

def expect_cmd(freq):
    want = (1, freq + SDR_OFFSET_HZ + tuning_correction(freq, DID))
    wait_for(lambda: want in commands, what=f'retune command {want}')
    commands.remove(want)

def wavs(): return sorted(glob.glob(os.path.join(radio.RECORD_LOCAL_DIR, '*.wav')))

# 1. idle streaming
wait_for(lambda: entry['last_data'] > 0 and len(entry['waterfall']) > 3, what='IQ flowing')
wait_for(lambda: entry['la']['total'] > 10, what='live audio')
assert entry['connected'] and not entry['is_recording']
expect_cmd(METEOR_LRPT_HZ)   # explicit tune on connect (state.current_frequency park)
want_rate = (2, SDR_RATE)
wait_for(lambda: want_rate in commands, what='sample-rate command on connect')
commands.remove(want_rate)
assert last_doppler['d'] == 0
print("1 idle: streaming, waterfall rows, live audio, no retune OK")

# 2. pass start Meteor-M 2-4 @137.1 + doppler
set_sched(137100000, "Meteor-M 2-4", True)
expect_cmd(137100000)
wait_for(lambda: entry['is_recording'] and 'Meteor-M_2-4_' in os.path.basename(entry['wav_path'] or ''), what='Meteor-M 2-4 wav')
assert state.is_recording and state.current_wav_path == entry['wav_path']
with state.status_lock:
    state.doppler_hz, state.doppler_freq_hz = 2000, 137100000
wait_for(lambda: last_doppler['d'] == 2000, what='doppler applied after live retune (was gated on the start frequency)')
print("2 pass start: retune, WAV open, Doppler applied on the retuned band OK")

# 3. same-frequency pass switch
first_wav = entry['wav_path']
set_sched(137100000, "Meteor-M 2-3", True)
wait_for(lambda: entry['wav_path'] and 'Meteor-M_2-3_' in os.path.basename(entry['wav_path']), what='Meteor wav')
time.sleep(0.2); assert not commands, f"no retune expected on same frequency, got {commands}"
with wave.open(first_wav) as w: assert w.getnframes() > 0 and w.getframerate() == 48000
print("3 same-frequency switch: WAV split, no retune, first WAV valid OK")

# 4. band switch mid-pass (overlapping pass) to ISS
set_sched(437550000, "ISS (Zarya)", True)
expect_cmd(437550000)
wait_for(lambda: entry['wav_path'] and 'ISS_(Zarya)_' in os.path.basename(entry['wav_path']), what='ISS wav')
with state.status_lock:
    state.doppler_hz, state.doppler_freq_hz = -9000, 437550000
wait_for(lambda: last_doppler['d'] == -9000, what='ISS doppler')
print("4 band switch mid-pass: WAV closed/reopened, retune, Doppler follows OK")

# 5. manual dongle override set and cleared during the pass
with state.status_lock: state.manual_dongle_freq[DID] = 100000000
expect_cmd(100000000)
wait_for(lambda: not entry['is_recording'], what='override stops recording')
wait_for(lambda: last_doppler['d'] == 0, what='doppler off on the override band')
with state.status_lock: state.manual_dongle_freq.pop(DID)
expect_cmd(437550000)
wait_for(lambda: entry['is_recording'], what='recording resumes after override cleared')
print("5 dongle override: retune out/in, recording paused/resumed OK")

# 6. pass end
set_sched(137620000, "idle", False)
expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'] and not state.is_recording and state.current_wav_path is None, what='pass end closes WAV')
for p in wavs():
    with wave.open(p) as w: assert w.getnframes() > 0, p
print(f"6 pass end: WAV closed, {len(wavs())} valid WAVs: {[os.path.basename(p) for p in wavs()]}")

# 6b. manual raw recording (dashboard Record button): captures the idle
# tune's UNTOUCHED uint8 IQ (no audio WAV any more), survives the global
# recording pause, and stops into the analysis zip (raw_iq.u8 +
# description.json + waterfall.png) that the dashboard auto-downloads
import zipfile as zf
with state.status_lock: state.manual_recording[DID] = True
wait_for(lambda: entry.get('manual_iq') is not None, what='manual raw recording starts while idle')
assert 'Manual_137.6200MHz' in entry['manual_meta']['base'], entry['manual_meta']['base']
assert not entry['is_recording'], "manual raw recording must not open an audio WAV"
with state.status_lock: state.recordings_paused = True      # pause must not stop it
time.sleep(0.5)
assert entry.get('manual_iq') is not None, "manual raw recording must survive the global pause"
with state.status_lock: state.recordings_paused = False
with state.status_lock: state.manual_recording.pop(DID, None)
wait_for(lambda: entry.get('manual_iq') is None and entry.get('manual_zip'),
         secs=8, what='manual recording finalizes into a zip')
mzip6b = entry['manual_zip']
with zf.ZipFile(os.path.join(radio.RECORD_LOCAL_DIR, 'manual', os.path.basename(mzip6b))) as z:
    names = z.namelist()
    assert 'raw_iq.u8' in names and 'description.json' in names, names
    assert 'analysis.json' in names, names        # pre-flight analysis
    desc = json.loads(z.read('description.json'))
    adesc = json.loads(z.read('analysis.json'))
    assert adesc['sample_rate_hz'] == SDR_RATE and adesc['emitters'] == [], adesc
    assert adesc['sparse_signals'] == [], adesc     # noise-only capture
    iqbytes = z.getinfo('raw_iq.u8').file_size
assert iqbytes > 0 and desc['format']['complex_samples'] == iqbytes // 2, (iqbytes, desc['format'])
assert desc['radio']['center_freq_hz'] == 137620000, desc['radio']
assert desc['format']['sample_rate_hz'] == SDR_RATE, desc['format']
assert 'waterfall.png' in names, names      # PIL present, rows collected
print("6b manual raw record: raw IQ + zip + description.json + waterfall OK")

# 6c. manual recording on a frequency override records the override band;
# the retune back finalizes it (a raw file must never span two tunes)
with state.status_lock:
    state.manual_dongle_freq[DID] = 100000000
    state.manual_recording[DID] = True
expect_cmd(100000000)
wait_for(lambda: entry.get('manual_meta') is not None
         and 'Manual_100.0000MHz' in entry['manual_meta']['base'],
         what='manual raw recording on the override band')
with state.status_lock:
    state.manual_dongle_freq.pop(DID, None)
    state.manual_recording.pop(DID, None)
expect_cmd(137620000)
wait_for(lambda: entry.get('manual_iq') is None, what='retune finalizes the manual recording')
wait_for(lambda: entry.get('manual_zip') not in (None, mzip6b),
         secs=8, what='override-band zip built')
print("6c manual raw record on a frequency override: records the override band, retune finalizes OK")

# 6d. the MANUAL_REC_MAX_SECS cap auto-finalizes a forgotten recording
_prev_cap = radio.MANUAL_REC_MAX_SECS
radio.MANUAL_REC_MAX_SECS = 2
_prev_zip = entry['manual_zip']
with state.status_lock: state.manual_recording[DID] = True
wait_for(lambda: entry.get('manual_iq') is not None, what='cap-test recording starts')
wait_for(lambda: DID not in state.manual_recording
         and entry.get('manual_zip') not in (None, _prev_zip),
         secs=10, what='cap auto-finalizes into a fresh zip')
radio.MANUAL_REC_MAX_SECS = _prev_cap
print("6d manual raw recording cap: auto-finalized at the limit OK")

# 6e. pre-flight analysis (analysis/iqscan.py): a synthetic OOK remote
# (fixed 31-bit code, gap-length bits), a steady carrier, the dongle's
# LO tone at the raw-stream center and a sparse radar-like pulse train
# in noise - the analyzer finds each, flags the LO spike, and pulls the
# sparse train out of the max-hold where the average sees nothing
from noaa_receiver.analysis.iqscan import analyze_recording
from noaa_receiver.decoding import pocsag
rng = np.random.default_rng(7)
n_synth = int(1.5 * SDR_RATE)                       # 1.5 s of IQ
tt = np.arange(n_synth) / SDR_RATE
synth = (rng.normal(0, 6, n_synth)
         + 1j * rng.normal(0, 6, n_synth)).astype(np.complex64)
def synth_tone(t0, dur, freq, amp=90.0):
    i0 = int(t0 * SDR_RATE); i1 = int((t0 + dur) * SDR_RATE)
    synth[i0:i1] += amp * np.exp(2j * np.pi * freq * tt[i0:i1])
CODE = "0011100110000000010101001111011"             # 31-bit fixed code
P, G0, G1, SYNC = 0.5e-3, 1.0e-3, 1.6e-3, 3.2e-3
for rep in range(3):                               # 3 repetitions
    tcur = 0.30 + rep * (31 * (P + G1) + SYNC)
    for bit in CODE:
        synth_tone(tcur, P, -66_200.0, amp=70.0)
        tcur += P + (G1 if bit == '1' else G0)
synth_tone(0.0, 1.0, 150_000.0, amp=60.0)           # steady carrier
synth_tone(0.0, 1.0, 0.0, amp=25.0)                # dongle LO tone (raw DC)
for k in range(5):                                 # sparse radar-like train
    synth_tone(0.05 + k * 0.2, 20e-6, 700_000.0, amp=75.0)
# POCSAG paging channel at raw +1 MHz: preamble + one batch
# with a numeric page - the pre-flight must decode it
poc_bits = '10' * 288
poc_words = [pocsag.IDLE] * 16
poc_words[0] = pocsag.encode_word(1, (123456 << 2) | 0)
poc_words[1] = pocsag.encode_word(0, 0x01234)
poc_words[2] = pocsag.IDLE
poc_bits += format(pocsag.SC, '032b')
for w32 in poc_words:
    poc_bits += format(w32, '032b')
poc_f = np.where(np.array([int(c) for c in poc_bits]) == 1,
                 2400.0, -2400.0)
poc_frep = np.repeat(poc_f, SDR_RATE // 1200)
poc_ph = (2 * np.pi * np.cumsum(poc_frep + 1_000_000.0)
          / SDR_RATE)
synth[:len(poc_frep)] += (60.0 * np.exp(1j * poc_ph)).astype(np.complex64)
ap_synth = os.path.join(radio.RECORD_LOCAL_DIR, 'synth.iq.u8')
u8 = np.empty(n_synth * 2, dtype=np.uint8)
u8[0::2] = (np.clip(synth.real, -127, 127) + 127.5).astype(np.uint8)
u8[1::2] = (np.clip(synth.imag, -127, 127) + 127.5).astype(np.uint8)
u8.tofile(ap_synth)
# raw-stream center = the TUNER (display center + offset tuning)
res = analyze_recording(ap_synth, 433_993_000, SDR_RATE)
em = res['emitters']
ook = [x for x in em if x['modulation'] and x['modulation']['type'] == 'ook']
assert ook and abs(ook[0]['offset_khz'] + 66.2) < 8, em
assert abs(ook[0]['abs_mhz'] - 433.9268) < 0.01, ook[0]   # tuner-centered
codes = ook[0]['modulation'].get('codes') or []
assert any(c['bits'] == CODE and c['count'] >= 2 for c in codes), codes
cont = [x for x in em if x['modulation'] and x['modulation']['type'] == 'continuous_carrier']
assert cont and any(abs(x['abs_mhz'] - 434.143) < 0.01 for x in cont), em
lo = [x for x in em if x.get('dc_or_lo_spike')]
assert lo and abs(lo[0]['offset_khz']) < 3, em      # LO tone at raw DC
poc = [x for x in em if 'pocsag' in x]
assert poc and any(p['addr'] == 123456 and p['text'] == '01234'
                   for p in poc[0]['pocsag']['pages']), em
sp = res['sparse_signals']
spr = [x for x in sp if abs(x['offset_khz'] - 700.0) < 15]
assert spr and spr[0]['peak_dbfs'] > res['noise_floor_dbfs'] + 15, sp
assert not any(abs(x['offset_khz'] - 700.0) < 15 for x in em), em
os.remove(ap_synth)
print("6e pre-flight analysis: OOK + carrier + LO flag + sparse max-hold + POCSAG OK")

# 7. WAV open failure must not kill the receiver; retried later
# (wave.open is monkeypatched to fail while the flag is set: a chmod'd
# read-only RECORD_LOCAL_DIR only blocks the open on Linux, not on Windows)
fail_wav_open = {'fail': True}
real_wave_open = wave.open
def failing_wave_open(path, mode='rb'):
    if mode == 'wb' and fail_wav_open['fail']:
        raise OSError('simulated disk full')
    return real_wave_open(path, mode)
wave.open = failing_wave_open
sessions_before = srv.sessions
set_sched(METEOR_LRPT_HZ, "Meteor-M 2-4", True)
expect_cmd(METEOR_LRPT_HZ)
time.sleep(1.5)
print("   7 diag: recording=", entry['is_recording'], "connected=", entry['connected'],
      "sessions", sessions_before, "->", srv.sessions, "wav_path", entry['wav_path'])
assert not entry['is_recording'] and entry['connected'] and srv.sessions == sessions_before, "receiver reconnected on a WAV open failure"
fail_wav_open['fail'] = False
wait_for(lambda: entry['is_recording'], secs=3, what='WAV open retried')
set_sched(137620000, "idle", False); expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'], what='pass end')
print("7 WAV open failure: no reconnect, retried after WAV_RETRY_SECS OK")

# 8. network drop: the server closes the connection -> the thread reconnects
sessions_before = srv.sessions
srv.drop_client()
wait_for(lambda: srv.sessions > sessions_before, secs=15, what='capture thread reconnected after the drop')
wait_for(lambda: entry['last_data'] > time.time() - 1, what='streaming again')
print("8 network drop mid-stream: reconnected and streaming again OK")

# 9. same satellite reopened within one second must not overwrite
set_sched(137100000, "Meteor-M 2-4", True); expect_cmd(137100000)
wait_for(lambda: entry['is_recording'], what='rec')
set_sched(137100000, "Meteor-M 2-3", True)
wait_for(lambda: entry['wav_path'] and 'Meteor-M_2-3_' in entry['wav_path'], what='split')
set_sched(137100000, "Meteor-M 2-4", True)
wait_for(lambda: entry['wav_path'] and 'Meteor-M_2-4' in entry['wav_path'], what='split back')
set_sched(137620000, "idle", False); expect_cmd(137620000)
wait_for(lambda: not entry['is_recording'], what='end')
names = [os.path.basename(p) for p in wavs()]
assert len(names) == len(set(names)) and len([n for n in names if n.startswith('Meteor-M_2-4_')]) >= 3, names
print("9 unique names on fast re-split:", [n for n in names if 'Meteor-M_2-4' in n])

# 9b. AIS switch (dashboard button): the capture thread role flips — the
# satellite thread exits, the AIS thread tunes 162 MHz and marks the
# receiver enabled; a pending manual recording is dropped with the role;
# switching back restarts satellite capture on the shared frequency.
from noaa_receiver.decoding import ais as _ais
assert radio.set_dongle_ais('nope:1', True) is False    # unknown dongle
with state.status_lock: state.manual_recording[DID] = True
wait_for(lambda: entry.get('manual_iq') is not None, what='manual raw recording before the AIS switch')
assert radio.set_dongle_ais(DID, True) is True
assert db.query_one("SELECT ais FROM dongles")["ais"] == 1, "the AIS role must persist"
wait_for(lambda: not t.is_alive(), secs=5, what='satellite thread exits after the AIS switch')
assert entry['ais'] and not entry['is_recording']
assert entry.get('manual_iq') is None, "the raw capture must be finalized on the role switch"
assert DID not in state.manual_recording, "the manual recording flag must be dropped with the role"
t = threading.Thread(target=_ais.ais_capture_thread, args=(DID,), daemon=True); t.start()
wait_for(lambda: state.ais_enabled and state.ais_dongle == DID, what='AIS thread marks the receiver enabled')
expect_cmd(AIS_CENTER_HZ)      # explicit 162 MHz tune on connect
wait_for(lambda: entry['last_data'] > time.time() - 2 and set(state.ais_channels) == {'A', 'B'},
         what='AIS capture streaming both channels')
assert radio.set_dongle_ais(DID, False) is True
assert db.query_one("SELECT ais FROM dongles")["ais"] == 0
wait_for(lambda: not t.is_alive(), secs=5, what='AIS thread exits after the switch back')
assert not state.ais_enabled and state.ais_dongle is None
t = threading.Thread(target=radio.sdr_capture_thread, args=(DID,), daemon=True); t.start()
entry['last_data'] = 0   # don't be fooled by the AIS thread's last stamp
wait_for(lambda: entry['last_data'] > 0 and not entry['ais'], what='satellite capture streaming again')
expect_cmd(137620000)         # back on the shared idle frequency
print("9b AIS toggle: role flip both ways, 162 MHz tune, recording flag dropped OK")

# 9c. POCSAG role switch: unknown dongle rejected, the flag flips and
# persists, switching to AIS clears it (one dedicated role per dongle)
assert radio.set_dongle_pocsag("nope:1", True) is False
assert radio.set_dongle_pocsag(DID, True) is True
with state.status_lock:
    assert entry["pocsag"] and not entry["ais"]
rows = db.query("SELECT pocsag FROM dongles")
assert rows and rows[0]["pocsag"] == 1, "role must persist"
assert radio.set_dongle_ais(DID, True) is True
with state.status_lock:
    assert entry["ais"] and not entry["pocsag"], "AIS must clear the POCSAG role"
assert radio.set_dongle_ais(DID, False) is True
print("9c POCSAG role switch: flag, persistence, role exclusivity OK")

# 11. pause/play (dashboard button): pause closes the stream, the capture
# thread exits (recording closed, no reconnect while paused - the
# supervisor skips paused dongles); play clears the flag and capture
# restarts. The test starts threads manually (no supervisor here), so
# play restarts the thread the same way the supervisor would.
assert radio.set_dongle_paused('nope:1', True) is False    # unknown dongle
assert radio.set_dongle_paused(DID, True) is True
wait_for(lambda: not t.is_alive(), secs=6, what='capture thread exits on pause')
assert not entry['connected'] and not entry['is_recording'] and entry['paused']
sessions_before = srv.sessions
time.sleep(4)   # two supervisor polls: a paused dongle must not reconnect
assert srv.sessions == sessions_before, 'a paused dongle must not be reconnected'
assert radio.set_dongle_paused(DID, True) is True          # idempotent
assert radio.set_dongle_paused(DID, False) is True
t = threading.Thread(target=radio.sdr_capture_thread, args=(DID,), daemon=True); t.start()
entry['last_data'] = 0
wait_for(lambda: entry['last_data'] > 0, what='streaming again after play')
expect_cmd(137620000)         # back on the shared idle frequency
assert not entry['paused']
print("11 pause/play: thread exits on pause, no reconnect while paused, play restarts OK")

# 10. remove_dongle stops the thread and clears the state
did_b, _ = radio.add_dongle('127.0.0.1', PORT + 50)   # a second (unreachable) dongle
assert did_b in state.sdrs
with state.status_lock: state.manual_recording[DID] = True   # removal must clear it
assert radio.remove_dongle(DID) is True
wait_for(lambda: not t.is_alive(), secs=5, what='capture thread exits after remove')
assert DID not in state.sdrs and DID not in state.manual_dongle_freq and DID not in state.manual_recording
assert state.primary_dongle == did_b, "second dongle must be promoted to primary"
assert radio.remove_dongle('nope:1') is False
radio.remove_dongle(did_b)
assert state.primary_dongle is None
print("10 remove_dongle: thread exit, state cleared, primary promotion OK")

# 12. USB dongles: registry id "usb:<idx>", rtl_sdr source fields, and the
# persistence roundtrip through the dongles table as ("usb", idx)
usb_did, usb_created = radio.add_dongle("usb", 2)
assert usb_did == "usb:2" and usb_created, (usb_did, usb_created)
u = state.sdrs[usb_did]
assert u["usb_idx"] == 2 and u["host"] == "usb" and u["port"] == 2 and u["proc"] is None, u
rows = [dict(r) for r in db.query("SELECT host, port FROM dongles")]
assert {"host": "usb", "port": 2} in rows, rows
_did2, _c2 = radio.add_dongle("usb", 2)          # idempotent
assert _did2 == usb_did and not _c2
try:
    radio.add_dongle("usb", "x")
    raise AssertionError("invalid USB index accepted")
except ValueError:
    pass
assert radio.remove_dongle(usb_did) and usb_did not in state.sdrs
print("12 USB dongle registry: id, source fields, persistence, idempotency, validation OK")

# 13. auto-join satellite passes (the dongle-card checkbox): a dongle in
# a dedicated decode role with auto_sat leaves that role when a pass
# starts and returns to it after set. Driven here exactly like the
# scheduler drives it: auto_sat_join_all() on PASS START,
# auto_sat_leave_all() on PASS END.
did, created = radio.add_dongle(HOST, PORT)
assert created and state.primary_dongle == DID
entry = state.sdrs[DID]
assert radio.set_dongle_auto_sat('nope:1', True) is False      # unknown dongle
assert radio.set_dongle_auto_sat(DID, True) is True
assert db.query_one("SELECT auto_sat FROM dongles")["auto_sat"] == 1, "auto-join must persist"
assert radio.set_dongle_auto_sat(DID, True) is True            # idempotent
# a dongle on the satellites has nothing to suspend
radio.auto_sat_join_all()
with state.status_lock:
    assert not entry.get('auto_saved_role'), "a dongle on the satellites must not be suspended"
# put it in the AIS role and start the AIS thread (no supervisor in
# this test — threads are started by hand, like 9b)
assert radio.set_dongle_ais(DID, True) is True
t_ais = threading.Thread(target=_ais.ais_capture_thread, args=(DID,), daemon=True); t_ais.start()
wait_for(lambda: state.ais_enabled and state.ais_dongle == DID, what='AIS thread up')
# PASS START: the role is suspended, the AIS thread exits, the satellite
# capture thread takes over, retunes to the pass and records
set_sched(437550000, "ISS (Zarya)", True)
radio.auto_sat_join_all()
with state.status_lock:
    assert not entry['ais'] and entry['auto_saved_role'] == 'ais'
wait_for(lambda: not t_ais.is_alive(), secs=5, what='AIS thread exits on auto-join')
t = threading.Thread(target=radio.sdr_capture_thread, args=(DID,), daemon=True); t.start()
expect_cmd(437550000)
wait_for(lambda: entry['is_recording'] and 'ISS_(Zarya)_' in os.path.basename(entry['wav_path'] or ''), what='auto-joined dongle records the pass')
# idempotent: a direct switch to an overlapping pass must not re-suspend
radio.auto_sat_join_all()
with state.status_lock:
    assert entry['auto_saved_role'] == 'ais' and not entry['ais']
# PASS END: the role is restored, the satellite thread exits, the AIS
# thread takes over again
set_sched(137620000, "idle", False)
radio.auto_sat_leave_all()
with state.status_lock:
    assert entry['ais'] and not entry.get('auto_saved_role')
wait_for(lambda: not t.is_alive(), secs=5, what='satellite thread exits on role restore')
wait_for(lambda: not entry['is_recording'], what='pass WAV closed on role restore')
t_ais = threading.Thread(target=_ais.ais_capture_thread, args=(DID,), daemon=True); t_ais.start()
wait_for(lambda: state.ais_enabled and state.ais_dongle == DID, what='AIS thread back after the pass')
expect_cmd(AIS_CENTER_HZ)
# a manual role choice mid-pass wins over the pending restore (the
# auto-joined dongle shows "Satellites" in the role dropdown, so the
# operator's manual pick is a DIFFERENT dedicated role)
set_sched(137100000, "Meteor-M 2-3", True)
radio.auto_sat_join_all()
with state.status_lock:
    assert entry['auto_saved_role'] == 'ais' and not entry['ais']
wait_for(lambda: not t_ais.is_alive(), secs=5, what='AIS thread exits on the second auto-join')
assert radio.set_dongle_adsb(DID, True) is True               # operator picks a role by hand mid-pass
with state.status_lock:
    assert entry['adsb'] and not entry.get('auto_saved_role'), "a manual role change must cancel the pending restore"
set_sched(137620000, "idle", False)
radio.auto_sat_leave_all()
with state.status_lock:
    assert entry['adsb'] and not entry['ais'], "no restore after a manual mid-pass role change"
assert radio.set_dongle_adsb(DID, False) is True
# unchecking the checkbox cancels a pending restore too
assert radio.set_dongle_ais(DID, True) is True
set_sched(137100000, "Meteor-M 2-3", True)
radio.auto_sat_join_all()
with state.status_lock:
    assert entry['auto_saved_role'] == 'ais'
assert radio.set_dongle_auto_sat(DID, False) is True
with state.status_lock:
    assert not entry.get('auto_saved_role') and not entry['auto_sat']
radio.auto_sat_leave_all()
with state.status_lock:
    assert not entry['ais'], "unchecking must cancel the pending restore"
# enabling the checkbox mid-pass joins the running pass immediately
assert radio.set_dongle_ais(DID, True) is True
set_sched(137100000, "Meteor-M 2-3", True)
assert radio.set_dongle_auto_sat(DID, True) is True
with state.status_lock:
    assert entry['auto_saved_role'] == 'ais' and not entry['ais'], "enabling mid-pass must join immediately"
set_sched(137620000, "idle", False)
radio.auto_sat_leave_all()
with state.status_lock:
    assert entry['ais'] and not entry.get('auto_saved_role')
# a paused dongle is not auto-joined
assert radio.set_dongle_paused(DID, True) is True
radio.auto_sat_join_all()
with state.status_lock:
    assert entry['ais'] and not entry.get('auto_saved_role'), "a paused dongle must not auto-join"
assert radio.set_dongle_paused(DID, False) is True
# APRS keeps its channel preset across the suspend/restore
assert radio.set_dongle_aprs(DID, True) is True
assert radio.set_dongle_aprs_iss(DID, True) is True
radio.auto_sat_join_all()
with state.status_lock:
    assert entry['auto_saved_role'] == 'aprs' and not entry['aprs']
radio.auto_sat_leave_all()
with state.status_lock:
    assert entry['aprs'] and entry['aprs_iss'] and not entry.get('auto_saved_role')
assert radio.set_dongle_aprs(DID, False) is True
# persistence roundtrip: the flag survives a reload from the dongles table
radio.load_dongles()
with state.status_lock:
    assert state.sdrs[DID]['auto_sat'] is True, "auto-join must restore from the dongles table"
assert radio.remove_dongle(DID) is True
print("13 auto-join satellite passes: suspend/restore, manual override, pause, APRS preset, persistence OK")
print("ALL RADIO TESTS PASSED")
os._exit(0)
