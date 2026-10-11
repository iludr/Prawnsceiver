"""DMR (ETSI TS 102 361) decoder for FM-demodulated audio recordings.

Born 2026-10-11 out of the 153.1126 MHz mystery-signal investigation:
a strong 24/7 carrier turned out to be a DMR Tier III trunked control
channel (C_ALOHA beacons + Idle bursts, System ID 39, color code 1).
The whole chain was implemented here so a future recording can be
decoded without external tools (multimon-ng has no DMR support).

Decode chain:
  FM-demod audio -> 4FSK slicer (4800 sym/s, 4 levels)
  -> sync search (9 words of TS 102 361-1 §9.1.1, both polarities)
  -> slot type Hamming(20,8) -> CC + data type
  -> BPTC(196,96) -> CSBK, CRC-16-CCITT ^ 0x5A5A
  -> Tier III fields (Aloha, broadcast announcements)

Reference implementations used for the bit layouts: GopherTrunk
(github.com/MattCheramie/GopherTrunk, framing + dmr/tier3 packages) and
IanWraith/DMRDecode (C_ALOHA field layout). Validated against a real
off-air capture: sync matches at 0 errors, every repeated CSBK clean.

CLI:
  python -m noaa_receiver.analysis.dmr recording.wav [--json]

The input is the station's own FM-demod WAV (any sample rate, the
slicer resamples by symbol-clock decimation).
"""
import json
import sys
import wave

import numpy as np

SYMBOL_RATE = 4800.0        # 4FSK symbols/s (2 bits per symbol)
SYNC_TOLERANCE = 3          # max wrong dibits for a sync hit (of 24)
SYNC_SKIP = 60              # min dibits between two accepted sync hits

# ---- ETSI TS 102 361-1 §9.1.1: the nine 48-bit sync words ----
SYNC_WORDS = {
    'BS-Voice':     0x755FD7DF75F7,
    'BS-Data':      0xDFF57D75DF5D,
    'MS-Voice':     0x7F7D5DD57DFD,
    'MS-Data':      0xD5D7F77FD757,
    'MS-RC':        0x77D55F7DFD77,
    'DM-Voice-TS1': 0x5D577F7757FF,
    'DM-Voice-TS2': 0x7DFFD5F55D5F,
    'DM-Data-TS1':  0xF7FDD5DDFD55,
    'DM-Data-TS2':  0xD7557F5FF7F5,
}
DATA_SYNCS = ('BS-Data', 'MS-Data', 'DM-Data-TS1', 'DM-Data-TS2')
VOICE_SYNCS = ('BS-Voice', 'MS-Voice', 'DM-Voice-TS1', 'DM-Voice-TS2')

# 4FSK symbol -> dibit (ETSI TS 102 361-1 §4.3): +3->'01', +1->'00',
# -1->'10', -3->'11'. Levels are indexed ascending by deviation:
# 0 = -3, 1 = -1, 2 = +1, 3 = +3.
LEVEL_TO_DIBIT = [3, 2, 0, 1]
DIBIT_TO_LEVEL = [2, 3, 1, 0]

# ---- burst geometry (dibits), TS 102 361-1 §6.4.2 ----
BURST_DIBITS = 132
PAYLOAD_DIBITS = 49        # payload half (98 bits)
SLOTTYPE_DIBITS = 5        # slot-type half (10 bits) before/after sync
SYNC_DIBITS = 24
CACH_DIBITS = 12           # repeater downlink: CACH precedes each burst

DT_NAMES = {0: 'PIHeader', 1: 'VoiceLCHeader', 2: 'TerminatorWithLC',
            3: 'CSBK', 4: 'MBCHeader', 5: 'MBCContinuation', 6: 'DataHeader',
            7: 'Rate1_2', 8: 'Rate3_4', 9: 'Idle', 10: 'Rate1_1'}
DT_CSBK = 3

CSBK_OPNAMES = {0x19: 'C_ALOHA', 0x1C: 'C_AHOY', 0x1E: 'C_ACKVIT',
                0x1F: 'C_RAND', 0x20: 'C_ACKD', 0x21: 'C_ACKU',
                0x26: 'C_NACK', 0x28: 'C_BCAST', 0x2A: 'P_MAINT',
                0x2E: 'P_CLEAR', 0x2F: 'P_PROTECT', 0x30: 'PV_GRANT',
                0x31: 'TV_GRANT', 0x32: 'BTV_GRANT', 0x33: 'PD_GRANT',
                0x34: 'TD_GRANT', 0x39: 'C_MOVE', 0x3D: 'Preamble'}

ANNC_NAMES = {0: 'TSCC announcement', 1: 'CallTimerParms', 2: 'VoteNow',
              3: 'LocalTime', 4: 'MassReg', 5: 'ChanFreq',
              6: 'AdjacentSite', 7: 'GenSiteParams'}

CSBK_CRC_MASK = 0x5A5A     # TS 102 361-1 §B.3.11, Table B.21


# ---------------------------------------------------------------- codecs

def popcount(x):
    return bin(x).count('1')


def _h20_8_parity(data):
    """12 parity bits of the (20,8,7) slot-type code (TS 102 361-1 B.1.1)."""
    masks = (0x4F, 0x68, 0xB4, 0xDA, 0xED, 0xB9, 0x13, 0xC6,
             0xE3, 0x3E, 0x9F, 0x75)
    p = 0
    for k, m in enumerate(masks):
        if popcount(data & m) & 1:
            p |= 1 << (11 - k)
    return p


def hamming_20_8_encode(info):
    """info byte (CC<<4 | DT) -> 20-bit codeword (info in bits 19..12)."""
    data = info & 0xFF
    return (data << 12) | _h20_8_parity(data)


def hamming_20_8_decode(cw):
    """-> (info byte, corrected bit count) or (best guess, -1)."""
    cw &= 0xFFFFF
    best, bestd = 0, 21
    for d in range(256):
        dist = popcount(hamming_20_8_encode(d) ^ cw)
        if dist < bestd:
            best, bestd = d, dist
            if dist == 0:
                break
    return (best, bestd) if bestd <= 3 else (best, -1)


def hamming_13_9_encode(d):
    d &= 0x1FF
    b = lambda i: (d >> i) & 1
    p0 = b(0) ^ b(1) ^ b(3) ^ b(5) ^ b(6)
    p1 = b(0) ^ b(1) ^ b(2) ^ b(4) ^ b(6) ^ b(7)
    p2 = b(0) ^ b(1) ^ b(2) ^ b(3) ^ b(5) ^ b(7) ^ b(8)
    p3 = b(0) ^ b(2) ^ b(4) ^ b(5) ^ b(8)
    return (d << 4) | (p3 << 3) | (p2 << 2) | (p1 << 1) | p0


def hamming_13_9_decode(cw):
    cw &= 0x1FFF
    if (hamming_13_9_encode(cw >> 4) ^ cw) & 0xF == 0:
        return cw >> 4, 0
    for i in range(13):
        f = cw ^ (1 << i)
        if (hamming_13_9_encode(f >> 4) ^ f) & 0xF == 0:
            return f >> 4, 1
    return cw >> 4, -1


def hamming_15_11_encode(d):
    d &= 0x7FF
    b = lambda i: (d >> i) & 1
    p0 = b(0) ^ b(1) ^ b(2) ^ b(3) ^ b(5) ^ b(7) ^ b(8)
    p1 = b(1) ^ b(2) ^ b(3) ^ b(4) ^ b(6) ^ b(8) ^ b(9)
    p2 = b(2) ^ b(3) ^ b(4) ^ b(5) ^ b(7) ^ b(9) ^ b(10)
    p3 = b(0) ^ b(1) ^ b(2) ^ b(4) ^ b(6) ^ b(7) ^ b(10)
    return (d << 4) | (p3 << 3) | (p2 << 2) | (p1 << 1) | p0


def hamming_15_11_decode(cw):
    cw &= 0x7FFF
    if (hamming_15_11_encode(cw >> 4) ^ cw) & 0xF == 0:
        return cw >> 4, 0
    for i in range(15):
        f = cw ^ (1 << i)
        if (hamming_15_11_encode(f >> 4) ^ f) & 0xF == 0:
            return f >> 4, 1
    return cw >> 4, -1


def bptc_196_96_encode(info96):
    """96 info bits (0/1, MSB-first octets) -> 196 channel bits."""
    m = [[0] * 15 for _ in range(13)]
    idx = 0
    for c in range(3, 11):
        m[0][c] = info96[idx]
        idx += 1
    for r in range(1, 9):
        for c in range(11):
            m[r][c] = info96[idx]
            idx += 1
    for r in range(9):
        d = 0
        for c in range(11):
            d |= m[r][c] << c
        cw = hamming_15_11_encode(d)
        m[r][11], m[r][12], m[r][13], m[r][14] = cw & 1, (cw >> 1) & 1, (cw >> 2) & 1, (cw >> 3) & 1
    for c in range(15):
        d = 0
        for r in range(9):
            d |= m[r][c] << r
        cw = hamming_13_9_encode(d)
        m[9][c], m[10][c], m[11][c], m[12][c] = cw & 1, (cw >> 1) & 1, (cw >> 2) & 1, (cw >> 3) & 1
    de = [0] * 196
    de[0] = 0                                    # reserved R bit
    for r in range(13):
        for c in range(15):
            de[r * 15 + c + 1] = m[r][c]
    out = [0] * 196
    for i in range(196):
        out[(i * 181) % 196] = de[i]
    return out


def bptc_196_96_decode(chan):
    """196 channel bits -> (96 info bits, corrected count or -1)."""
    de = [chan[(a * 181) % 196] for a in range(196)]
    m = [[de[r * 15 + c + 1] for c in range(15)] for r in range(13)]
    total, failed = 0, False
    for _ in range(5):
        changed = False
        for c in range(15):                      # column code 13,9
            cw = 0
            for r in range(9):
                cw |= m[r][c] << (r + 4)
            cw |= m[9][c] | m[10][c] << 1 | m[11][c] << 2 | m[12][c] << 3
            data, errs = hamming_13_9_decode(cw)
            if errs == 1:
                total += 1
                changed = True
            elif errs < 0:
                failed = True
            cw2 = hamming_13_9_encode(data)
            for r in range(9):
                m[r][c] = (data >> r) & 1
            m[9][c], m[10][c], m[11][c], m[12][c] = cw2 & 1, (cw2 >> 1) & 1, (cw2 >> 2) & 1, (cw2 >> 3) & 1
        for r in range(9):                       # row code 15,11
            cw = 0
            for c in range(11):
                cw |= m[r][c] << (c + 4)
            cw |= m[r][11] | m[r][12] << 1 | m[r][13] << 2 | m[r][14] << 3
            data, errs = hamming_15_11_decode(cw)
            if errs == 1:
                total += 1
                changed = True
            elif errs < 0:
                failed = True
            cw2 = hamming_15_11_encode(data)
            for c in range(11):
                m[r][c] = (data >> c) & 1
            m[r][11], m[r][12], m[r][13], m[r][14] = cw2 & 1, (cw2 >> 1) & 1, (cw2 >> 2) & 1, (cw2 >> 3) & 1
        if not changed:
            break
    info = [m[0][c] for c in range(3, 11)]
    for r in range(1, 9):
        info += [m[r][c] for c in range(11)]
    return info, (-1 if failed else total)


def crc_ccitt(data, init=0):
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ------------------------------------------------------------- bit helpers

def bytes_to_bits(data):
    return [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]


def bits_to_bytes(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for j in range(8):
            b = (b << 1) | (bits[i + j] & 1)
        out.append(b)
    return bytes(out)


def dibits_to_bits(ds):
    out = []
    for d in ds:
        out.append((int(d) >> 1) & 1)
        out.append(int(d) & 1)
    return out


def bits_to_dibits(bits):
    return [(bits[i] << 1) | bits[i + 1] for i in range(0, len(bits), 2)]


def word_to_dibit_pat(word):
    """48-bit sync word -> 24 dibits, MSB-first."""
    return [(word >> (2 * (23 - i))) & 3 for i in range(24)]


# --------------------------------------------------------------- CSBK layer

def assemble_csbk(lb, pf, opcode, fid, payload8):
    """-> 12 info bytes with the CRC trailer attached."""
    out = bytearray(12)
    if lb:
        out[0] |= 0x80
    if pf:
        out[0] |= 0x40
    out[0] |= opcode & 0x3F
    out[1] = fid & 0xFF
    out[2:10] = payload8
    crc = crc_ccitt(bytes(out[:10])) ^ CSBK_CRC_MASK
    out[10] = crc >> 8
    out[11] = crc & 0xFF
    return bytes(out)


def parse_csbk(info12):
    """-> dict or None on CRC mismatch (fields still parsed)."""
    stored = (info12[10] << 8) | info12[11]
    ok = stored == (crc_ccitt(info12[:10]) ^ CSBK_CRC_MASK)
    p = info12[2:10]
    bits = bytes_to_bits(p)
    def take(a, n):                              # payload bits a..a+n-1
        v = 0
        for i in range(n):
            v = (v << 1) | bits[a + i]
        return v
    out = {'lb': bool(info12[0] & 0x80), 'pf': bool(info12[0] & 0x40),
           'opcode': info12[0] & 0x3F, 'fid': info12[1], 'payload': p.hex(),
           'crc_ok': ok}
    name = CSBK_OPNAMES.get(out['opcode'])
    if name:
        out['opcode_name'] = name
    if out['opcode'] == 0x19:                    # C_ALOHA (DMRDecode layout)
        out.update({'infill': bits[6], 'active_connection': bits[7],
                    'mask': take(8, 5), 'service_function': take(13, 2),
                    'nrand_wait': take(15, 4), 'reg': bool(bits[19]),
                    'backoff': take(20, 4), 'system_id': take(24, 16),
                    'ms_address': take(40, 24)})
    elif out['opcode'] == 0x28:                  # C_BCAST
        at = p[0] >> 3
        out['anncd_type'] = at
        out['anncd_name'] = ANNC_NAMES.get(at, 'type%d' % at)
        if at == 7:                              # general site parameters
            out.update({'system_id': int.from_bytes(p[1:3], 'big'),
                        'rfss_id': p[3], 'site_id': p[4], 'net_mask': p[5]})
        elif at == 6:                           # adjacent site
            out.update({'neighbor_system_id': int.from_bytes(p[1:3], 'big'),
                        'neighbor_site_id': int.from_bytes(p[3:5], 'big'),
                        'lcn': int.from_bytes(p[5:7], 'big'),
                        'color_code': p[7] >> 4})
    return out


def aloha_payload(reserved=0b000010, infill=0, active=0, mask=0,
                  service_function=0, nrand_wait=14, reg=1, backoff=5,
                  system_id=39, ms_address=0):
    """Build the C_ALOHA 8-octet payload from its fields.

    Bit layout per DMRDecode (CSBKO 0x19): reserved(6) infill(1)
    active(1) mask(5) service_function(2) nrand_wait(4) reg(1)
    backoff(4) system_id(16) ms_address(24).
    Defaults reproduce the Regensburg 153.1126 MHz capture byte for byte.
    """
    bits = [0] * 64
    def put(v, pos, n):
        for i in range(n):
            bits[pos + i] = (v >> (n - 1 - i)) & 1
    put(reserved, 0, 6)
    bits[6] = infill
    bits[7] = active
    put(mask, 8, 5)
    put(service_function, 13, 2)
    put(nrand_wait, 15, 4)
    bits[19] = reg
    put(backoff, 20, 4)
    put(system_id, 24, 16)
    put(ms_address, 40, 24)
    return bytes(bits_to_bytes(bits))


# ------------------------------------------------------- burst assembly/parse

def assemble_slot_type(color_code, data_type):
    """-> two 5-dibit halves of the 20-bit slot-type codeword."""
    cw = hamming_20_8_encode(((color_code & 0xF) << 4) | (data_type & 0xF))
    bits = [(cw >> (19 - i)) & 1 for i in range(20)]
    return bits_to_dibits(bits[:10]), bits_to_dibits(bits[10:])


def assemble_burst(sync_word, color_code, data_type, info96):
    """-> 132 dibits of a data/control burst."""
    chan = bptc_196_96_encode(info96)
    p1 = bits_to_dibits(chan[:98])
    p2 = bits_to_dibits(chan[98:])
    st1, st2 = assemble_slot_type(color_code, data_type)
    return (p1 + st1 + word_to_dibit_pat(sync_word) + st2 + p2)


def parse_slot_type(dibs):
    """20 slot-type bits around a sync -> (cc, dt, corrected) or None."""
    st = dibits_to_bits(dibs[:SLOTTYPE_DIBITS]) + dibits_to_bits(
        dibs[SLOTTYPE_DIBITS + SYNC_DIBITS:SLOTTYPE_DIBITS + SYNC_DIBITS + SLOTTYPE_DIBITS])
    cw = 0
    for b in st:
        cw = (cw << 1) | b
    info, errs = hamming_20_8_decode(cw)
    if errs < 0:
        return None
    return (info >> 4) & 0xF, info & 0xF, errs


# ------------------------------------------------------------------ decoder

def slice_symbols(x, fs):
    """FM-demod audio -> (symbols as level indices 0..3, cluster centers).

    Samples one point per 4FSK symbol, scanning all phase offsets for
    the cleanest 4-level separation.
    """
    step = fs / SYMBOL_RATE
    n = int(len(x) / step) - 2
    best = None
    for ph in range(max(1, int(step))):
        idx = (np.arange(n) * step + ph).astype(int)
        v = x[idx]
        centers = np.quantile(v, [0.125, 0.375, 0.625, 0.875])
        for _ in range(3):                       # Lloyd refinement
            assign = np.argmin(np.abs(v[:, None] - centers[None, :]), axis=1)
            newc = centers.copy()
            for k in range(4):
                sel = v[assign == k]
                if len(sel):
                    newc[k] = sel.mean()
            if np.allclose(newc, centers):
                break
            centers = newc
        d = np.min(np.abs(v[:, None] - centers[None, :]), axis=1)
        score = d.mean() / (v.std() + 1e-12)
        if best is None or score < best[0]:
            best = (score, ph, centers)
    _, ph, centers = best
    idx = (np.arange(n) * step + ph).astype(int)
    v = x[idx]
    sym = np.argmin(np.abs(v[:, None] - centers[None, :]), axis=1)
    return sym, centers


def find_sync_hits(dibs, tol=SYNC_TOLERANCE):
    """-> {sync_name: [dibit indices]} for all 9 words at one polarity."""
    dd = np.asarray(dibs, dtype=np.int8)
    out = {}
    win = np.lib.stride_tricks.sliding_window_view(dd, 24)
    for name, word in SYNC_WORDS.items():
        pat = np.array(word_to_dibit_pat(word), dtype=np.int8)
        bad = (win != pat).sum(axis=1)
        hits = np.where(bad <= tol)[0]
        keep = []
        for i in hits:
            if not keep or i - keep[-1] >= SYNC_SKIP:
                keep.append(int(i))
        if keep:
            out[name] = keep
    return out


def _score_polarity(dibs, hits):
    """Mean slot-type Hamming errors over data-sync hits (lower = better)."""
    errs = []
    for name in DATA_SYNCS:
        for s in hits.get(name, [])[:20]:
            burst = dibs[s - 54:s + 29]
            if len(burst) < 83:
                continue
            r = parse_slot_type(burst[49:83])
            if r:
                errs.append(r[2])
    return sum(errs) / len(errs) if errs else None


def decode_symbols(sym):
    """Level-index symbol stream -> decode result dict."""
    base = np.array([LEVEL_TO_DIBIT[s] for s in sym], dtype=np.int8)
    cands = []
    for rot in (0, 2):                           # polarity closure of syncs
        dibs = (base + rot) % 4
        hits = find_sync_hits(dibs)
        score = _score_polarity(dibs, hits)
        if score is not None:
            cands.append((score, rot, dibs, hits))
    if not cands:
        return {'polarity': None, 'sync': None, 'bursts': [], 'symbols': len(sym)}
    cands.sort(key=lambda c: c[0])
    score, rot, dibs, hits = cands[0]
    events = []
    for name in DATA_SYNCS + VOICE_SYNCS:
        for s in hits.get(name, []):
            t = s / SYMBOL_RATE
            if name in VOICE_SYNCS:
                events.append({'t': round(t, 3), 'sync': name, 'dt': 'voice'})
                continue
            burst = dibs[s - 54:s + 78]
            if len(burst) < BURST_DIBITS:
                continue                          # truncated at stream edge
            r = parse_slot_type(burst[49:83])
            if r is None:
                events.append({'t': round(t, 3), 'sync': name, 'dt': 'bad'})
                continue
            cc, dt, errs = r
            evt = {'t': round(t, 3), 'sync': name, 'cc': cc,
                   'dt': DT_NAMES.get(dt, 'dt%d' % dt), 'slottype_errors': errs}
            if dt == DT_CSBK:
                payload = dibits_to_bits(burst[:49]) + dibits_to_bits(burst[83:132])
                info, fecerr = bptc_196_96_decode(payload)
                pkt = bits_to_bytes(info)
                evt['csbk'] = parse_csbk(pkt)
                evt['csbk']['raw'] = pkt.hex()
                evt['csbk']['fec_corrected'] = fecerr
            events.append(evt)
    events.sort(key=lambda e: e['t'])
    return {'polarity': 'normal' if rot == 0 else 'inverted',
            'sync': [n for n in hits] or None,
            'slottype_error_rate': score, 'bursts': events,
            'symbols': len(sym)}


def decode_audio(x, fs):
    sym, centers = slice_symbols(x, fs)
    return decode_symbols(sym)


def load_wav(path):
    with wave.open(path) as w:
        fs = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float64)
        if w.getnchannels() == 2:
            x = x.reshape(-1, 2).mean(1)
    return x / 32768.0, fs


# ---------------------------------------------------------------- reporting

def report(result):
    lines = ['📻 DMR decode — %d symbols, polarity %s, syncs: %s' % (
        result['symbols'], result['polarity'],
        ', '.join(result['sync']) if result['sync'] else 'none')]
    bursts = result['bursts']
    counts = {}
    for e in bursts:
        counts[e['dt']] = counts.get(e['dt'], 0) + 1
    lines.append('bursts: %d  (%s)' % (len(bursts), ', '.join(
        '%s %d' % kv for kv in sorted(counts.items()))))
    uniq = {}
    for e in bursts:
        if e.get('dt') != 'CSBK' or not e.get('csbk'):
            continue
        c = e['csbk']
        key = (c['opcode'], c['raw'])
        uniq.setdefault(key, [0, c])
        uniq[key][0] += 1
    for (op, raw), (n, c) in sorted(uniq.items(), key=lambda kv: -kv[1][0]):
        name = c.get('opcode_name', 'op_%02X' % op)
        line = '  %-10s x%-4d %s  CRC %s' % (name, n, raw,
                                             'ok' if c['crc_ok'] else 'FAIL')
        if c['opcode'] == 0x19:
            line += ('\n      System ID %d · reg %s · backoff %d · nrand_wait %d'
                     % (c['system_id'], 'required' if c['reg'] else 'no',
                        c['backoff'], c['nrand_wait']))
        elif c['opcode'] == 0x28:
            line += '\n      %s' % c.get('anncd_name', '?')
            if 'system_id' in c:
                line += ' · System ID %d · Site ID %d' % (c['system_id'], c['site_id'])
        lines.append(line)
    for e in bursts:
        if e.get('dt') in ('DataHeader', 'Rate1_2', 'Rate3_4', 'Rate1_1'):
            lines.append('  data burst: %s at t=%.2fs (CC %d)' % (
                e['dt'], e['t'], e.get('cc', -1)))
            break
    return '\n'.join(lines)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    as_json = '--json' in argv
    paths = [a for a in argv[1:] if not a.startswith('--')]
    for path in paths:
        x, fs = load_wav(path)
        res = decode_audio(x, fs)
        if as_json:
            print(json.dumps(res, indent=1))
        else:
            print(report(res))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
