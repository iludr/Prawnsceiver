"""Checks for noaa_receiver.analysis.dmr — the DMR control-channel decoder.

Run: python3 tests/test_dmr.py (needs numpy). Three layers:
1. codec roundtrips (Hamming 20/8, 13/9, 15/11, BPTC(196,96), CSBK CRC)
2. the encoder must reproduce the real 153.1126 MHz Aloha capture byte
   for byte (captured 2026-10-11, CRC-verified off air)
3. end-to-end: synthesize a repeater downlink (CACH + bursts -> 4FSK
   audio with ISI + noise), decode it back, both polarities.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import numpy as np

from noaa_receiver.analysis import dmr

rng = np.random.default_rng(7)

# --- 1. Hamming(20,8): slot type CC=1, DT=CSBK (the real capture's value) ---
cw = dmr.hamming_20_8_encode(0x13)
info, errs = dmr.hamming_20_8_decode(cw)
assert (info, errs) == (0x13, 0), (info, errs)
for bit in range(20):
    fixed, e2 = dmr.hamming_20_8_decode(cw ^ (1 << bit))
    assert fixed == 0x13 and e2 == 1, (bit, fixed, e2)
print('hamming_20_8: roundtrip + single-bit correction ok')

for enc, dec, n in ((dmr.hamming_13_9_encode, dmr.hamming_13_9_decode, 13),
                    (dmr.hamming_15_11_encode, dmr.hamming_15_11_decode, 15)):
    for _ in range(200):
        d = int(rng.integers(0, 1 << (n - 4)))
        cw = enc(d)
        v, e = dec(cw)
        assert v == d and e == 0
        bad = cw ^ (1 << int(rng.integers(0, n)))
        v, e = dec(bad)
        assert v == d and e == 1, (n, d, v)
print('hamming_13_9 / 15_11: roundtrip + correction ok')

# --- BPTC(196,96): encode -> decode identity, and 1-bit error repair ---
for _ in range(50):
    info96 = list(rng.integers(0, 2, 96).astype(int))
    chan = dmr.bptc_196_96_encode(info96)
    out, fixed = dmr.bptc_196_96_decode(chan)
    assert out == info96 and fixed == 0
    pos = int(rng.integers(1, 196))
    chan[pos] ^= 1
    out, fixed = dmr.bptc_196_96_decode(chan)
    assert out == info96 and fixed >= 1, (pos, fixed)
print('bptc_196_96: roundtrip + single-bit repair ok')

# --- 2. the encoder reproduces the real off-air Aloha packet ---
ALOHA_ONAIR = bytes.fromhex('99000801d50027000000289b')
pkt = dmr.assemble_csbk(lb=1, pf=0, opcode=0x19, fid=0x00,
                        payload8=dmr.aloha_payload())
assert pkt == ALOHA_ONAIR, (pkt.hex(), ALOHA_ONAIR.hex())
c = dmr.parse_csbk(pkt)
assert c['crc_ok'] and c['opcode'] == 0x19 and c['opcode_name'] == 'C_ALOHA'
assert c['system_id'] == 39 and c['reg'] and c['backoff'] == 5 \
    and c['nrand_wait'] == 14 and c['ms_address'] == 0
print('csbk: encoder reproduces the on-air Aloha byte for byte; '
      'System ID 39, reg, backoff 5, nrand_wait 14')

# --- 3. end-to-end through synthesized 4FSK demod audio ---
def synth_stream():
    """8 repeater slots: CACH + Aloha/Idle bursts alternating."""
    slots = []
    for i in range(8):
        if i % 2 == 0:
            info96 = dmr.bytes_to_bits(ALOHA_ONAIR)
            burst = dmr.assemble_burst(dmr.SYNC_WORDS['BS-Data'], 1,
                                       dmr.DT_CSBK, info96)
        else:
            burst = dmr.assemble_burst(dmr.SYNC_WORDS['BS-Data'], 1, 9,
                                       list(rng.integers(0, 2, 96).astype(int)))
        slots.append([0] * dmr.CACH_DIBITS + burst)   # CACH not decoded
    dibs = [d for slot in slots for d in slot]
    return dibs

LEVEL_VALUES = np.array([-3.0, -1.0, 1.0, 3.0]) * 2e-3

def dibs_to_audio(dibs, invert):
    syms = [dmr.DIBIT_TO_LEVEL[d] for d in dibs]
    per = int(48000 / dmr.SYMBOL_RATE)              # 10 samples/symbol
    x = np.repeat(LEVEL_VALUES[syms], per)
    x = np.convolve(x, np.hanning(per) / np.hanning(per).sum(), 'same')
    x = x + rng.normal(0, 1e-4, len(x))
    return -x if invert else x

for invert in (False, True):
    x = dibs_to_audio(synth_stream(), invert)
    res = dmr.decode_audio(x, 48000)
    alohas = [e for e in res['bursts']
              if e.get('dt') == 'CSBK' and e.get('csbk', {}).get('crc_ok')]
    idles = [e for e in res['bursts'] if e.get('dt') == 'Idle']
    assert alohas and idles, (invert, res)
    a = alohas[0]['csbk']
    assert a['opcode'] == 0x19 and a['system_id'] == 39 and a['reg']
    assert res['polarity'] == ('inverted' if invert else 'normal')
    print('end-to-end polarity %s: %d Aloha + %d Idle bursts decoded, '
          'System ID 39, CC 1' % (res['polarity'], len(alohas), len(idles)))

print('test_dmr: all checks passed')
os._exit(0)
