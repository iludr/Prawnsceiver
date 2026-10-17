"""archive.py: local-first recording capture, archive to the share.

Run: python3 tests/test_archive.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver import archive, db, state           # noqa: E402
from noaa_receiver.decoding import decode             # noqa: E402

db.query("SELECT 1")           # schema ready against the isolated test DB
NOW = 2_000_000_000.0          # frozen "now" for every readiness check


def touch(path, age, content=b'x'):
    with open(path, 'wb') as f:
        f.write(content)
    t = NOW - age
    os.utime(path, (t, t))
    return path


def mark(base, success=True, message="ok"):
    with db.write() as cur:
        cur.execute("INSERT OR REPLACE INTO decodes VALUES (?,?,?,?)",
                    (base, 1 if success else 0, message, NOW))


def family(local, base, age, with_iq=True, with_products=True):
    touch(os.path.join(local, base + '.wav'), age, b'RIFF')
    touch(os.path.join(local, base + '.png'), age, b'PNG')
    if with_iq:
        touch(os.path.join(local, base + '.iq.u8'), age)
    if with_products:
        d = os.path.join(local, base + '_lrpt')
        os.makedirs(d, exist_ok=True)
        touch(os.path.join(d, 'composite.png'), age)
        t = NOW - age
        os.utime(d, (t, t))
    return base


def moved(archive_dir, name):
    return os.path.exists(os.path.join(archive_dir, name))


local, share = tempfile.mkdtemp(prefix='arc_local_'), tempfile.mkdtemp(prefix='arc_share_')

# 1. A decoded, quiet family moves over completely
base = family(local, 'Meteor-M_2-4_20261017_142100', age=400)
mark(base)
n = archive.archive_pass(local, share, now=NOW, force=True)
assert n == 4, n       # wav + png + iq + product dir
assert moved(share, base + '.wav') and moved(share, base + '.iq.u8')
assert moved(share, base + '_lrpt')
assert moved(share, base + '_lrpt' + os.sep + 'composite.png')
assert not os.path.exists(os.path.join(local, base + '.wav'))
print("1. decoded family archived: OK")

# 2. A fresh family (still being written) never moves
base = family(local, 'Meteor-M_2-3_20261017_150000', age=10)
mark(base)
assert archive.archive_pass(local, share, now=NOW, force=True) == 0
assert os.path.exists(os.path.join(local, base + '.wav'))
print("2. fresh family kept local: OK")

# 3. No decode marker yet: waits for the auto-decode
base = family(local, 'Meteor-M_2-4_20261017_160000', age=400)
assert archive.archive_pass(local, share, now=NOW, force=True) == 0
# ...but a STALE recording (no decode is ever coming) goes over
t = NOW - (archive.STALE_SECS + 100)
os.utime(os.path.join(local, base + '.wav'), (t, t))
assert archive.archive_pass(local, share, now=NOW, force=True) > 0
assert moved(share, base + '.wav')
print("3. undecoded waits, stale archives: OK")

# 4. A decode running RIGHT NOW blocks the move (state.decodes_active)
base = family(local, 'Meteor-M_2-3_20261017_170000', age=400)
mark(base)
state.decodes_active.add(base)
try:
    assert archive.archive_pass(local, share, now=NOW, force=True) == 0
finally:
    state.decodes_active.discard(base)
assert archive.archive_pass(local, share, now=NOW, force=True) > 0
print("4. in-flight decode blocks, then archives: OK")

# 5. Leftover sweep: an orphaned png (its wav already moved in an
#    interrupted pass) goes over on its own once quiet
base2 = 'Meteor-M_2-4_20261017_180000'
touch(os.path.join(local, base2 + '.png'), age=400)
assert archive.archive_pass(local, share, now=NOW, force=True) == 1
assert moved(share, base2 + '.png')
# ...but a FRESH orphan stays (a zip/product still being written)
touch(os.path.join(local, base2 + '_2.png'), age=10)
assert archive.archive_pass(local, share, now=NOW, force=True) == 0
print("5. leftover sweep (stale only): OK")

# 6. Manual raw-IQ zips move to archive/manual/ once quiet
os.makedirs(os.path.join(local, 'manual'), exist_ok=True)
touch(os.path.join(local, 'manual', 'Manual_137.9125MHz_20261017_190000.zip'), age=400)
touch(os.path.join(local, 'manual', 'Manual_137.9125MHz_20261017_190000.iq.u8'), age=400)
n = archive.archive_pass(local, share, now=NOW, force=True)
assert moved(os.path.join(share, 'manual'),
             'Manual_137.9125MHz_20261017_190000.zip')
assert n == 2, n
print("6. manual zips archived: OK")

# 7. Half-moved retry: a dest that already exists is skipped, not an error
base3 = family(local, 'Meteor-M_2-3_20261017_200000', age=400)
mark(base3)
touch(os.path.join(share, base3 + '.wav'), 0)     # already there
n = archive.archive_pass(local, share, now=NOW, force=True)
assert not os.path.exists(os.path.join(local, base3 + '.wav'))
assert moved(share, base3 + '.png')
print("7. half-moved family completes: OK")

# 8. The mount guard: same-filesystem dirs are "not mounted" without force
base4 = family(local, 'Meteor-M_2-4_20261017_210000', age=400)
mark(base4)
samefs = tempfile.mkdtemp(prefix='arc_samefs_')    # same fs as local
assert archive.archive_pass(local, samefs, now=NOW) == 0
assert os.path.exists(os.path.join(local, base4 + '.wav'))
assert archive.archive_pass(local, local, now=NOW, force=True) == 0   # same dir
print("8. mount guard + same-dir no-op: OK")

# 9. read_decode_marker agreement: the decodes table is location-independent
assert decode.read_decode_marker(os.path.join(share, base + '.wav'))['success']
print("9. decode marker resolves after the move: OK")

print("ALL ARCHIVER TESTS PASSED")
os._exit(0)
