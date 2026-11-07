"""Single background disk-I/O thread for the master process.

Decode paths (ADS-B log per frame, registry saves, ...) and other
operational writes must not run on the waterfall WebSocket loop, the
capture/decode threads or the scheduler: a slow disk (or a hiccuping
NFS share) would stall exactly the paths that must stay live. Callers
hand the work over with submit() and get on with their loop; the one
I/O thread serializes all writes (also sparing SQLite from concurrent
writers).
"""
import queue
import threading

_q = queue.Queue()
_started = False
_lock = threading.Lock()


def submit(fn, *args):
    """Enqueue one function call for the I/O thread (started lazily on
    first use, so embedders/tests need no explicit startup)."""
    start_io_thread()
    _q.put((fn, args))


def flush():
    """Block until everything enqueued so far has been executed."""
    _q.join()


def io_thread():
    while True:
        fn, args = _q.get()
        try:
            fn(*args)
        except Exception:
            pass
        finally:
            _q.task_done()


def start_io_thread():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=io_thread, daemon=True, name='io').start()
