"""Recording archiver: capture locally first, move to the share when safe.

The capture threads write every WAV / raw IQ / manual zip to the LOCAL
disk (config.RECORD_LOCAL_DIR) so a network share going away mid-pass
can never kill a recording again (the 2026-10-17 outage). This thread
moves each finished recording family — the WAV plus its png, thumbnail,
raw IQ and SatDump product directories — to the archive
(config.RECORD_DIR, typically the NFS share via PRAWN_RECORD_DIR) once
ALL of these hold:

  - the analysis is done: the recording's decode attempt marker exists
    (success or failure both count — a dark transmitter never decodes
    better on retry), or the recording is old enough (STALE_SECS) that
    no decode is ever coming. A decode RUNNING right now
    (state.decodes_active — synchronous SSTV or the background SatDump
    run, whose spawn-time marker already says 'success') blocks the
    move until it really finished.
  - nothing in the family has been touched for FRESH_SECS: a WAV still
    being written or a SatDump run dropping products always has a
    fresh mtime.
  - the archive is REALLY mounted: after the 2026-10-17 outage (the
    missing NAS made the host-path bind come up over an empty LOCAL
    directory) the archive is only trusted when it sits on a DIFFERENT
    filesystem than the local recording dir.

Leftovers of an interrupted move (a png whose WAV already went over)
are swept on every pass, and the manual/ raw-IQ zips move once they
have been quiet for FRESH_SECS. A single-directory setup (no
PRAWN_RECORD_DIR — the notebook field station) never runs a pass at
all. The dashboard merges both locations while a family straddles them
(see web/history.get_recordings).
"""
import os
import shutil
import time

from . import state
from .config import RECORD_DIR, RECORD_LOCAL_DIR
from .decoding.decode import read_decode_marker
from .web.thumbs import THUMB_SUFFIX

# A file younger than this is "in flight" (WAV being written, SatDump
# dropping products, zip being built) - never archived.
FRESH_SECS = 300.0
# Without a decode marker, a recording this old will never be decoded -
# archive it anyway (manual recordings, non-satellite WAVs).
STALE_SECS = 1800.0
# Archiver cadence: a family sits locally up to this long after decode.
ARCHIVE_INTERVAL_SECS = 60.0

# Flat family members next to the WAV (product directories are globbed
# separately - every <base>_*/ directory belongs to the recording).
_FAMILY_SUFFIXES = ('.wav', '.png', '.waterfall.png', '.iq.u8', '.json',
                    '.analysis.json', THUMB_SUFFIX)


def archive_mounted():
    """True when the archive directory sits on a different filesystem
    than the local recording dir - the only state in which moving is
    both meaningful and safe (same-dir setups never archive; a bind over
    an empty directory after a lost NAS does not count as mounted)."""
    try:
        return os.stat(RECORD_DIR).st_dev != os.stat(RECORD_LOCAL_DIR).st_dev
    except OSError:
        return False


def _family(local_dir, base):
    """Every local member of one recording family: the flat files with
    a known suffix plus all <base>_*/ product directories."""
    members = []
    for suffix in _FAMILY_SUFFIXES:
        p = os.path.join(local_dir, base + suffix)
        if os.path.exists(p):
            members.append(p)
    entries = os.listdir(local_dir)
    for f in entries:
        if f.startswith(base + '_') and os.path.isdir(os.path.join(local_dir, f)):
            members.append(os.path.join(local_dir, f))
    return members


def _ready(local_dir, base, now):
    """Is this family complete, idle and decoded? See the module
    docstring for the three conditions."""
    wav = os.path.join(local_dir, base + '.wav')
    if not os.path.exists(wav):
        return False
    if base in state.decodes_active:
        return False          # a decode is crunching it right now
    members = _family(local_dir, base)
    if not members:
        return False
    if now - max(os.path.getmtime(m) for m in members) < FRESH_SECS:
        return False          # something is still being written
    if read_decode_marker(wav) is None \
            and now - os.path.getmtime(wav) < STALE_SECS:
        return False          # no decode attempted yet - wait for it
    return True


def _move(member, archive_dir):
    """One member over to the archive; False on error (never fatal - the
    next pass retries, and a family half-moved just completes later)."""
    dest = os.path.join(archive_dir, os.path.basename(member))
    try:
        if os.path.exists(dest):
            # A half-done earlier move: equal size means the copy itself
            # completed and only the source removal was missed - drain
            # the local copy. A size MISMATCH is a copy that died
            # mid-write (NFS drop): discard the partial dest, move again.
            if os.path.getsize(dest) == os.path.getsize(member):
                if os.path.isdir(member):
                    import shutil as _sh
                    _sh.rmtree(member)
                else:
                    os.remove(member)
                return True
            os.remove(dest)
        shutil.move(member, dest)
        return True
    except OSError as e:
        state.log_console(f"Archive move failed for "
                          f"{os.path.basename(member)}: {e}", "warn")
        return False


def archive_pass(local_dir=None, archive_dir=None, now=None, force=False):
    """One archiver iteration: move every ready family from the local
    capture dir to the archive. Returns the number of files moved.

    force=True skips the is-the-share-mounted check (same filesystem)
    — for tests and for explicit operator requests, never the
    background thread."""
    local_dir = local_dir or RECORD_LOCAL_DIR
    archive_dir = archive_dir or RECORD_DIR
    now = time.time() if now is None else now
    if os.path.realpath(local_dir) == os.path.realpath(archive_dir):
        return 0
    try:
        if not force and \
                os.stat(archive_dir).st_dev == os.stat(local_dir).st_dev:
            return 0          # archive not mounted (see archive_mounted)
        os.makedirs(archive_dir, exist_ok=True)
        names = os.listdir(local_dir)
    except OSError:
        return 0              # archive or local dir not there right now
    moved = []
    # 1) WAV-anchored families, oldest first
    wavs = sorted(f for f in names
                  if f.endswith('.wav') and not f.startswith('.'))
    guarded = set()           # every member of a family that is NOT ready
    for wav in wavs:
        base = wav[:-len('.wav')]
        members = _family(local_dir, base)
        if _ready(local_dir, base, now):
            for member in members:
                if not _move(member, archive_dir):
                    break     # do not widen a failing family's gap
                moved.append(os.path.basename(member))
        else:
            guarded.update(os.path.basename(m) for m in members)
    # 2) leftover sweep: siblings of families already (half) moved -
    #    a png or product directory whose WAV went over in an earlier,
    #    interrupted pass. Anything not claimed by a present family and
    #    quiet for FRESH_SECS goes over on its own.
    for f in sorted(names):
        if f.startswith('.') or f == 'manual' or f in guarded:
            continue
        p = os.path.join(local_dir, f)
        if not os.path.exists(p):
            continue          # a member this same pass already moved
        if now - os.path.getmtime(p) < FRESH_SECS:
            continue
        if _move(p, archive_dir):
            moved.append(f)
    # 3) manual raw-IQ zips (dashboard Record button) with their
    #    sidecar files: everything quiet in local/manual/
    msrc = os.path.join(local_dir, 'manual')
    if os.path.isdir(msrc):
        for f in sorted(os.listdir(msrc)):
            p = os.path.join(msrc, f)
            if now - os.path.getmtime(p) < FRESH_SECS:
                continue      # zip still building / being downloaded
            os.makedirs(os.path.join(archive_dir, 'manual'), exist_ok=True)
            if _move(p, os.path.join(archive_dir, 'manual')):
                moved.append('manual/' + f)
    if moved:
        state.log_console(f"📦 Archived {len(moved)} file(s) to "
                          f"{archive_dir}: "
                          + ", ".join(moved[:4]) + (" …" if len(moved) > 4 else ""))
    return len(moved)


def archive_thread():
    """Background thread: keep the local capture dir drained into the
    archive. Exits immediately on single-directory setups."""
    if os.path.realpath(RECORD_DIR) == os.path.realpath(RECORD_LOCAL_DIR):
        return
    state.log_console(f"Recording archiver active: captures stay in "
                      f"{RECORD_LOCAL_DIR}, finished recordings move to "
                      f"{RECORD_DIR}")
    while True:
        try:
            archive_pass()
        except Exception as e:
            state.log_console(f"Archiver error: {e}", "error")
        time.sleep(ARCHIVE_INTERVAL_SECS)
