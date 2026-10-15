"""POCSAG pager protocol decoder (ITU-R R-REC-M.584 / ETSI ETS 300 401).

POCSAG rides direct 2-FSK (1200 baud everywhere in the European
466.0-466.3 MHz paging downlink) and frames pages in batches:

- preamble: >= 576 bits of alternating 1010... (receiver clock sync)
- batch: the 32-bit sync code 0x7CD215D8 followed by 16 codeframes -
  544 bits total (empirically confirmed on a live 466.2 MHz paging
  channel 2026-10-16: sync words every 544 bits; the 16 codeframes
  form the spec's 8 FRAMES of two codeframes each)
- codeframe: 32 bits = flag(1) + payload(20) + BCH(31,21) check(10) +
  even parity(1); g(x) = x^10+x^9+x^8+x^6+x^5+x^3+1 (0x769)
- address codeframe: flag 1, payload = 18 address bits + 2 function
  bits; the addressed frame pair = address & 7, so an address lands
  in codeframe 2*(address & 7) of a batch
- message codeframe: flag 0, payload = data; a message continues
  across codeframes and batches until the next address or the idle
  codeframe 0x7A89C197 (padding)
- numeric messages: 5 chars x 4 bits per codeframe; alphanumeric:
  7-bit ASCII packed LSB-first, continuously across codeframes

Used by the recording pre-flight (analysis/iqscan.py): every narrowband
non-OOK emitter gets a decode attempt - the continuously-keyed idle
code identifies a paging channel even when nobody is being paged.
Pages report 'addr' (18 address bits), 'func' (0 numeric, 1/2 tone,
3 alpha) and 'ric' (the usually-quoted pager identity,
addr*4 + func).
"""
import numpy as np

SC = 0x7CD215D8
IDLE = 0x7A89C197
_BCH_G = 0x769                      # x^10+x^9+x^8+x^6+x^5+x^3+1
_SC_BITS = np.array([int(c) for c in format(SC, '032b')], dtype=np.int8)
_NUM = '0123456789U -][.'           # numeric charset (15 = reserved)
MAX_SECS = 30                       # decode at most this much per band


def _bch_remainder(w31):
    """Remainder of a 31-bit word mod g(x); 0 = valid codeword."""
    for i in range(30, 9, -1):
        if (w31 >> i) & 1:
            w31 ^= _BCH_G << (i - 10)
    return w31


def encode_word(flag, payload20):
    """Build one 32-bit codeframe: BCH(31,21)-encode the 21-bit
    flag+payload, append even parity."""
    msg21 = ((flag & 1) << 20) | (payload20 & 0xFFFFF)
    w = msg21 << 10
    for i in range(30, 9, -1):
        if (w >> i) & 1:
            w ^= _BCH_G << (i - 10)
    word31 = (msg21 << 10) | (w & 0x3FF)
    return (word31 << 1) | (bin(word31).count('1') & 1)


def correct_word(word32):
    """BCH-verify one 32-bit codeframe and correct up to 2 flipped
    bits (brute force over 1- and 2-bit patterns - exact for the
    code's 2-bit correction radius). None when uncorrectable."""
    w31 = word32 >> 1
    if _bch_remainder(w31) == 0:
        return (w31 << 1) | (bin(w31).count('1') & 1)
    for i in range(31):
        w = w31 ^ (1 << i)
        if _bch_remainder(w) == 0:
            return (w << 1) | (bin(w).count('1') & 1)
    for i in range(30):
        for j in range(i + 1, 31):
            w = w31 ^ (1 << i) ^ (1 << j)
            if _bch_remainder(w) == 0:
                return (w << 1) | (bin(w).count('1') & 1)
    return None


def _decode_numeric(payload20):
    """5 chars x 4 bits, first char in the highest nibble."""
    return ''.join(_NUM[(payload20 >> (16 - 4 * k)) & 0xF] for k in range(5))


def _finish_alpha(alpha_words):
    """7-bit ASCII packed LSB-first across codeframes; drop padding
    and non-printables."""
    stream = ''.join(alpha_words)
    out = []
    for i in range(0, len(stream) - 6, 7):
        ch = sum(int(stream[i + j]) << j for j in range(7))
        if 32 <= ch < 127:
            out.append(chr(ch))
    return ''.join(out).rstrip()


def decode_frames(bitstr, sc_positions):
    """Decode POCSAG batches from a symbol-aligned bit string
    (MSB-first words, one polarity already chosen) at the given sync
    code positions. Returns (pages, stats)."""
    pages, stats = [], {'batches': 0, 'idle_words': 0, 'bad_words': 0}
    cur, alpha = None, []

    def finish():
        if cur is not None:
            if cur['type'] == 'alpha' and alpha:
                cur['text'] = _finish_alpha(alpha)
            pages.append(cur)

    n = len(bitstr)
    last = -1
    for i in sorted(sc_positions):
        if i <= last:                       # overlapping detection
            continue
        stats['batches'] += 1
        w = i + 32
        end = min(w + 16 * 32, n)
        while w + 32 <= end:
            word = int(bitstr[w:w + 32], 2)
            w += 32
            cw = correct_word(word)
            if cw is None:
                stats['bad_words'] += 1
                finish()
                cur, alpha = None, []
                continue
            if cw == IDLE:
                stats['idle_words'] += 1
                finish()
                cur, alpha = None, []
                continue
            payload = (cw >> 11) & 0xFFFFF
            if (cw >> 31) & 1:              # address codeframe
                finish()
                addr, func = payload >> 2, payload & 3
                cur = {'addr': addr, 'func': func,
                       'ric': (addr << 2) | func,
                       'type': {0: 'numeric', 1: 'tone', 2: 'tone',
                                3: 'alpha'}.get(func, 'numeric'),
                       'text': ''}
                alpha = []
            elif cur is not None:           # message data
                if cur['type'] == 'numeric':
                    cur['text'] += _decode_numeric(payload)
                else:
                    alpha.append(format(payload, '020b'))
        last = i + 32                       # next SC must be past this
    finish()
    # exact-duplicate pages (overlapping batches) collapse
    seen, out = set(), []
    for p in pages:
        key = (p['ric'], p['text'])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out, stats


def _find_sc(bits, maxerr=2):
    """Positions where the sync code matches within maxerr bits."""
    b = (bits * 2 - 1).astype(np.int8)
    corr = np.correlate(b, _SC_BITS * 2 - 1, 'valid')
    return np.where((32 - corr) // 2 <= maxerr)[0]


def decode_band(getc, n_cplx, offset_hz, fs, baud=1200):
    """Attempt a POCSAG decode of one narrowband emitter's band from
    the raw IQ stream (getc memmap interface, analysis/iqscan.py).

    FM-discriminates the band (chunked, decimated to ~19200 Hz), tries
    both FSK polarities and all symbol phases, locks on the sync code
    and decodes the batches. Returns a JSON-ready dict when the
    protocol is detected, None when no sync code locks."""
    if baud != 1200:
        return None
    m = fs // 19200
    if m < 1:
        return None
    sps = fs / m / baud
    if sps < 4:
        return None
    sps = int(round(sps))
    # Channel selectivity FIRST: a wideband FM discriminator would ride
    # whatever signal is strongest anywhere in the capture (the live
    # 466 MHz recording has a +2 dBFS paging channel that bled into
    # every offset). Low-pass +-25 kHz around the shift before
    # discriminating.
    taps = np.sinc(2 * 25_000 / fs * (np.arange(33) - 16)) * np.hamming(33)
    taps /= taps.sum()
    chunks = []
    k = 0
    chunk = m * 4000                       # exactly 4000 decimated samples
    while k + chunk + 1 <= n_cplx \
            and len(chunks) * 4000 < 19200 * MAX_SECS:
        x = getc(k, chunk + 1)
        n0 = np.arange(k, k + chunk + 1, dtype=np.float64)
        bb = x * np.exp(-2j * np.pi * offset_hz * n0 / fs)
        bb = np.convolve(bb, taps, 'same')
        d = np.angle(bb[1:] * np.conj(bb[:-1])) * fs / (2 * np.pi)
        usable = (len(d) // m) * m
        chunks.append(d[:usable].reshape(-1, m).mean(axis=1))
        k += chunk
    if not chunks:
        return None
    f = np.concatenate(chunks)
    f -= np.median(f)
    best = None
    for inv in (0, 1):
        s = (f > 0).astype(np.int8) if not inv else (f < 0).astype(np.int8)
        for ph in range(sps):
            bits = s[ph::sps]
            if len(bits) < 700:
                continue
            scpos = _find_sc(bits)
            if len(scpos) and (best is None or len(scpos) > len(best[2])):
                best = (inv, ph, scpos, bits)
    if best is None:
        return None
    inv, ph, scpos, bits = best
    bitstr = ''.join('1' if v else '0' for v in bits)
    pages, stats = decode_frames(bitstr, list(scpos))
    out = {'baud': baud,
           'batches': stats['batches'],
           'idle_words': stats['idle_words'],
           'bad_words': stats['bad_words'],
           'pages': pages[:20],
           'note': ("POCSAG paging channel (sync code locked; idle words "
                    "mean nobody was paged during the capture). 'ric' is "
                    "the usually-quoted pager identity (address*4 + "
                    "function).")}
    return out
