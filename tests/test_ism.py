"""ISM sensor decoding (decoding/ism.py): the live OOK decoder against a
synthetic Nexus PPM stream and the REAL 2026-10-23 433.92 MHz recording
(the four sensors identified by the tools/iq433 analysis).

Run: .venv/bin/python -u tests/test_ism.py (needs numpy; uses the NFS
recording when mounted, else skips the real-capture section).
"""
import glob
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE", os.path.join(tempfile.mkdtemp(), "station.db"))

import numpy as np
from noaa_receiver.decoding.ism import (OokDecoder, _nexus_decode, _pwm48_decode,
                                        _crc8, ism_status, ism_log, _register,
                                        _log_packet)
from noaa_receiver import state, db
from noaa_receiver.config import SDR_OFFSET_HZ
db.query("SELECT 1")   # schema ready against the isolated test DB

# ---- unit: the payload decoders against the 2026-10-23 ground truth ----
# Nexus row verified from the recording: id 0x03, ch3, 19.8 C, 57 %RH
bits03 = '000000111010000011000110111100111001' + '100'[:0]  # 36 bits
d = _nexus_decode(bits03)
assert d and d["id"] == "03" and d["channel"] == 3, d
assert abs(d["temp_c"] - 19.8) < 0.05, d
assert d["humidity"] == 57, d
assert d["battery"] == "ok" and d["proto"] in ("nexus", "rubicson"), d
# negative temperature: sign bit in t0 bit3, e.g. -5.6 C -> t12 = 556 | 0x800
neg = '00000011' + '0' * 0  # build a synthetic negative row below
nib = [0x0, 0x3, 0x0, 0x8, 0xE, 0x2, 0xF, 5, 7]            # -22.6 C (t0 sign bit + 226)
b36 = ''.join('%X' % n for n in nib)
bits = ''.join(format(int(c, 16), '04b') for c in b36)
d = _nexus_decode(bits)
assert d and abs(d["temp_c"] - (-22.6)) < 0.05, d
# battery-low flag: bit3 CLEAR = low (the id-03 row's 0xA = OK)
nib = [0x0, 0x3, 0x0 | 0x0, 0x0, 0xB, 0xE, 0xF, 0, 0]          # 19.0 C, ch1, low
bits = ''.join(format(int(c, 16), '04b') for c in ['%X' % n for n in nib])
d = _nexus_decode(bits)
assert d and d["battery"] == "low" and d["channel"] == 1, d
assert abs(d["temp_c"] - 19.0) < 0.05, d
# the PWM48 device: the recorded payload 4C 80 58 47 5D C8 (sum checksum)
d = _pwm48_decode('01001100' '10000000' '01011000' '01000111' '01011101' '11001000')
assert d and d["id"] == "4C" and abs(d["temp_c"] - 8.8) < 0.05, d
assert d["humidity"] == 71 and d["proto"] == "pwm48", d
# a wrong checksum is rejected
bad = '01001100' '10000000' '01011000' '01000111' '01011101' '11001001'
assert _pwm48_decode(bad) is None
print("ISM payload decoders: Nexus temp/hum/neg/battery + PWM48 sum checksum OK")

# ---- live decoder on a synthetic Nexus PPM stream (480 us pulses) ----
# one 36-bit row: pulse ~470 us, gaps 1000 (0) / 2000 (1) us, then idle.
# The carrier sits at -60 kHz baseband (the tune is SDR_OFFSET_HZ above
# the band), like a real rtl_tcp stream.
def ppm_row_iq(bits, amp=90.0):
    fs = 2400000
    f_bb = -SDR_OFFSET_HZ   # the band lands at -60 kHz in the raw tuner baseband
    pieces_i = []; pieces_q = []
    n_total = 0
    for bit in bits:
        gap = 1000 if bit == '0' else 2000
        n_on = int(470e-6 * fs)
        n_off = int(gap * 1e-6 * fs)
        ph = 2 * np.pi * f_bb * (np.arange(n_on) + n_total) / fs
        pieces_i.append(amp * np.cos(ph) + 127.5)
        pieces_q.append(amp * np.sin(ph) + 127.5)
        n_total += n_on + n_off
        pieces_i.append(np.full(n_off, 127.5))
        pieces_q.append(np.full(n_off, 127.5))
    i_all = np.concatenate(pieces_i); q_all = np.concatenate(pieces_q)
    sig = np.empty(len(i_all) * 2, dtype=np.uint8)
    sig[0::2] = np.clip(i_all, 0, 255).astype(np.uint8)
    sig[1::2] = np.clip(q_all, 0, 255).astype(np.uint8)
    return sig.tobytes()

dec = OokDecoder()
bits03 = '000000111010000011000110111100111001' + '100'  # 36 bits + 3 pad
for rep in range(3):
    dec.feed(ppm_row_iq(bits03), time.time() + rep)
    dec.feed(b'\x7f\x7f' * (2400000 // 2 // 50), time.time() + rep)   # ~20 ms silence
pkts = []
for rep in range(3):
    pkts += dec.feed(ppm_row_iq(bits03), time.time() + rep * 10)
    pkts += dec.feed(b'\x7f\x7f' * (2400000 // 2 // 50), time.time() + rep * 10)   # ~20 ms silence
assert pkts, "synthetic PPM row must decode"
assert len(pkts) == 2, f"repeats within 3 s must be deduped: {len(pkts)}"
p = pkts[0]
assert p["id"] == "03" and p["channel"] == 3, p
assert abs(p["temp_c"] - 19.8) < 0.05, p
assert p["humidity"] == 57, p
print("ISM live decoder: synthetic Nexus PPM row decoded OK")

# ---- the REAL 2026-10-23 recording: all four sensors end-to-end ----
REC = '/mnt/nfs/prawnceiver/recordings/manual/Manual_433.9208MHz_20261023_234155.iq.u8'
if os.path.exists(REC):
    from noaa_receiver.sdr.radio import CAPTURE_BATCH, IQ_BLOCK
    dec = OokDecoder()
    pkts = []
    t0 = time.time()
    with open(REC, 'rb') as f:
        blen = CAPTURE_BATCH * IQ_BLOCK
        while True:
            raw = f.read(blen)
            if not raw:
                break
            pkts += dec.feed(raw, time.time())
    print(f"real recording: {len(pkts)} packets in {time.time()-t0:.1f} s")
    def find(proto=None, id=None):
        for p in pkts:
            if (proto is None or p["proto"] == proto) and (id is None or p["id"] == id):
                return p
        return None
    p03 = find(id="03")
    assert p03, "id 03 sensor must decode: " + str({(p['proto'], p['id']) for p in pkts})
    assert p03["channel"] == 3 and abs(p03["temp_c"] - 19.8) < 0.05 and p03["humidity"] == 57, p03
    p26 = find(id="26")
    assert p26 and p26["channel"] == 1 and abs(p26["temp_c"] - 19.0) < 0.05, p26
    assert p26["humidity"] is None and p26["battery"] == "low", p26
    pA2 = find(id="A2")
    assert pA2 and pA2["channel"] == 1 and abs(pA2["temp_c"] - 7.6) < 0.05, pA2
    p4C = find(proto="pwm48")
    assert p4C and p4C["id"] == "4C" and p4C["hex"].startswith("4C 80 58 47 5D C8"), p4C
    assert abs(p4C["temp_c"] - 8.8) < 0.05 and p4C["humidity"] == 71, p4C
    protos = {}
    for p in pkts:
        protos[p["proto"]] = protos.get(p["proto"], 0) + 1
    print("   sensors:", protos, "| CRC family:", [p["proto"] for p in pkts if p["id"] == "A2"][:1])
    # registry + log plumbing
    for p in pkts[:5]:
        p.setdefault("freq_mhz", 433.92)
        with state.ism_lock:
            _register(p)
    assert state.ism_sensors, "registry must fill"
    st = ism_status()
    assert st["sensors"], st
    for p in pkts[:3]:
        p["freq_mhz"] = 433.92
        _log_packet(p)
    assert len(ism_log(10)) == 3, ism_log(10)
    print("ISM status/log/registry plumbing OK")
else:
    print("real recording not mounted — skipping the end-to-end section")

print("ALL ISM TESTS PASSED")
os._exit(0)
