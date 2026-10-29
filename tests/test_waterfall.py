"""Pass waterfall diagrams (analysis/waterfall.py) and the ISS raw-IQ
keep (decoding/decode._keep_raw_iq, config.IQ_RECORD_FREQS).

Every recorded pass must leave a <base>.waterfall.png spectrogram —
even when nothing is decoded — and ISS passes record their raw IQ,
which is never deleted after a decode (the SSTV decode runs on the WAV
audio; the baseband is the fallback for decode issues).

Run: python -u tests/test_waterfall.py
"""
import os
import sys
import tempfile
import time
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))

from noaa_receiver import archive, db, state          # noqa: E402
from noaa_receiver.analysis.waterfall import (      # noqa: E402
    pass_waterfall, waterfall_png_path)
from noaa_receiver.config import (                   # noqa: E402
    IQ_RECORD_FREQS, ISS_SSTV_HZ, TRACKED_SATS)
from noaa_receiver.decoding.decode import (          # noqa: E402
    _keep_raw_iq, decode_recording, read_decode_marker)
from noaa_receiver.web import history                # noqa: E402

db.query("SELECT 1")           # schema ready against the isolated test DB


def check(name, cond, detail=""):
    assert cond, f"{name}: {detail}"
    print(f"  {name} OK")


def write_iq(path, c):
    """Complex baseband -> raw u8 IQ bytes (the rtl_tcp recording format)."""
    a = np.empty((len(c), 2), dtype=np.uint8)
    a[:, 0] = np.clip(np.round(c.real + 127.5), 0, 255)
    a[:, 1] = np.clip(np.round(c.imag + 127.5), 0, 255)
    with open(path, 'wb') as f:
        f.write(a.tobytes())


def write_wav(path, samples, rate=48000):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def touch(path, age, content=b'x'):
    with open(path, 'wb') as f:
        f.write(content)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


print("1. config: ISS passes record raw IQ (and it is the tracked tune)")
check("ISS SSTV frequency is in IQ_RECORD_FREQS", ISS_SSTV_HZ in IQ_RECORD_FREQS)
check("TRACKED_SATS uses the ISS_SSTV_HZ constant",
      TRACKED_SATS[25544] == ("ISS (Zarya)", ISS_SSTV_HZ), TRACKED_SATS[25544])

print("2. _keep_raw_iq: ISS IQ is never deleted, Meteor IQ is")
check("ISS recording -> keep", _keep_raw_iq("/x/ISS_(Zarya)_20261023_120000.wav"))
check("Meteor recording -> delete",
      not _keep_raw_iq("/x/Meteor-M_2-4_20261023_120000.wav"))
check("unknown recording -> delete",
      not _keep_raw_iq("/x/Random_20261023_120000.wav"))

print("3. waterfall from the raw IQ: carrier burst lands at its bin")
d = tempfile.mkdtemp()
fs, dur = 960_000, 4.0
n = int(fs * dur)
t = np.arange(n) / fs
rng = np.random.default_rng(1)
c = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 5.0
mid = slice(n // 3, 2 * n // 3)
c[mid] += 40.0 * np.exp(2j * np.pi * 100_000 * t[mid])   # +100 kHz burst
wav = os.path.join(d, 'Meteor-M_2-4_20261023_120000.wav')
write_iq(wav[:-len('.wav')] + '.iq.u8', c)
write_wav(wav, np.zeros(int(dur * 48000), dtype=np.int16))
wf = pass_waterfall(wav, max_rows=64, n_fft=256)
check("waterfall path", wf == waterfall_png_path(wav), str(wf))
check("waterfall exists", wf and os.path.exists(wf))
from PIL import Image                                  # noqa: E402
with Image.open(wf) as im:
    check("grayscale PNG", im.mode == 'L', im.mode)
    check("width = FFT bins", im.width == 256, im.width)
    arr = np.asarray(im)
check("row cap respected", 8 <= arr.shape[0] <= 64, arr.shape)
# +100 kHz at 960 kHz / 256 bins = bin 128 + 26.7 -> the tone's Hann
# mainlobe covers columns ~153-157; the burst sounds during the middle
# third of the recording
h = arr.shape[0]
carrier_band = arr[h // 3:2 * h // 3, 153:158].mean()
quiet_band = arr[h // 3:2 * h // 3, 10:21].mean()
check("carrier band bright", carrier_band > quiet_band + 50,
      f"carrier {carrier_band:.0f} vs quiet {quiet_band:.0f}")
check("burst only mid-pass (edges dark at the carrier bin)",
      arr[h // 3:2 * h // 3, 155].mean() > arr[:h // 6, 155].mean() + 50,
      f"mid {arr[h//3:2*h//3, 155].mean():.0f} vs top {arr[:h//6, 155].mean():.0f}")
mtime = os.path.getmtime(wf)
check("idempotent (existing waterfall returned, not rewritten)",
      pass_waterfall(wav, max_rows=64, n_fft=256) == wf
      and os.path.getmtime(wf) == mtime)

print("4. waterfall falls back to the WAV audio when there is no IQ")
d = tempfile.mkdtemp()
dur = 4.0
n = int(48000 * dur)
t = np.arange(n) / 48000
rng = np.random.default_rng(2)
a = rng.standard_normal(n) * 500.0
mid = slice(n // 3, 2 * n // 3)
a[mid] += 12000.0 * np.sin(2 * np.pi * 2300 * t[mid])     # SSTV-ish tone
wav = os.path.join(d, 'ISS_(Zarya)_20261023_120000.wav')
write_wav(wav, a)
wf = pass_waterfall(wav, max_rows=64, n_fft=256)
check("waterfall written from the WAV", wf and os.path.exists(wf), str(wf))
with Image.open(wf) as im:
    arr = np.asarray(im)
h = arr.shape[0]
# 2300 Hz at 48 kHz / 256 bins = bin 128 + 12.3 -> the tone's Hann
# mainlobe covers columns ~138-143
tone_band = arr[h // 3:2 * h // 3, 138:144].mean()
quiet_band = arr[h // 3:2 * h // 3, 60:71].mean()
check("tone band bright", tone_band > quiet_band + 50,
      f"tone {tone_band:.0f} vs quiet {quiet_band:.0f}")

print("5. a too-short recording gets no waterfall")
d = tempfile.mkdtemp()
wav = os.path.join(d, 'ISS_(Zarya)_20261023_120100.wav')
write_wav(wav, np.zeros(1000, dtype=np.int16))           # 21 ms
check("None for a 21 ms WAV", pass_waterfall(wav, max_rows=64, n_fft=256) is None)

print("6. decode_recording failure leaves a waterfall + a failure marker")
d = tempfile.mkdtemp()
wav = os.path.join(d, 'ISS_(Zarya)_20261023_120200.wav')
rng = np.random.default_rng(3)
write_wav(wav, (rng.standard_normal(48000 * 5) * 3000).astype(np.int16))
ok, png, err = decode_recording(wav)
check("decode failed (noise holds no SSTV)", not ok, err)
marker = read_decode_marker(wav)
check("failure marker written", marker is not None and not marker['success'],
      str(marker))
wf = waterfall_png_path(wav)
check("waterfall left behind", os.path.exists(wf), wf)
# ...and a second attempt short-circuits on the marker (no re-decode)
ok2, _png2, err2 = decode_recording(wav)
check("second attempt short-circuits", not ok2 and 'already attempted' in err2, err2)

print("6b. an ISS recording's raw IQ survives the decode attempt")
# The operator requirement: when ISS flies, the raw IQ is KEPT (decode
# fallback) — no decode path may remove it. The SSTV decode runs on the
# WAV audio and never touches the baseband.
d = tempfile.mkdtemp()
wav = os.path.join(d, 'ISS_(Zarya)_20261023_120300.wav')
rng = np.random.default_rng(4)
write_wav(wav, (rng.standard_normal(48000 * 5) * 3000).astype(np.int16))
iq = wav[:-len('.wav')] + '.iq.u8'      # the sibling the capture thread writes
write_iq(iq, (rng.standard_normal(960_000 * 2)
              + 1j * rng.standard_normal(960_000 * 2)) * 5.0)
iq_size = os.path.getsize(iq)
ok, png, err = decode_recording(wav)
check("decode failed (noise holds no SSTV)", not ok, err)
check("raw IQ still on disk after the decode attempt",
      os.path.exists(iq) and os.path.getsize(iq) == iq_size)
check("waterfall generated from the IQ",
      os.path.exists(waterfall_png_path(wav)))

print("7. the archiver moves the waterfall with the recording family")
local, share = tempfile.mkdtemp(), tempfile.mkdtemp()
base = 'Meteor-M_2-4_20261023_130000'
touch(os.path.join(local, base + '.wav'), 400, b'RIFF')
touch(os.path.join(local, base + '.waterfall.png'), 400, b'PNG')
touch(os.path.join(local, base + '.iq.u8'), 400)
with db.write() as cur:
    cur.execute("INSERT OR REPLACE INTO decodes VALUES (?,?,?,?)",
                (base, 0, 'test: dark pass', time.time()))
n = archive.archive_pass(local, share, now=time.time(), force=True)
check("family (wav + waterfall + iq) moved", n == 3, n)
check("waterfall is on the archive",
      os.path.exists(os.path.join(share, base + '.waterfall.png')))
check("waterfall left the local dir",
      not os.path.exists(os.path.join(local, base + '.waterfall.png')))

print("8. get_recordings exposes the waterfall field")
d = tempfile.mkdtemp()
base = 'ISS_(Zarya)_20261023_140000'
open(os.path.join(d, base + '.wav'), 'wb').write(b'RIFF')
open(os.path.join(d, base + '.waterfall.png'), 'wb').write(b'PNG')
open(os.path.join(d, base + '.iq.u8'), 'wb').write(b'\0' * 1024 * 1024)
saved_dir, saved_local = history.RECORD_DIR, history.RECORD_LOCAL_DIR
history.RECORD_DIR = history.RECORD_LOCAL_DIR = d
try:
    r = history.get_recordings()[0]
    check("waterfall listed", r['waterfall'] == base + '.waterfall.png', str(r))
    check("raw IQ listed (kept for ISS)",
          r['iq'] and r['iq']['filename'] == base + '.iq.u8', str(r))
    check("still NOT decoded (waterfall is not a decode image)",
          not r['decoded'] and r['png'] is None, str(r))
    # without the waterfall the field is None
    os.remove(os.path.join(d, base + '.waterfall.png'))
    r = history.get_recordings()[0]
    check("waterfall None when absent", r['waterfall'] is None, str(r))
finally:
    history.RECORD_DIR, history.RECORD_LOCAL_DIR = saved_dir, saved_local

print("6c. raw IQ is deleted after EVERY successful decode (2026-10-29)")
from noaa_receiver.decoding.decode import _delete_raw_iq_after_success  # noqa: E402
d = tempfile.mkdtemp()
# Meteor pass, decode succeeded -> IQ deleted
wav = os.path.join(d, 'Meteor-M_2-3_20261029_110408.wav')
iq = wav[:-len('.wav')] + '.iq.u8'
open(iq, 'wb').write(b'\0' * 4096)
_delete_raw_iq_after_success(wav, iq)
check("Meteor IQ deleted on success", not os.path.exists(iq))
# ISS pass, decode succeeded -> IQ kept (decode fallback)
wav = os.path.join(d, 'ISS_(Zarya)_20261029_030116.wav')
iq = wav[:-len('.wav')] + '.iq.u8'
open(iq, 'wb').write(b'\0' * 4096)
_delete_raw_iq_after_success(wav, iq)
check("ISS IQ kept even on success", os.path.exists(iq))
# missing IQ is a no-op, not an error
_delete_raw_iq_after_success(wav, iq + '.gone')
check("missing IQ tolerated", True)

print("test_waterfall: all OK")
os._exit(0)
