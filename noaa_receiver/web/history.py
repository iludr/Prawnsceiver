"""Pass history log and recording file management."""
import glob
import json
import os
from datetime import datetime, timedelta

from .. import db
from .. import state
from ..config import RECORD_DIR, RECORD_LOCAL_DIR, UTC_OFFSET
from ..decoding.decode import read_decode_marker

def log_pass(sat_name, frequency, max_alt, duration_min, rise_time, set_time, signal_peak, decoded, png_file, wav_file, quality=None, satnogs=None):
    """Log a completed pass (with recording metadata) to the database.

    satnogs: the SatNOGS DB snapshot fetched during the pass (names, launch,
    status, transmitters) — stored with the pass so the history page shows
    what was received even long after the satellite decays.

    One entry per physical pass: tracking of a pass can END twice
    (overlapping same-frequency passes flipping the receiver back and
    forth, a manual-tune interruption that re-triggers the same window).
    The second log carries the later decode state — merge, don't append.
    """
    rise_ts = rise_time.timestamp()
    set_ts = set_time.timestamp()
    rise_local = (rise_time + timedelta(hours=UTC_OFFSET)).strftime("%a %d.%m %H:%M")
    set_local = (set_time + timedelta(hours=UTC_OFFSET)).strftime("%H:%M")
    satnogs_json = json.dumps(satnogs) if satnogs is not None else None
    with db.write() as cur:
        row = cur.execute(
            "SELECT id, signal_peak, decoded FROM passes"
            " WHERE sat_name=? AND ABS(rise_ts-?)<=120"
            " ORDER BY id DESC LIMIT 1", (sat_name, rise_ts)).fetchone()
        if row:
            cur.execute(
                "UPDATE passes SET"
                " signal_peak=MAX(COALESCE(signal_peak,0),?),"
                " decoded=CASE WHEN ?=1 THEN 1 ELSE decoded END,"
                " png=COALESCE(?, png),"
                " wav=COALESCE(?, wav),"
                " quality=COALESCE(quality, ?),"
                " satnogs=COALESCE(satnogs, ?)"
                " WHERE id=?",
                (round(signal_peak, 1), 1 if decoded else 0, png_file,
                 wav_file, quality, satnogs_json, row["id"]))
            return
        cur.execute(
            "INSERT INTO passes (sat_name, rise_ts, set_ts, rise_local,"
            " set_local, max_alt, duration_min, frequency_hz, signal_peak,"
            " quality, decoded, wav, png, satnogs, logged_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sat_name, rise_ts, set_ts, rise_local, set_local, max_alt,
             duration_min, int(frequency), round(signal_peak, 1), quality,
             1 if decoded else 0, wav_file, png_file, satnogs_json,
             datetime.now().timestamp()))

def _load_history():
    """The pass history, newest first, in the shape the history page has
    always parsed (the storage moved to the station database; the JSON
    shape is the stable interface)."""
    out = []
    for r in db.query("SELECT * FROM passes ORDER BY id"):
        out.append({
            "sat_name": r["sat_name"],
            "frequency_mhz": round((r["frequency_hz"] or 0) / 1e6, 4),
            "max_alt": r["max_alt"],
            "duration_min": r["duration_min"],
            "rise_local": r["rise_local"],
            "set_local": r["set_local"],
            "rise_ts": r["rise_ts"],
            "set_ts": r["set_ts"],
            "signal_peak": r["signal_peak"],
            "decoded": bool(r["decoded"]),
            "png": r["png"],
            "wav": r["wav"],
            "quality": r["quality"],
            "satnogs": json.loads(r["satnogs"]) if r["satnogs"] else None,
            "timestamp": r["logged_ts"],
        })
    return out

def set_recording_quality(wav_name, quality):
    """Persist a reception-quality value in the matching history entry."""
    with db.write() as cur:
        cur.execute("UPDATE passes SET quality=? WHERE wav=?",
                    (quality, wav_name))
    return True

def quality_map():
    """Map recording filename -> stored reception quality."""
    return {r["wav"]: r["quality"]
            for r in db.query("SELECT wav, quality FROM passes"
                             " WHERE wav IS NOT NULL")}

def recording_path(*parts):
    """Absolute path of a recording artifact by its (sanitized) path
    segments under the recordings tree: the LOCAL capture dir first
    (fresh, still-decoding recordings - see archive.py), then the
    archive. Falls back to the archive path when the local copy is gone
    (or never existed)."""
    local = os.path.join(RECORD_LOCAL_DIR, *parts)
    if os.path.exists(local):
        return local
    return os.path.join(RECORD_DIR, *parts)


def _product_entries(pdir):
    """Every product file of one SatDump product directory, one level
    deep: SatDump v1.2.0 nests the instrument images in a subdirectory
    (<out>/MSU-MR/*.png — full LRPT decodes leave NO image at the top
    level), and the history page's Products list is the only link to
    them. Returns (display name, absolute path) pairs, sorted."""
    out = []
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return out
    for name in names:
        p = os.path.join(pdir, name)
        if os.path.isfile(p):
            out.append((name, p))
        elif os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp):
                    out.append((name + '/' + sub, sp))
    return out


def _scan_recordings(root, qualities, active_wav):
    """The recordings in one directory, in the shape get_recordings()
    has always returned (see there). Split out so the local capture dir
    and the archive can be scanned with the same code."""
    recordings = []
    if os.path.exists(root):
        for f in sorted(os.listdir(root), reverse=True):
            if f.endswith('.wav'):
                path = os.path.join(root, f)
                size = os.path.getsize(path)
                has_png = os.path.exists(path.replace('.wav', '.png'))
                marker = read_decode_marker(path)
                recordings.append({
                    "filename": f,
                    "size_mb": round(size / (1024*1024), 1),
                    "decoded": has_png,
                    "png": f.replace('.wav', '.png') if has_png else None,
                    "quality": qualities.get(f),
                    "decode_attempted": marker is not None,
                    "decode_error": (marker.get('message')
                                     if marker is not None and not marker.get('success')
                                     else None),
                    # A wav that is still being written must not be played or
                    # decoded: its WAV header is stale and the file incomplete
                    "recording_in_progress": bool(active_wav and os.path.abspath(path) == os.path.abspath(active_wav)),
                    # Raw IQ capture next to the WAV (digital modes: LRPT/DSB
                    # are recorded as raw baseband alongside the FM audio),
                    # shown in the UI and deleted WITH the recording.
                    "iq": {"filename": f.replace('.wav', '.iq.u8'),
                           "size_mb": round(os.path.getsize(path.replace('.wav', '.iq.u8'))
                                            / (1024*1024), 1)}
                          if os.path.exists(path.replace('.wav', '.iq.u8')) else None,
                    # Pass waterfall diagram (spectrogram of the raw IQ or
                    # the WAV audio), generated for every recorded pass —
                    # shown in the UI next to the decode image
                    "waterfall": (f.replace('.wav', '.waterfall.png')
                                  if os.path.exists(path.replace('.wav', '.waterfall.png'))
                                  else None),
                    # Every generated decode product (SatDump product
                    # directories next to the recording, e.g. <base>_lrpt/,
                    # including their image subdirectories - see
                    # _product_entries): the history page links them all
                    # instead of one image
                    "products": [
                        {"name": name,
                         "size_kb": round(os.path.getsize(p) / 1024.0, 1),
                         "path": os.path.basename(pdir) + '/' + name}
                        for pdir in sorted(glob.glob(os.path.join(root, f[:-4] + '_*')))
                        if os.path.isdir(pdir)
                        for name, p in _product_entries(pdir)],
                })
    return recordings


def get_recordings():
    """List available recordings (with reception quality where known).

    The fresh recordings sit in the LOCAL capture dir (RECORD_LOCAL_DIR)
    until the archiver moves them to the archive (RECORD_DIR - a network
    share); both are listed, the local copy winning while a family
    straddles the two mid-move. A single-directory setup scans once."""
    qualities = quality_map()
    with state.status_lock:
        active_wav = state.current_wav_path if state.is_recording else None
    local = _scan_recordings(RECORD_LOCAL_DIR, qualities, active_wav)
    if os.path.realpath(RECORD_DIR) == os.path.realpath(RECORD_LOCAL_DIR):
        return local
    merged = {r["filename"]: r
              for r in _scan_recordings(RECORD_DIR, qualities, active_wav)}
    for r in local:
        merged[r["filename"]] = r
    return [merged[k] for k in sorted(merged, reverse=True)]
