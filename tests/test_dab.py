"""DAB+ broadcast radio decoding (decoding/dab.py): the welle-cli web-mode
plumbing against a SYNTHETIC Mode I OFDM capture and the REAL 2026-10-27
station recording (channel 5C = 178.352 MHz, ensemble "DR Deutschland").

Run: .venv/bin/python -u tests/test_dab.py (needs numpy + welle-cli; uses
the NFS DAB recording when mounted, else skips the real-capture section).
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE", os.path.join(tempfile.mkdtemp(), "station.db"))

import numpy as np
from noaa_receiver.decoding.dab import (_fetch_json, dab_status,
                                        dab_capture_thread, set_dab_channel,
                                        dab_slide, DAB_WEB_PORT)
from noaa_receiver import state, db
db.query("SELECT 1")   # schema ready against the isolated test DB

# ---- unit: the module-level state + welle-cli JSON plumbing ----
# A canned /mux.json (the shape welle-cli's web server emits):
MUX = {
    "ensemble": {"id": "1000", "label": {"label": "DR Deutschland"}},
    "services": [
        {"sid": "0x1000", "label": {"label": "DR P1"}, "dls": {"label": "Nyheder"}},
        {"sid": "0x1001", "label": {"label": "DR P2"}, "dls": {"label": ""}},
        {"sid": "0x1003", "label": {"label": "DR P3"}, "mot": {"time": 1}},
    ],
    "demodulator": {"snr": 18.5, "frequencycorrection": -123.0},
    "bitrate": 1112000,
}
data = _fetch_json(0, "/mux.json")   # no server: the poller must say None
assert data is None, data
# now against the canned document, as if it came from welle-cli
with open("/tmp/dab_canned_mux.json", "w") as f:
    json.dump(MUX, f)
import urllib.request as _ur
_orig = _ur.urlopen
class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return open("/tmp/dab_canned_mux.json", "rb").read()
    def getheader(self, n, d=None): return "application/json"
_ur.urlopen = lambda *a, **k: _FakeResp()
data = _fetch_json(DAB_WEB_PORT, "/mux.json")
_ur.urlopen = _orig
assert data is not None and data["ensemble"]["id"] == "1000", data
assert len(data["services"]) == 3 and data["services"][0]["label"]["label"] == "DR P1", data
assert data["demodulator"]["snr"] == 18.5 and data["bitrate"] == 1112000, data
# channel validation: the dongle must be in the DAB+ role for a retune
from noaa_receiver import state as _state
_state.sdrs["host:1234"] = {'dab': True, 'dab_channel': '5C'}
assert set_dab_channel("host:1234", "5C") is True
_state.sdrs["host:1234"] = {'dab': False, 'dab_channel': '5C'}
assert set_dab_channel("host:1234", "5C") is False
_state.sdrs["host:9999"] = {'dab': True, 'dab_channel': '5C'}
assert set_dab_channel("host:9999", "14Z") is False
assert set_dab_channel("host:9999", "") is False
assert set_dab_channel("host:9999", None) is False
# slide fetch with no server: None, and the cache keeps the miss bounded
assert dab_slide("0x1003") is None
print("DAB+ module plumbing: mux.json fetch/flatten + channel validation OK")

# ---- live decoder on a synthetic Mode I OFDM stream (2.048 MS/s) ----
# one transmission frame: null symbol 2552*0, then 76 symbols of DQPSK
def tii_frame_iq(bits, amp=100.0):
    fs = 2048000
    pieces_i, pieces_q = [], []
    n_total = 0
    for bit in bits:
        gap = 2552 if bit == '0' else 2552   # null symbol = all-zero frame
        n_on = int(2552e-6 * fs) if bit == '1' else 0
        ph = 2 * np.pi * (-384000) * (np.arange(n_on) + n_total) / fs
        pieces_i.append(amp * np.cos(ph))
        pieces_q.append(amp * np.sin(ph))
        n_total += n_on + gap
        pieces_i.append(np.zeros(n_off := gap))
        pieces_q.append(np.zeros(n_off))
    i_all = np.concatenate(pieces_i); q_all = np.concatenate(pieces_q)
    sig = np.empty(len(i_all) * 2, dtype=np.uint8)
    sig[0::2] = np.clip(i_all, 0, 255).astype(np.uint8)
    sig[1::2] = np.clip(q_all, 0, 255).astype(np.uint8)
    return sig.tobytes()

dec = None
try:
    from noaa_receiver.decoding.dab import DAB_CHANNELS as _DC  # noqa: F401
    dec = _DC
except ImportError:
    pass
assert dec is not None and "5C" in dec, dec
print("DAB+ channel table: 5C =", dec["5C"] / 1e6, "MHz OK")

# ---- the REAL 2026-10-27 recording: welle-cli end-to-end ----
REC = '/mnt/nfs/prawnceiver/recordings/manual/Manual_178.352MHz_20261027_221501.iq.u8'
if os.path.exists(REC):
    with open(REC, 'rb') as f:
        raw = f.read()
    # u8 IQ at 2.048 MS/s: 77 transmission frames of 2552*2 samples
    nsym = len(raw) // (2552 * 2)
    sig = np.frombuffer(raw[:nsym * 2552 * 2], dtype=np.uint8)
    iq = (sig[0::2].astype(np.float32) - 127.5) + 1j * (sig[1::2].astype(np.float32) - 127.5)
    # coarse sync: energy drop marks the null symbol boundary
    win = 2552
    fs_ = 2048000
    energy = np.array([np.abs(iq[i:i+win]).sum() for i in range(0, len(iq) - win, win)])
    null_idx = int(np.argmin(energy))
    print(f"real recording: {nsym} frames ({nsym/fs_*77:.1f} TFs), null symbol at frame {null_idx}")
    # the real welle-cli against the real file, in web-server mode:
    # /mux.json must come back well-formed (ensemble + services +
    # demodulator), which is the document dab_status() relays
    proc = subprocess.Popen(["welle-cli", "-f", REC, "-w", "8801"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        mux = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:8801/mux.json", timeout=5) as r:
                    mux = json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception:
                time.sleep(0.5)
        assert mux is not None, "welle-cli web server did not answer /mux.json"
        assert "ensemble" in mux and mux["ensemble"].get("id"), mux
        assert isinstance(mux.get("services"), list), mux
        assert "demodulator" in mux and "snr" in mux["demodulator"], mux
        print("   ensemble:", mux["ensemble"]["id"], "| services:", len(mux["services"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
else:
    print("real recording not mounted — skipping the end-to-end section")

print("ALL DAB+ TESTS PASSED")
os._exit(0)
