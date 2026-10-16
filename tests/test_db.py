"""Station database: schema, legacy import, domain roundtrips, backup/restore.

Run: python -u tests/test_db.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from noaa_receiver import db
from noaa_receiver.web import history
from noaa_receiver.decoding import decode

# Everything below works on one temp station root: database + legacy files
ROOT = tempfile.mkdtemp()
os.environ["PRAWN_DB_FILE"] = os.path.join(ROOT, "station.db")
db.DB_FILE = os.environ["PRAWN_DB_FILE"]   # module global is bound at import
db.close()
import noaa_receiver.config as config
config.PASS_HISTORY_FILE = os.path.join(ROOT, "pass_history.json")
config.DONGLES_FILE = os.path.join(ROOT, "dongles.json")
config.TLE_CACHE_FILE = os.path.join(ROOT, "tle_cache.json")
config.AIS_SHIPS_FILE = os.path.join(ROOT, "ais_ships_persist.json")
config.AIS_LOG_FILE = os.path.join(ROOT, "ais_log.jsonl")
config.RECORD_DIR = os.path.join(ROOT, "recordings")
os.makedirs(config.RECORD_DIR, exist_ok=True)
# history/decode imported the config values BEFORE this patch - refresh
history.RECORD_DIR = config.RECORD_DIR

def check(name, cond, detail=""):
    assert cond, f"{name}: {detail}"
    print(f"  {name} OK")

print("1. legacy import from all five JSON/JSONL stores")
legacy_history = [{"sat_name": "Meteor-M 2-3", "frequency_mhz": 137.9125, "max_alt": 54.0,
                   "duration_min": 12.0, "rise_local": "Fri 09.10 09:02",
                   "set_local": "09:14", "rise_ts": 1759993320.0,
                   "set_ts": 1759994040.0, "signal_peak": 939.3,
                   "decoded": True, "png": "Meteor-M_2-3_20261009_090200.png",
                   "wav": "Meteor-M_2-3_20261009_090200.wav", "quality": 80,
                   "satnogs": {"name": "Meteor-M 2-3"},
                   "timestamp": "2026-10-09T09:14:00"}]
open(config.PASS_HISTORY_FILE, "w").write(json.dumps(legacy_history))
open(config.DONGLES_FILE, "w").write(json.dumps(
    [{"host": "192.168.3.245", "port": 1234, "ais": False},
     {"host": "192.168.3.245", "port": 1235, "ais": True}]))
open(config.TLE_CACHE_FILE, "w").write(json.dumps(
    {"57166": ["Meteor-M 2-3", "1 57166U ...", "2 57166 ..."]}))
open(config.AIS_SHIPS_FILE, "w").write(json.dumps(
    [{"mmsi": 123456789, "name": "AILA", "last_seen": time.time()}]))
with open(config.AIS_LOG_FILE, "w") as f:
    f.write(json.dumps({"ts": time.time() - 10, "mmsi": 123456789}) + "\n")
    f.write(json.dumps({"ts": time.time() - 5, "mmsi": 998877665}) + "\n")
# a decode marker next to a recording
open(os.path.join(config.RECORD_DIR, "Meteor-M_2-3_20261009_090200.wav"), "wb").write(b"RIFF")
open(os.path.join(config.RECORD_DIR, "Meteor-M_2-3_20261009_090200.decode.json"), "w").write(
    json.dumps({"success": True, "message": "decoded", "ts": 1.0}))
db.import_legacy(config.RECORD_DIR)
rows = db.query("SELECT * FROM passes")
check("passes imported", len(rows) == 1 and rows[0]["sat_name"] == "Meteor-M 2-3", rows)
check("frequency stored in Hz", rows[0]["frequency_hz"] == 137912500, rows[0])
check("dongles imported", db.query("SELECT COUNT(*) c FROM dongles")[0]["c"] == 2)
check("dongle roles", db.query_one("SELECT ais FROM dongles WHERE port=1235")["ais"] == 1)
check("TLEs imported", db.query_one("SELECT name FROM tle_cache WHERE catnr=57166")["name"] == "Meteor-M 2-3")
check("ships imported", db.query_one("SELECT data FROM ships WHERE mmsi=123456789") is not None)
check("ais log imported", db.query("SELECT COUNT(*) c FROM ais_messages")[0]["c"] == 2)
check("markers imported", decode.read_decode_marker("Meteor-M_2-3_20261009_090200.wav") is not None)
# idempotent: nothing re-imports, sources retired
for src in ("pass_history.json", "dongles.json", "tle_cache.json",
            "ais_ships_persist.json", "ais_log.jsonl"):
    check(f"retired {src}", not os.path.exists(os.path.join(ROOT, src)))
db.import_legacy(config.RECORD_DIR)
check("idempotent re-run", db.query("SELECT COUNT(*) c FROM passes")[0]["c"] == 1)

print("2. history roundtrip in the legacy JSON shape")
h = history._load_history()
check("shape", h[0]["frequency_mhz"] == 137.9125 and h[0]["satnogs"]["name"] == "Meteor-M 2-3", h[0])
check("decoded flag", h[0]["decoded"] is True)
# log_pass merge rule: same satellite, rise within 120 s -> merged
rise = datetime.fromtimestamp(1759993320.0 + 30, tz=timezone.utc)
set_ = datetime.fromtimestamp(1759994040.0 + 30, tz=timezone.utc)
history.log_pass("Meteor-M 2-3", 137912500, 54.0, 12.0, rise, set_, 1200.0,
                 True, "new.png", "Meteor-M_2-3_20261009_090200.wav", quality=91)
check("merged not appended",
      db.query("SELECT COUNT(*) c FROM passes")[0]["c"] == 1)
row = db.query_one("SELECT * FROM passes")
check("peak merged", row["signal_peak"] == 1200.0, row)
check("quality kept", row["quality"] == 80)   # first value wins (COALESCE)
# a genuinely different pass appends
rise2 = datetime.fromtimestamp(1759993320.0 + 3600, tz=timezone.utc)
set2 = datetime.fromtimestamp(1759994040.0 + 3600, tz=timezone.utc)
history.log_pass("Meteor-M 2-4", 137912500, 22.1, 8.0, rise2, set2, 400.0, False, None, None)
check("different pass appended", len(history._load_history()) == 2)
check("quality map", history.quality_map()["Meteor-M_2-3_20261009_090200.wav"] == 80)

print("3. decode markers")
decode._write_decode_marker("X_20261010_101010.wav", False, "no products")
m = decode.read_decode_marker("X_20261010_101010.wav")
check("roundtrip", m is not None and not m["success"] and m["message"] == "no products", m)
# absolute path and bare filename resolve to the same row
m2 = decode.read_decode_marker(os.path.join(config.RECORD_DIR, "X_20261010_101010.wav"))
check("path-insensitive key", m2 == m)
decode.forget_decode_marker("X_20261010_101010.wav")
check("forget", decode.read_decode_marker("X_20261010_101010.wav") is None)

print("4. backup / restore roundtrip")
os.makedirs(os.path.join(config.RECORD_DIR, "Meteor-M_2-3_20261009_090200_lrpt"), exist_ok=True)
open(os.path.join(config.RECORD_DIR, "Meteor-M_2-3_20261009_090200_lrpt", "composite.png"), "wb").write(b"PNG" * 100)
zpath = os.path.join(ROOT, "backup.zip")
_, total = db.build_backup(zpath, config.RECORD_DIR)
check("backup built", os.path.getsize(zpath) > 0 and total > 0)
# wipe + restore into a fresh state
db.close()
os.remove(os.environ["PRAWN_DB_FILE"])
for f in os.listdir(config.RECORD_DIR):
    p = os.path.join(config.RECORD_DIR, f)
    os.remove(p) if os.path.isfile(p) else __import__("shutil").rmtree(p)
summary = db.restore_backup(zpath, config.RECORD_DIR)
check("passes restored", summary["passes"] == 2, summary)
check("ships restored", summary["ships"] == 1, summary)
# wav + its .decode.json sidecar + the product dir composite = 3
check("recordings restored", summary["recording_files"] == 3, summary)
check("db rows back", db.query("SELECT COUNT(*) c FROM passes")[0]["c"] == 2)
check("marker back", decode.read_decode_marker("Meteor-M_2-3_20261009_090200.wav") is not None)
check("file back", os.path.exists(os.path.join(config.RECORD_DIR, "Meteor-M_2-3_20261009_090200.wav")))
# a corrupt archive is refused without touching the live state
open(os.path.join(ROOT, "bad.zip"), "wb").write(b"not a zip")
before = db.query("SELECT COUNT(*) c FROM passes")[0]["c"]
try:
    db.restore_backup(os.path.join(ROOT, "bad.zip"), config.RECORD_DIR)
    check("bad archive refused", False)
except ValueError:
    check("bad archive refused", db.query("SELECT COUNT(*) c FROM passes")[0]["c"] == before)

print("test_db: all OK")
os._exit(0)
