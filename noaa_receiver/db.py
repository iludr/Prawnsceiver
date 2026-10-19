"""Station database: one SQLite file cataloging all persistent state.

Replaces the JSON/JSONL side files (pass_history.json, dongles.json,
tle_cache.json, ais_ships_persist.json, ais_log.jsonl and the per-
recording <base>.decode.json markers) with a single station.db. The
filesystem remains the store for the big artifacts (WAV / raw IQ / PNGs
/ product directories) — this database is the catalog, not the blob
store; rows point at files.

Concurrency: one process, several threads (scheduler, one capture
thread per dongle, HTTP handlers). A single shared connection with
check_same_thread=False; writes serialized by a module lock inside
explicit transactions; WAL journal so readers never block the writer
and a crash cannot corrupt the file. SQLite comfortably handles this
station's write rate (a few inserts per second at AIS burst peaks).

Legacy import: import_legacy() ingests the old JSON files and the
decode markers ONCE (rows already present win, so it is idempotent),
then renames each source to <name>.migrated so a restart mid-migration
can never double-import or lose data.

Backup/restore: build_backup() writes a zip containing a consistent
SQLite snapshot plus the whole recordings tree; restore_backup()
replaces the live database and recordings with such an archive (the
caller must refuse during an active pass - live capture threads hold
open file handles).
"""
import glob
import json
import os
import sqlite3
import threading
import time
import zipfile

from .config import LOGDIR

# The PRAWN_DB_FILE override exists for tests (isolated temp
# databases) - production always uses LOGDIR/station.db
DB_FILE = os.environ.get("PRAWN_DB_FILE") or os.path.join(LOGDIR, "station.db")


_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);

-- Pass history (was pass_history.json, capped at 50 entries)
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY,
    sat_name TEXT NOT NULL,
    rise_ts REAL NOT NULL,
    set_ts REAL,
    rise_local TEXT, set_local TEXT,
    max_alt REAL, duration_min REAL, frequency_hz INTEGER,
    signal_peak REAL, quality INTEGER,
    decoded INTEGER DEFAULT 0,
    wav TEXT, png TEXT,
    satnogs TEXT, logged_ts REAL);
CREATE INDEX IF NOT EXISTS passes_sat_rise ON passes(sat_name, rise_ts);

-- Registered rtl_tcp dongles (was dongles.json)
CREATE TABLE IF NOT EXISTS dongles (
    host TEXT NOT NULL, port INTEGER NOT NULL,
    ais INTEGER DEFAULT 0, added_ts REAL,
    PRIMARY KEY (host, port));

-- Last good TLEs (was tle_cache.json)
CREATE TABLE IF NOT EXISTS tle_cache (
    catnr INTEGER PRIMARY KEY, name TEXT, l1 TEXT, l2 TEXT,
    updated_ts REAL);

-- Decode-attempt markers (was <recording>.decode.json sidecars)
CREATE TABLE IF NOT EXISTS decodes (
    base TEXT PRIMARY KEY,      -- recording name without extension
    success INTEGER, message TEXT, ts REAL);

-- Every AIS ship ever received (was ais_ships_persist.json);
-- the full ship dict is kept as JSON so the dashboard shape is stable
CREATE TABLE IF NOT EXISTS ships (
    mmsi INTEGER PRIMARY KEY, data TEXT, updated_ts REAL);

-- Rolling AIS frame log (was ais_log.jsonl); trimmed by ts
CREATE TABLE IF NOT EXISTS ais_messages (
    id INTEGER PRIMARY KEY, ts REAL, entry TEXT);
CREATE INDEX IF NOT EXISTS ais_messages_ts ON ais_messages(ts);

-- Rolling Orbcomm decoded-packet log; trimmed by ts
CREATE TABLE IF NOT EXISTS orbcomm_messages (
    id INTEGER PRIMARY KEY, ts REAL, entry TEXT);
CREATE INDEX IF NOT EXISTS orbcomm_messages_ts ON orbcomm_messages(ts);

-- Rolling decoded POCSAG page log; trimmed by ts
CREATE TABLE IF NOT EXISTS pocsag_messages (
    id INTEGER PRIMARY KEY, ts REAL, entry TEXT);
CREATE INDEX IF NOT EXISTS pocsag_messages_ts ON pocsag_messages(ts);

-- Rolling APRS decoded-frame log; trimmed by ts
CREATE TABLE IF NOT EXISTS aprs_messages (
    id INTEGER PRIMARY KEY, ts REAL, entry TEXT);
CREATE INDEX IF NOT EXISTS aprs_messages_ts ON aprs_messages(ts);

-- Every APRS station ever received (like the AIS ships table);
-- the full station dict is kept as JSON so the page shape is stable
CREATE TABLE IF NOT EXISTS aprs_stations (
    call TEXT PRIMARY KEY, data TEXT, updated_ts REAL);

-- Rolling decoded ADS-B frame log; trimmed by ts
CREATE TABLE IF NOT EXISTS adsb_messages (
    id INTEGER PRIMARY KEY, ts REAL, entry TEXT);
CREATE INDEX IF NOT EXISTS adsb_messages_ts ON adsb_messages(ts);

-- Every ADS-B aircraft ever received (like the AIS ships table);
-- the full aircraft dict is kept as JSON so the page shape is stable
CREATE TABLE IF NOT EXISTS adsb_aircrafts (
    icao TEXT PRIMARY KEY, data TEXT, updated_ts REAL);

-- Prawntuner receive-antenna sessions and their measured length points
CREATE TABLE IF NOT EXISTS tuner_sessions (
    id INTEGER PRIMARY KEY,
    dongle TEXT NOT NULL, service TEXT NOT NULL, antenna TEXT NOT NULL,
    started REAL NOT NULL, ended REAL,
    target_hz INTEGER, nominal_cm REAL, initial_length_cm REAL,
    converged_length_cm REAL, best_dbfs REAL, restore_json TEXT);
CREATE INDEX IF NOT EXISTS tuner_sessions_started ON tuner_sessions(started);
CREATE TABLE IF NOT EXISTS tuner_points (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL, ts REAL NOT NULL, length_cm REAL NOT NULL,
    peak_dbfs REAL, floor_dbfs REAL, ratio REAL, decodes INTEGER,
    valid INTEGER NOT NULL, note TEXT,
    FOREIGN KEY(session_id) REFERENCES tuner_sessions(id));
CREATE INDEX IF NOT EXISTS tuner_points_session_ts
    ON tuner_points(session_id, ts);
"""


def connect():
    """The shared connection (lazily created, schema-checked)."""
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
            _conn = sqlite3.connect(DB_FILE, check_same_thread=False,
                                    timeout=20)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=20000")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
            # Schema migration (CREATE IF NOT EXISTS cannot extend an
            # existing table): the dongle role flags added after the
            # first deployment arrive as columns here
            cols = {r[1] for r in _conn.execute("PRAGMA table_info(dongles)")}
            if "orbcomm" not in cols:
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN orbcomm INTEGER DEFAULT 0")
                _conn.commit()
            if "aprs" not in cols:
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN aprs INTEGER DEFAULT 0")
                _conn.commit()
            if "aprs_iss" not in cols:
                # ISS digipeater checkbox for a dongle in the APRS role
                # (145.825 MHz instead of 144.800 MHz)
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN aprs_iss INTEGER DEFAULT 0")
                _conn.commit()
            if "aprs_lasar" not in cols:
                # LASARsat digipeater checkbox for a dongle in the APRS
                # role (436.925 MHz, GFSK 9600 - see decoding/aprs.py)
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN aprs_lasar INTEGER DEFAULT 0")
                _conn.commit()
            if "pocsag" not in cols:
                # POCSAG dongle role (see decoding/pocsag.py)
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN pocsag INTEGER DEFAULT 0")
                _conn.commit()
            if "adsb" not in cols:
                # ADS-B dongle role (see decoding/adsb.py)
                _conn.execute("ALTER TABLE dongles"
                              " ADD COLUMN adsb INTEGER DEFAULT 0")
                _conn.commit()
            # One-time repair: the pre-explicit-column positional
            # INSERTs wrote the orbcomm flag into added_ts and the
            # timestamp into orbcomm (the column arrived by ALTER
            # after the INSERTs were written). Idempotent - a garbage
            # orbcomm value is always a timestamp, roles re-persist
            # correctly from here on.
            rows = list(_conn.execute(
                "SELECT rowid, added_ts, orbcomm FROM dongles"
                " WHERE orbcomm > 1000000000"))
            for rowid, added_ts, bad in rows:
                _conn.execute(
                    "UPDATE dongles SET added_ts = ?, orbcomm = ?"
                    " WHERE rowid = ?",
                    (bad, 1 if added_ts == 1 else 0, rowid))
            if rows:
                _conn.commit()
        return _conn


def close():
    """Close the shared connection (backup/restore swaps the file)."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


class write:
    """Serialized write transaction: with db.write() as cur: ..."""
    def __enter__(self):
        _lock.acquire()
        self.cur = connect().cursor()
        return self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                connect().commit()
            else:
                connect().rollback()
        finally:
            self.cur.close()
            _lock.release()
        return False


def query(sql, args=()):
    """All matching rows as dicts."""
    with _lock:
        return [dict(r) for r in connect().execute(sql, args).fetchall()]


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


# ---------- legacy import (one-time, idempotent) ----------

def _imported(cur, source):
    row = cur.execute("SELECT value FROM meta WHERE key='imported_'||?",
                      (source,)).fetchone()
    return row is not None


def _mark_imported(cur, source):
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('imported_' || ?, '1')",
                (source,))


def _retire(path):
    """Keep the migrated source for forensics instead of deleting it."""
    os.replace(path, path + ".migrated")


def import_legacy(record_dir):
    """Ingest every legacy store into the database (idempotent: rows win
    over files, each source is imported exactly once and then renamed
    to <name>.migrated). Called at startup before anything reads."""
    from . import state
    from .config import (AIS_LOG_FILE, AIS_SHIPS_FILE, DONGLES_FILE,
                         PASS_HISTORY_FILE, RECORD_DIR, TLE_CACHE_FILE)
    record_dir = record_dir or RECORD_DIR
    n = {}
    with write() as cur:
        # --- pass history ---
        if os.path.exists(PASS_HISTORY_FILE) and not _imported(cur, "pass_history"):
            count = 0
            try:
                with open(PASS_HISTORY_FILE, encoding="utf-8") as f:
                    for h in json.load(f):
                        if "rise_ts" not in h:
                            continue
                        cur.execute(
                            "INSERT OR IGNORE INTO passes (sat_name, rise_ts,"
                            " set_ts, rise_local, set_local, max_alt,"
                            " duration_min, frequency_hz, signal_peak,"
                            " quality, decoded, wav, png, satnogs, logged_ts)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (h.get("sat_name"), h["rise_ts"], h.get("set_ts"),
                             h.get("rise_local"), h.get("set_local"),
                             h.get("max_alt"), h.get("duration_min"),
                             int((h.get("frequency_mhz") or 0) * 1e6 + 0.5),
                             h.get("signal_peak"), h.get("quality"),
                             1 if h.get("decoded") else 0,
                             h.get("wav"), h.get("png"),
                             json.dumps(h["satnogs"]) if h.get("satnogs") else None,
                             None))
                        count += 1
                n["passes"] = count
                _mark_imported(cur, "pass_history")
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # --- dongles ---
        if os.path.exists(DONGLES_FILE) and not _imported(cur, "dongles"):
            try:
                with open(DONGLES_FILE, encoding="utf-8") as f:
                    for e in json.load(f):
                        cur.execute(
                            "INSERT OR IGNORE INTO dongles (host, port, ais,"
                            " added_ts) VALUES (?,?,?,?)",
                            (e["host"], int(e["port"]),
                             1 if e.get("ais") else 0, time.time()))
                _mark_imported(cur, "dongles")
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # --- TLE cache ---
        if os.path.exists(TLE_CACHE_FILE) and not _imported(cur, "tle_cache"):
            try:
                with open(TLE_CACHE_FILE, encoding="utf-8") as f:
                    cached = json.load(f)
                now = time.time()
                for catnr_str, lines in cached.items():
                    if isinstance(lines, list) and len(lines) == 3:
                        cur.execute("INSERT OR REPLACE INTO tle_cache VALUES (?,?,?,?,?)",
                                    (int(catnr_str), lines[0], lines[1], lines[2], now))
                _mark_imported(cur, "tle_cache")
            except (OSError, ValueError, TypeError):
                pass
        # --- AIS ship registry ---
        if os.path.exists(AIS_SHIPS_FILE) and not _imported(cur, "ships"):
            try:
                with open(AIS_SHIPS_FILE, encoding="utf-8") as f:
                    ships = json.load(f)
                now = time.time()
                for s in ships:
                    if isinstance(s, dict) and "mmsi" in s:
                        cur.execute("INSERT OR IGNORE INTO ships VALUES (?,?,?)",
                                    (s["mmsi"], json.dumps(s), now))
                n["ships"] = len(ships)
                _mark_imported(cur, "ships")
            except (OSError, ValueError, TypeError):
                pass
    # --- AIS log (large; commit per chunk) ---
    if os.path.exists(AIS_LOG_FILE) and not query_one(
            "SELECT value FROM meta WHERE key='imported_ais_log'"):
        count = 0
        try:
            with open(AIS_LOG_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        with write() as cur:
                            cur.execute("INSERT INTO ais_messages (ts, entry)"
                                        " VALUES (?,?)",
                                        (e.get("ts", 0), line.strip()))
                        count += 1
                    except ValueError:
                        continue
            with write() as cur:
                _mark_imported(cur, "ais_log")
            n["ais_messages"] = count
        except OSError:
            pass
    # --- decode markers next to recordings ---
    if not query_one("SELECT value FROM meta WHERE key='imported_markers'"):
        count = 0
        if os.path.isdir(record_dir):
            for mpath in glob.glob(os.path.join(record_dir, "*.decode.json")):
                try:
                    with open(mpath, encoding="utf-8") as f:
                        m = json.load(f)
                    with write() as cur:
                        cur.execute("INSERT OR IGNORE INTO decodes VALUES (?,?,?,?)",
                                    (os.path.basename(mpath)[:-len(".decode.json")],
                                     1 if m.get("success") else 0,
                                     m.get("message"), m.get("ts")))
                    count += 1
                except (OSError, ValueError):
                    continue
        with write() as cur:
            _mark_imported(cur, "markers")
        if count:
            n["decode_markers"] = count
    # Retire the sources only after their rows are committed - and only
    # the ones that actually imported (a source that failed to parse must
    # stay on disk for a retry, not be renamed away)
    with write() as cur:
        for source, path in (("pass_history", PASS_HISTORY_FILE),
                             ("dongles", DONGLES_FILE),
                             ("tle_cache", TLE_CACHE_FILE),
                             ("ships", AIS_SHIPS_FILE),
                             ("ais_log", AIS_LOG_FILE)):
            if _imported(cur, source):
                try:
                    if os.path.exists(path):
                        _retire(path)
                except OSError:
                    pass
    if n:
        state.log_console("📚 Legacy data imported into station.db: "
                           + ", ".join(f"{k}={v}" for k, v in sorted(n.items())))
    return n


# ---------- backup / restore ----------

def build_backup(dest_path, record_dir):
    """Full station backup: a consistent SQLite snapshot plus the whole
    recordings tree (audio, raw IQ, images, product directories) into a
    zip. Stored (not deflated): the payloads are incompressible media.
    Returns (path, bytes_written)."""
    from .config import RECORD_DIR, RECORD_LOCAL_DIR
    record_dir = record_dir or RECORD_DIR
    manifest = {"format": "prawntenna-backup", "version": 1,
                "created": time.time()}
    # Consistent snapshot of the live database (WAL-safe)
    snap = dest_path + ".db"
    with _lock:
        src = connect()
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        dst.close()
    total = 0
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.write(snap, "station.db")
        have = set()
        for root, dirs, files in os.walk(record_dir):
            for name in sorted(files):
                p = os.path.join(root, name)
                arc = os.path.join("recordings",
                                   os.path.relpath(p, record_dir))
                z.write(p, arc)
                have.add(arc.replace(os.sep, "/"))
                total += os.path.getsize(p)
        # Pending local captures (recorded but not yet archived - see
        # archive.py) ride along under the same archive root; a name
        # that exists in both (a family mid-move) is taken from the
        # archive copy only, so the zip never holds it twice.
        if os.path.realpath(RECORD_LOCAL_DIR) != os.path.realpath(record_dir):
            for root, dirs, files in os.walk(RECORD_LOCAL_DIR):
                for name in sorted(files):
                    p = os.path.join(root, name)
                    arc = os.path.join("recordings",
                                       os.path.relpath(p, RECORD_LOCAL_DIR)
                                       ).replace(os.sep, "/")
                    if arc in have:
                        continue
                    z.write(p, arc)
                    total += os.path.getsize(p)
        total += os.path.getsize(snap)
    os.remove(snap)
    return dest_path, total


def restore_backup(zip_path, record_dir):
    """Replace the live database and the recordings tree with a backup
    built by build_backup. The current state is moved aside (station.db
    and recordings/ get .pre-restore suffixes) so a bad archive can
    always be rolled back by hand. Callers must refuse during an active
    pass. Returns a summary dict; raises ValueError on a bad archive."""
    from .config import RECORD_DIR
    record_dir = record_dir or RECORD_DIR
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ValueError("not a station backup archive")
    with z:
        names = z.namelist()
        if "manifest.json" not in names or "station.db" not in names:
            raise ValueError("not a station backup archive")
        try:
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != "prawntenna-backup":
                raise ValueError
        except ValueError:
            raise ValueError("not a station backup archive")
        close()
        if os.path.exists(DB_FILE):
            os.replace(DB_FILE, DB_FILE + ".pre-restore")
        for suffix in ("-wal", "-shm"):
            p = DB_FILE + suffix
            if os.path.exists(p):
                os.remove(p)
        z.extract("station.db", os.path.dirname(DB_FILE))
        # recordings: move the current tree aside, then extract
        if os.path.isdir(record_dir):
            os.replace(record_dir, record_dir + ".pre-restore")
        os.makedirs(record_dir, exist_ok=True)
        for name in names:
            if name.startswith("recordings/"):
                z.extract(name, os.path.dirname(record_dir))
    # Reopen + sanity-check the restored database
    with write() as cur:
        n_passes = cur.execute("SELECT COUNT(*) FROM passes").fetchone()[0]
        n_ships = cur.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
    files = sum(len(f) for _, _, f in os.walk(record_dir))
    return {"passes": n_passes, "ships": n_ships,
            "recording_files": files, "created": manifest.get("created")}
