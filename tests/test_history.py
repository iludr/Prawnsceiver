"""history.py: pass-history logging — one entry per physical pass.

Run: python3 tests/test_history.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.web import history   # noqa: E402

d = tempfile.mkdtemp()
history.PASS_HISTORY_FILE = os.path.join(d, 'pass_history.json')

def rise(offset_s=0.0):
    return datetime(2026, 10, 7, 22, 54, tzinfo=timezone.utc) + timedelta(seconds=offset_s)

# First log of a pass: appended
history.log_pass("Meteor-M 2-4", 137912500, 64.5, 13.2, rise(), rise(792),
                  585.6, decoded=False, png_file=None, wav_file="Meteor-M_2-4_20261007_225338.wav")
h = history._load_history()
assert len(h) == 1 and h[0]["signal_peak"] == 585.6 and not h[0]["decoded"], h

# Same physical pass logged again (receiver flip-flop / re-trigger): merged,
# not duplicated — the later decode result and the higher peak win
history.log_pass("Meteor-M 2-4", 137912500, 64.5, 13.2, rise(9), rise(801),
                  611.1, decoded=True, png_file="Meteor-M_2-4_20261007_225338.png",
                  wav_file="Meteor-M_2-4_20261007_225338.wav", quality=23)
h = history._load_history()
assert len(h) == 1, h
assert h[0]["decoded"] and h[0]["png"] == "Meteor-M_2-4_20261007_225338.png", h
assert h[0]["signal_peak"] == 611.1 and h[0]["quality"] == 23, h

# A genuinely different pass (next orbit, hours later): appended
history.log_pass("Meteor-M 2-4", 137912500, 22.1, 8.0, rise(3600), rise(4080),
                  1007.1, decoded=False, png_file=None, wav_file="Meteor-M_2-4_20261008_003553.wav")
assert len(history._load_history()) == 2

# A different satellite rising in the same window is NOT merged
history.log_pass("Meteor-M 2-3", 137912500, 19.4, 7.0, rise(30), rise(450),
                  294.2, decoded=False, png_file=None, wav_file=None)
h = history._load_history()
assert len(h) == 3 and h[-1]["sat_name"] == "Meteor-M 2-3", h


# ---- get_recordings: raw IQ info + decode product list ----
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, 'Meteor-M_2-3_20261007_215400_lrpt'))
open(os.path.join(d, 'Meteor-M_2-3_20261007_215400.wav'), 'wb').write(b'RIFF')
open(os.path.join(d, 'Meteor-M_2-3_20261007_215400.iq.u8'), 'wb').write(b'\0' * 1024 * 1024)
open(os.path.join(d, 'Meteor-M_2-3_20261007_215400_lrpt', 'composite.png'), 'wb').write(b'x' * 2048)
saved_dir, saved_hist = history.RECORD_DIR, history.PASS_HISTORY_FILE
history.RECORD_DIR, history.PASS_HISTORY_FILE = d, os.path.join(d, 'no_history.json')
try:
    r = history.get_recordings()[0]
    assert r['iq'] and r['iq']['filename'] == 'Meteor-M_2-3_20261007_215400.iq.u8', r
    assert r['iq']['size_mb'] == 1.0, r
    assert len(r['products']) == 1, r
    assert r['products'][0]['name'] == 'composite.png', r
    assert r['products'][0]['size_kb'] == 2.0, r
    assert r['products'][0]['path'] == 'Meteor-M_2-3_20261007_215400_lrpt/composite.png', r
finally:
    history.RECORD_DIR, history.PASS_HISTORY_FILE = saved_dir, saved_hist
print("get_recordings products OK")

print("pass-history merge OK")
