"""Tests for noaa_receiver.decoding.pocsag: the BCH(31,21) code, batch
framing and message decoding at bit level, and the RF-level decode_band
(encode -> 2-FSK -> noise -> decode round trip, both polarities, and
rejection of non-POCSAG bands).

Run: python3 -u tests/test_pocsag.py (needs numpy)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                      os.path.join(tempfile.mkdtemp(), "station.db"))

from noaa_receiver.decoding import pocsag  # noqa: E402

# ---- 1. BCH(31,21) + even parity ----
assert pocsag._bch_remainder(pocsag.SC >> 1) == 0, "sync code must be a valid codeword"
assert pocsag._bch_remainder(pocsag.IDLE >> 1) == 0, "idle codeframe must be valid"
w = pocsag.encode_word(1, (123456 << 2) | 0)
assert pocsag._bch_remainder(w >> 1) == 0 and (bin(w).count('1') & 1) == 0
assert pocsag.correct_word(w ^ (1 << 7)) == w, "single flip must correct"
assert pocsag.correct_word(w ^ (1 << 7) ^ (1 << 19)) == w, "double flip must correct"
# 3 flips: beyond the code's radius - may fail or mis-correct, must not crash
pocsag.correct_word(w ^ (1 << 3) ^ (1 << 9) ^ (1 << 25))
print("1. BCH(31,21): sync/idle valid, <=2 flips corrected OK")


# ---- 2. bit-level round trip: numeric + alpha pages ----
def build_transmission(pages, n_batches=3):
    """[(addr, func, text)] -> preamble + batches, idle padded."""
    words = [pocsag.IDLE] * 16
    for addr, func, text in pages:
        f = addr & 7
        assert words[2 * f] == pocsag.IDLE, "frame collision in test layout"
        words[2 * f] = pocsag.encode_word(1, (addr << 2) | func)
        wi = 2 * f + 1
        if func == 3:                      # alpha: 7-bit chars, LSB first
            stream = ''.join(format(ord(c), '07b')[::-1] for c in text)
            stream += '0' * ((-len(stream)) % 20)
            for k in range(0, len(stream), 20):
                words[wi] = pocsag.encode_word(0, int(stream[k:k + 20], 2))
                wi += 1
        else:                              # numeric: 5 chars per word
            for k in range(0, len(text), 5):
                chunk5 = (text[k:k + 5] + '00000')[:5]
                payload = 0
                for j, ch in enumerate(chunk5):
                    payload |= pocsag._NUM.index(ch) << (16 - 4 * j)
                words[wi] = pocsag.encode_word(0, payload)
                wi += 1
        if wi < 16:
            words[wi] = pocsag.IDLE         # terminate the message
    bits = '10' * 288                       # 576-bit preamble
    for _ in range(n_batches):
        bits += format(pocsag.SC, '032b')
        for w32 in words:
            bits += format(w32, '032b')
    return bits


ADDR1, ADDR2 = 123456, 200003               # frames 0 and 3: no collision
tx = build_transmission([(ADDR1, 0, '01234'), (ADDR2, 3, 'HELLO')])
bits_arr = np.array([int(c) for c in tx], dtype=np.int8)
scpos = pocsag._find_sc(bits_arr)
pages, stats = pocsag.decode_frames(tx, list(scpos))
assert stats['batches'] == 3 and stats['bad_words'] == 0, stats
assert stats['idle_words'] > 0, stats
got = {p['addr']: p for p in pages}
assert got[ADDR1]['text'] == '01234' and got[ADDR1]['func'] == 0, pages
assert got[ADDR2]['text'] == 'HELLO' and got[ADDR2]['func'] == 3, pages
assert got[ADDR1]['ric'] == (ADDR1 << 2), pages
print("2. bit-level decode: numeric '01234' + alpha 'HELLO' + idle padding OK")

# ---- 3. RF round trip: 2-FSK + noise -> decode_band ----
fs = 2_400_000
f_bits = np.where(np.array([int(c) for c in tx]) == 1, 2400.0, -2400.0)
f_rep = np.repeat(f_bits, fs // 1200)
ph = 2 * np.pi * np.cumsum(f_rep) / fs
rng = np.random.default_rng(3)
sig = (70.0 * np.exp(1j * ph)).astype(np.complex64)
sig += (rng.normal(0, 6, len(sig))
        + 1j * rng.normal(0, 6, len(sig))).astype(np.complex64)
res = pocsag.decode_band(lambda k0, n: sig[k0:k0 + n], len(sig), 0.0, fs)
assert res is not None, "decoder must lock on the synthetic paging channel"
assert res['batches'] == 3 and res['bad_words'] == 0, res
got = {p['addr']: p for p in res['pages']}
assert got[ADDR1]['text'] == '01234', res['pages']
assert got[ADDR2]['text'] == 'HELLO', res['pages']
print("3. RF round trip: 2-FSK + noise -> both pages recovered OK")

# ---- 4. inverted polarity (conjugate flips every frequency) ----
res2 = pocsag.decode_band(lambda k0, n: np.conj(sig[k0:k0 + n]),
                          len(sig), 0.0, fs)
assert res2 is not None, "both FSK polarities must be tried"
got2 = {p['addr']: p for p in res2['pages']}
assert got2[ADDR1]['text'] == '01234' and got2[ADDR2]['text'] == 'HELLO'
print("4. inverted polarity also decodes OK")

# ---- 5. noise-only band must be rejected ----
noise = (rng.normal(0, 6, fs) + 1j * rng.normal(0, 6, fs)).astype(np.complex64)
assert pocsag.decode_band(lambda k0, n: noise[k0:k0 + n],
                          len(noise), 0.0, fs) is None
print("5. noise-only band rejected OK")

# ---- 6. BandDecoder: two paging channels in one 2.4 MHz capture ----
# The live-role DSP core: channel discovery + parallel decode,
# fed whole IQ batches like pocsag_capture_thread does
fs = 2_400_000
TUNER = 466_060_000.0          # raw-stream center
rng = np.random.default_rng(11)
n_iq = int(4.0 * fs)
band = (rng.normal(0, 6, n_iq) + 1j * rng.normal(0, 6, n_iq)).astype(np.complex64)
tx1 = build_transmission([(123456, 0, '01234')], n_batches=2)
tx2 = build_transmission([(200003, 3, 'HELLO')], n_batches=2)
def fsk_tx(bits, offset_hz, amp=60.0, dev=2400.0):
    f = np.where(np.array([int(c) for c in bits]) == 1, dev, -dev)
    f_rep = np.repeat(f, fs // 1200) + offset_hz
    ph = 2 * np.pi * np.cumsum(f_rep) / fs
    return (amp * np.exp(1j * ph)).astype(np.complex64)
sig1 = fsk_tx(tx1, (466_220_000.0 - TUNER))    # raw +160 kHz
sig2 = fsk_tx(tx2, (465_970_000.0 - TUNER))    # raw -90 kHz
band[:len(sig1)] += sig1[:len(band)]
band[:len(sig2)] += sig2[:len(band)]
dec = pocsag.BandDecoder("test:0", TUNER, fs)
pages_all = []
t_sim = 0.0
for k in range(0, n_iq, 32768):
    t_sim += 32768 / fs
    pages_all.extend(dec.feed(band[k:k + 32768], now=t_sim))
chan_mhz = sorted(dec.channel_status().values(), key=lambda c: c['freq_mhz'])
assert len(chan_mhz) == 2, chan_mhz            # both channels found
freqs = [c['freq_mhz'] for c in chan_mhz]
assert abs(freqs[0] - 465.970) < 0.02 and abs(freqs[1] - 466.220) < 0.02, freqs
got = {}
for p in pages_all:
    got.setdefault(round(p['channel_mhz'], 2), {})[p['addr']] = p
assert 123456 in got.get(466.22, {}) and got[466.22][123456]['text'] == '01234', got
assert 200003 in got.get(465.97, {}) and got[465.97][200003]['text'] == 'HELLO', got
print("6. BandDecoder: two paging channels discovered + both pages decoded OK")

print("ALL POCSAG TESTS PASSED")
os._exit(0)
