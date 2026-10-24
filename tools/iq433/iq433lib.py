"""Shared helpers for offline analysis of prawnceiver raw IQ captures.

A capture is an interleaved unsigned 8-bit I/Q stream (.iq.u8, DC level
127.5, 2.4 Msps complex) centered on the dongle's tuner frequency
(radio.tuner_freq_hz in the sidecar .json next to the file). Absolute RF
frequency = baseband offset + tuner_freq_hz; the dashboard display value
is absolute RF minus offset_tuning_hz minus frequency_correction_hz.

All DSP reuses noaa_receiver.sdr.dsp (frequency_shift / lowpass /
decimate_cont) so block-wise processing is phase-continuous, exactly like
the live demod paths.
"""
import json
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from noaa_receiver.sdr.dsp import frequency_shift, lowpass, new_state, decimate_cont  # noqa: E402

FS = 2_400_000
DC = 127.5


# ---------- capture file ----------

def sidecar(iq_path):
    """The .json metadata sidecar that sits next to a recording."""
    base = iq_path
    for suffix in ('.iq.u8', '.raw', '.u8'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    with open(base + '.json') as f:
        return json.load(f)


def tuner_hz(iq_path, override=None):
    """Raw-stream center frequency (Hz). Override wins, else the sidecar."""
    if override:
        return float(override)
    return float(sidecar(iq_path)['radio']['tuner_freq_hz'])


def open_iq(iq_path):
    """Read-only memmap of the interleaved u8 IQ stream."""
    return np.memmap(iq_path, dtype=np.uint8, mode='r')


def nsamples(mm):
    return len(mm) // 2


def getc(mm, k0, n):
    """Complex64 slice [k0, k0+n) of the capture."""
    b = np.asarray(mm[2 * k0:2 * (k0 + n)])
    return ((b[0::2].astype(np.float32) - DC)
            + 1j * (b[1::2].astype(np.float32) - DC)).astype(np.complex64)


# ---------- envelope chains ----------

def shift_env(mm, t0, t1, center_mhz, tuner, lp=25000.0, dec=24, chunk=0.5):
    """|LP(shift)| envelope of [t0, t1) centered on center_mhz, decimated.

    Returns (env, fs2). The shift is phase-continuous across chunks.
    """
    off = tuner - center_mhz * 1e6
    st = new_state()
    k0 = int(t0 * FS)
    n = int((t1 - t0) * FS)
    ch = int(chunk * FS)
    out = []
    pos = k0
    while pos < k0 + n:
        m = min(ch, k0 + n - pos)
        c = frequency_shift(getc(mm, pos, m), off, FS, st)
        if lp:
            c = lowpass(c, lp, st)
        out.append(decimate_cont(np.abs(c).astype(np.float32), dec, st))
        pos += m
    return np.concatenate(out), FS / dec


def inst_freq(cb):
    """Instantaneous frequency (Hz) of a baseband signal, same length."""
    dd = np.empty(len(cb) - 1, dtype=np.complex64)
    np.multiply(cb[1:], np.conj(cb[:-1]), out=dd)
    inst = np.arctan2(dd.imag, dd.real) * FS / (2 * np.pi)
    return np.r_[inst[0], inst]


# ---------- run / packet parsing ----------

def runs(env, thr):
    """ON runs of env > thr: (starts, ends) in samples."""
    on = env > thr
    d = np.diff(on.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if on[0]:
        starts = [0] + starts
    if on[-1]:
        ends = ends + [len(on)]
    return np.array(starts), np.array(ends)


def merge_close(starts, ends, min_gap):
    """Merge runs separated by less than min_gap samples (noise dips)."""
    if len(starts) == 0:
        return starts, ends
    s2, e2 = [starts[0]], [ends[0]]
    for s, e in zip(starts[1:], ends[1:]):
        if s - e2[-1] < min_gap:
            e2[-1] = e
        else:
            s2.append(s)
            e2.append(e)
    return np.array(s2), np.array(e2)


def split_packets(starts, ends, fs, env_len, gap_s=3e-3):
    """Split runs into packets at gaps longer than gap_s.

    Returns a list of (t_start_s, on_us array, gap_us array); gap_us has
    one entry less than on_us (the gap after the last pulse of a packet is
    the inter-packet gap). The gap after the final run runs to env_len.
    """
    on_us = (ends - starts) / fs * 1e6
    gap_us = np.empty(len(starts))
    for i in range(len(starts) - 1):
        gap_us[i] = (starts[i + 1] - ends[i]) / fs * 1e6
    gap_us[-1] = (env_len - ends[-1]) / fs * 1e6
    pk = np.where(gap_us > gap_s * 1e6)[0]
    bounds = np.r_[0, pk + 1, len(starts)]
    out = []
    for pi in range(len(bounds) - 1):
        a, b = int(bounds[pi]), int(bounds[pi + 1])
        if a >= len(starts):
            continue
        out.append((starts[a] / fs, on_us[a:b],
                    gap_us[a:b - 1] if b - a > 1 else np.array([])))
    return out


# ---------- bits ----------

def ppm_bits(gaps, ratio=1.5):
    """Pulse-distance reading: gap > median*ratio -> 1 else 0."""
    med = np.median(gaps) if len(gaps) else 0
    return ''.join('1' if g > med * ratio else '0' for g in gaps)


def pwm_bits(ons, ratio=1.5):
    """Pulse-width reading: ON > median*ratio -> 1 else 0."""
    med = np.median(ons) if len(ons) else 0
    return ''.join('1' if o > med * ratio else '0' for o in ons)


def bits_to_hex(bits):
    bb = bits[:len(bits) // 8 * 8]
    return ' '.join('%02X' % int(bb[i:i + 8], 2) for i in range(0, len(bb), 8))


def bits_to_hex_lsb(bits):
    bb = bits[:len(bits) // 8 * 8]
    return ' '.join('%02X' % int(bb[i:i + 8][::-1], 2) for i in range(0, len(bb), 8))


# ---------- spectra ----------

def spectrum(mm, t0, t1, tuner, nfft=4096):
    """(freqs_hz, avg_dbfs, maxhold_dbfs) over [t0, t1), absolute RF."""
    k0 = int(t0 * FS)
    n = int((t1 - t0) * FS)
    c = getc(mm, k0, n)
    win = np.hanning(nfft)
    nseg = n // nfft
    acc = np.zeros(nfft)
    mx = np.zeros(nfft)
    for i in range(nseg):
        p = np.abs(np.fft.fft(c[i * nfft:(i + 1) * nfft] * win)) ** 2
        acc += p
        mx = np.maximum(mx, p)
    acc /= nseg
    norm = (127.5 * nfft / 2) ** 2
    dba = np.fft.fftshift(10 * np.log10(acc / norm + 1e-12))
    dbm = np.fft.fftshift(10 * np.log10(mx / norm + 1e-12))
    fr = (np.arange(nfft) - nfft // 2) * FS / nfft + tuner
    return fr, dba, dbm


def find_peaks(fr, db, floor, min_raise=12.0, cluster=3000.0):
    """Local maxima at least min_raise dB above floor, clustered."""
    peaks = []
    n = len(db)
    for i in range(2, n - 2):
        if db[i] > db[i - 1] and db[i] >= db[i + 1] and db[i] > floor + min_raise:
            if peaks and fr[i] - peaks[-1][0] < cluster:
                if db[i] > peaks[-1][1]:
                    peaks[-1] = (fr[i], db[i])
            else:
                peaks.append((fr[i], db[i]))
    return peaks


def spectral_lines(mm, t0, t1, tuner, nfft_pow=18, nlines=14):
    """Strongest lines of one window (hanning FFT, dBFS = tone amplitude
    relative to full scale). Returns [(abs_hz, dbfs)] sorted by level."""
    k0 = int(t0 * FS)
    n = int((t1 - t0) * FS)
    c = getc(mm, k0, n)
    N = 1 << nfft_pow
    seg = c[:N] * np.hanning(N)
    mag = np.abs(np.fft.fftshift(np.fft.fft(seg)))
    db = 20 * np.log10(mag / (127.5 * N / 2) + 1e-12)
    freqs = (np.arange(N) - N // 2) * FS / N + tuner
    lines = []
    used = np.zeros(N, bool)
    for i in np.argsort(mag)[::-1][:4000]:
        if used[i]:
            continue
        lo = i
        while lo > 0 and mag[lo - 1] > mag[i] * 0.5:
            lo -= 1
        hi = i
        while hi < N - 1 and mag[hi + 1] > mag[i] * 0.5:
            hi += 1
        j = lo + int(np.argmax(mag[lo:hi + 1]))
        lines.append((freqs[j], db[j]))
        used[lo:hi + 1] = True
        if len(lines) >= nlines:
            break
    lines.sort(key=lambda x: -x[1])
    return lines


# ---------- spectrogram rendering ----------

CHARS = ' .:-=+*#%@'


def spectro(mm, t0, t1, center_mhz, tuner, nfft=2048, hop=None, lp=0):
    """Power spectrogram (dBFS) centered on center_mhz.

    Returns (db 2-D array [frame, bin] fftshifted, freqs_hz, t0, dt_frame).
    """
    if hop is None:
        hop = nfft // 8
    off = tuner - center_mhz * 1e6
    k0 = int(t0 * FS)
    n = int((t1 - t0) * FS)
    st = new_state()
    c = getc(mm, k0, n)
    c = frequency_shift(c, off, FS, st)
    if lp:
        c = lowpass(c, lp, st)
    win = np.hanning(nfft)
    nfr = 1 + (n - nfft) // hop
    S = np.empty((nfr, nfft), dtype=np.float32)
    for i in range(nfr):
        S[i] = np.abs(np.fft.fft(c[i * hop:i * hop + nfft] * win)) ** 2
    db = np.fft.fftshift(10 * np.log10(S / (127.5 * nfft / 2) ** 2 + 1e-12), axes=1)
    fr = (np.arange(nfft) - nfft // 2) * FS / nfft + center_mhz * 1e6
    return db, fr, t0, hop / FS


def ascii_spec(db, fr, t0, dt, vmin, vmax, rowagg=10, colagg=2, chars=CHARS):
    """ASCII spectrogram with a GLOBAL dB window (vmin..vmax)."""
    nfr, nbin = db.shape
    rows = nfr // rowagg
    cols = nbin // colagg
    print('ASCII spectrogram: %d rows x %d cols (row %.2f ms, col %.2f kHz)' % (
        rows, cols, rowagg * dt * 1e3, colagg * (fr[1] - fr[0]) / 1e3))
    print('dB scale: %d (top) .. %d (bottom); freq %.6f -> %.6f MHz' % (
        vmax, vmin, fr[0] / 1e6, fr[-1] / 1e6))
    for ri in range(rows):
        block = db[ri * rowagg:(ri + 1) * rowagg, :]
        line = '%7.3fs ' % (t0 + ri * rowagg * dt)
        for ci in range(cols):
            v = np.percentile(block[:, ci * colagg:(ci + 1) * colagg], 90)
            x = min(max((v - vmin) / (vmax - vmin), 0), 1)
            line += chars[min(int(x * (len(chars) - 1) + 0.5), len(chars) - 1)]
        print(line)


def spec_png(db, path, vmin, vmax):
    """Save the spectrogram as a PNG (time down, frequency right)."""
    from PIL import Image
    Sb = np.clip((db - vmin) / (vmax - vmin), 0, 1)
    r = np.clip(1.6 * Sb - 0.1, 0, 1)
    g = np.clip(1.6 * Sb ** 2 - 0.05, 0, 1)
    b = np.clip(0.9 * Sb ** 3, 0, 1)
    img = np.stack([r, g, b], axis=-1)
    img = (img * 255).astype(np.uint8)
    Image.fromarray(img[::-1]).save(path)
    return path
