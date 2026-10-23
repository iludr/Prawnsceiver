"""Pass waterfall images: a spectrogram PNG for every recorded pass.

A pass whose decoder produced no image still leaves a waterfall diagram
next to the recording (<base>.waterfall.png, operator request
2026-10-23): the history page then shows what the receiver actually
saw instead of a bare "not decoded". Every recorded pass gets one —
decoded passes keep their decode image as the primary artifact, the
waterfall rides along in the same slideshow.

The waterfall prefers the raw IQ sibling of the recording (the full
capture band: the satellite's downlink stays visible even when the
decoder cannot make sense of it) and falls back to the WAV (the
demodulated audio band — SSTV tone structure, or just silence).

Same geometry as the live waterfall feed and the manual recording's
waterfall (radio.py): Hann-windowed FFT rows in dBFS*10, a 60 dB
display window below the strongest row, newest row at the top,
grayscale. Memory-safe by design: the capture is memmapped / seek-read
row by row and the row count is capped, so a full-pass 3.5 GB IQ costs
the same as a 10-second one.
"""
import os

import numpy as np

WATERFALL_SUFFIX = '.waterfall.png'
MAX_ROWS = 720          # row cap, regardless of recording length
N_FFT = 1024            # waterfall width in px (FFT bins)
DB_WINDOW = 600         # 60 dB display window below the strongest row
MIN_ROWS = 8            # shorter captures get no waterfall


def waterfall_png_path(wav_path):
    """The pass waterfall's filename slot next to a recording."""
    base, _ = os.path.splitext(wav_path)
    return base + WATERFALL_SUFFIX


def _db_rows(magnitudes, full_scale):
    """|X| rows -> int16 dBFS*10 rows, the live waterfall's convention:
    full scale is a sine spanning the sample range, Hann-windowed, whose
    FFT peak is sum(window)/2 = 0.25 * N of |X| (radio.py's 31.875 * N
    for the u8 stream, iqscan.py's 0.25 * N * 127.5)."""
    return (20 * np.log10(np.maximum(magnitudes, 1e-6) / full_scale) * 10) \
        .astype(np.int16)


def _rows_from_u8_iq(iq_path, n_fft=N_FFT, max_rows=MAX_ROWS):
    """Spectrogram rows of a raw uint8 IQ recording (the untouched
    dongle stream: interleaved u8 I/Q, DC at 127.5, so a full-scale
    sine spans +/-127.5)."""
    n_cplx = os.path.getsize(iq_path) // 2
    if n_cplx < n_fft * MIN_ROWS:
        return None
    mm = np.memmap(iq_path, dtype=np.uint8, mode='r')
    try:
        win = np.hanning(n_fft).astype(np.float32)
        full_scale = 0.25 * n_fft * 127.5
        step = max(n_fft, (n_cplx - n_fft) // max_rows + 1)
        rows = []
        k = 0
        while k + n_fft <= n_cplx and len(rows) < max_rows:
            b = np.asarray(mm[2 * k:2 * (k + n_fft)])
            c = (b[0::2].astype(np.float32) - 127.5) \
                + 1j * (b[1::2].astype(np.float32) - 127.5)
            rows.append(np.abs(np.fft.fftshift(np.fft.fft(c * win))))
            k += step
    finally:
        del mm          # release the mapping (the IQ may be deleted
                        # right after — POSIX keeps an open mapping alive)
    return _db_rows(np.asarray(rows), full_scale) if rows else None


def _rows_from_wav(wav_path, n_fft=N_FFT, max_rows=MAX_ROWS):
    """Spectrogram rows of the demod audio WAV (int16 mono)."""
    import wave
    with wave.open(wav_path, 'rb') as w:
        n = w.getnframes()
        if n < n_fft * MIN_ROWS:
            return None
        win = np.hanning(n_fft).astype(np.float32)
        full_scale = 0.25 * n_fft * 32767.0
        step = max(n_fft, (n - n_fft) // max_rows + 1)
        rows = []
        k = 0
        while k + n_fft <= n and len(rows) < max_rows:
            w.setpos(k)
            raw = w.readframes(n_fft)
            if len(raw) < 2 * n_fft:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            rows.append(np.abs(np.fft.fftshift(np.fft.fft(a * win))))
            k += step
    return _db_rows(np.asarray(rows), full_scale) if rows else None


def pass_waterfall(wav_path, max_rows=MAX_ROWS, n_fft=N_FFT):
    """The pass waterfall PNG of a recording: its path when it exists or
    was just generated, None when the recording is too short or nothing
    readable sits next to it.

    Idempotent: an existing <base>.waterfall.png is returned as is, and
    a real decode image (<base>.png) is never touched — the waterfall is
    an ADDITIONAL artifact, not a replacement.
    """
    base, _ = os.path.splitext(wav_path)
    png = base + WATERFALL_SUFFIX
    if os.path.exists(png):
        return png
    rows = None
    iq_path = base + '.iq.u8'
    if os.path.exists(iq_path):
        try:
            rows = _rows_from_u8_iq(iq_path, n_fft, max_rows)
        except Exception:
            rows = None
    if rows is None:
        try:
            rows = _rows_from_wav(wav_path, n_fft, max_rows)
        except Exception:
            rows = None
    if rows is None or len(rows) < MIN_ROWS:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    # Same display mapping as the manual recording's waterfall
    # (radio.py): 60 dB below the strongest row, newest row at the top
    lo = float(rows.max()) - DB_WINDOW
    img = np.clip((rows - lo) * (255.0 / DB_WINDOW), 0, 255).astype(np.uint8)
    Image.fromarray(img[::-1], mode='L').save(png)
    return png
