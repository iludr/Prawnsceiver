"""DSP chain: offset rotation, low-pass filtering, Doppler NCO, FM discrimination.

Every stage carries its state in a per-capture-thread dict (new_state()) so
that block-wise processing is identical to processing the whole stream: FIR
tails, the decimation phase, the rotator counter and the NCO phase all
survive block boundaries. Two dongles demodulate concurrently, so nothing
stream-specific may live in a module global — that includes the Doppler
NCO's cached ramp, which therefore sits in the state dict too.
"""
from math import gcd

import numpy as np

from ..config import SDR_RATE

TWO_PI = 2.0 * np.pi


def new_state():
    """Fresh DSP state for one IQ stream."""
    return {
        "rot": 0,            # offset rotator: sample counter mod its period
        "fir_tail": None,    # IQ low-pass: last numtaps-1 input samples
        "afir_tail": None,   # audio low-pass: last numtaps-1 input samples
        "last_c": None,      # discriminator: previous block's last sample
        "audio_pos": 0,      # decimation phase: sample count mod decimation
        "nco_phase": 0.0,    # Doppler NCO phase in rad, kept in [0, 2*pi)
        "nco_key": None,     # (doppler_hz, fs) the cached ramp was built for
        "nco_ramp": None,    # exp(i*dphase*j), j < block length
    }


demod_state = new_state()  # default for single-stream callers


def iq_to_complex(iq_bytes):
    """Convert raw 8-bit interleaved IQ bytes to a complex64 baseband array."""
    raw = np.frombuffer(iq_bytes, dtype=np.uint8)
    if len(raw) & 1:
        raw = raw[:-1]
    f = raw.astype(np.float32)
    f -= 127.5
    # Interleaved float32 pairs viewed as complex64 — no second copy
    return f.view(np.complex64)


# ---------- offset rotator ----------
# One LUT per (offset, fs, block length). For the periodic phasor (period
# fs/gcd(offset, fs) = 40 samples at 60 kHz / 2.4 MHz) the LUT holds one
# pre-rolled block-length row per rotator phase, so a block costs a single
# complex multiply with no index arithmetic. Offsets with a long period fall
# back to a one-period LUT with modulo indexing.
_phasor_luts = {}
_MAX_ROLLED_ROWS = 64
# Rolled-LUT element cap: the 2-D LUT holds period * n complex64
# samples (8 bytes each), cached forever by (offset, rate, n). For the
# 512-sample capture blocks that is nothing, but a 10 s decode chunk at
# 2.4 Msps / 60 kHz offset stacked 40 x 24M = 7.7 GB and OOM-killed the
# 7.2 GB notebook server twice (2026-10-11). Above this many elements
# the one-period LUT with modulo indexing is used instead.
_MAX_ROLLED_ELEMENTS = 4_000_000


def frequency_shift(c, offset_hz, fs, st=None):
    """Rotate baseband so a signal at -offset_hz moves to 0 Hz.

    The dongle is tuned offset_hz ABOVE the wanted frequency, so the signal
    arrives at -offset_hz and the DC spike at 0; this rotation centers the
    signal and displaces the DC spike to +offset_hz. The phase is continuous
    across calls via st["rot"].
    """
    st = st if st is not None else demod_state
    if offset_hz == 0:
        return c
    n = len(c)
    key = (int(offset_hz), int(fs), n)
    lut = _phasor_luts.get(key)
    if lut is None:
        period = int(fs) // gcd(int(offset_hz), int(fs))
        w = TWO_PI * offset_hz / fs
        if period <= _MAX_ROLLED_ROWS and period * n <= _MAX_ROLLED_ELEMENTS:
            base = np.exp(1j * w * np.arange(period + n)).astype(np.complex64)
            lut = np.stack([base[r:r + n] for r in range(period)])
        else:
            lut = np.exp(1j * w * np.arange(period)).astype(np.complex64)
        _phasor_luts[key] = lut
    if lut.ndim == 2:
        period = lut.shape[0]
        rot = lut[st["rot"]]
    else:
        period = len(lut)
        rot = lut[(np.arange(n) + st["rot"]) % period]
    st["rot"] = (st["rot"] + n) % period
    return c * rot


# ---------- low-pass FIRs ----------
_lowpass_taps = {}
_real_taps = {}
FIR_TAPS = 250


def _windowed_sinc(cutoff_hz, numtaps):
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / SDR_RATE * m) * np.hamming(numtaps)
    return (h / h.sum()).astype(np.float32)


def get_lowpass_taps(cutoff_hz=22000.0):
    """Windowed-sinc low-pass FIR taps (complex64, for the IQ path), cached
    per cutoff. 250 taps at 2.4 MHz: -3 dB at ~18 kHz for the 22 kHz
    design cutoff, -8 dB at 24 kHz, below -55 dB from 40 kHz up — the
    same absolute selectivity 25 taps gave at 240 kHz. Taps must scale
    with SDR_RATE or the transition band widens and adjacent-channel
    noise floods the demod (measured 2026-10-10, see EVENTLOG)."""
    key = int(cutoff_hz)
    if key not in _lowpass_taps:
        _lowpass_taps[key] = _windowed_sinc(cutoff_hz, FIR_TAPS).astype(np.complex64)
    return _lowpass_taps[key]


def get_real_taps(cutoff_hz):
    """Real (audio) low-pass FIR taps, cached per cutoff."""
    key = int(cutoff_hz)
    if key not in _real_taps:
        _real_taps[key] = _windowed_sinc(cutoff_hz, FIR_TAPS)
    return _real_taps[key]


_fft_filter_cache = {}

def convolve_valid(x, taps, key):
    """'valid'-mode FIR convolution, numerically equal to
    np.convolve(x, taps, 'valid'), FFT-based for large inputs: a
    250-610 tap FIR at 2.4 Msps is 0.6-1.5 GMAC/s per dongle with the
    direct loop (a whole CPU core), ~20x cheaper via FFT. Small inputs
    (per-block callers, tests) keep the direct path. `key` identifies
    the taps set for the FFT cache (taps objects are long-lived)."""
    n, m = len(x), len(taps)
    if n == 0:
        return x[:0]
    if n < 4096 or m > n:
        return np.convolve(x, taps, mode='valid')
    N = 1 << (n + m - 1).bit_length()
    H = _fft_filter_cache.get((key, N))
    if H is None:
        H = np.fft.fft(taps, N)
        _fft_filter_cache[(key, N)] = H
    y = np.fft.ifft(np.fft.fft(x, N) * H)
    return y[m - 1:n].astype(x.dtype)


def lowpass(c, cutoff_hz=22000.0, st=None):
    """Complex low-pass with the filter tail carried across blocks."""
    st = st if st is not None else demod_state
    taps = get_lowpass_taps(cutoff_hz)
    tail = st["fir_tail"]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.complex64)
    x = np.concatenate([tail, c])
    st["fir_tail"] = x[-(len(taps) - 1):].copy()
    return convolve_valid(x, taps, f'lp{cutoff_hz}')


def lowpass_audio(a, cutoff_hz, st=None):
    """Real low-pass on the discriminator output (anti-alias before decimation)."""
    st = st if st is not None else demod_state
    taps = get_real_taps(cutoff_hz)
    tail = st["afir_tail"]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.float32)
    x = np.concatenate([tail, np.asarray(a, dtype=np.float32)])
    st["afir_tail"] = x[-(len(taps) - 1):].copy()
    return convolve_valid(x, taps, f'af{cutoff_hz}')


# ---------- Doppler NCO ----------
def doppler_shift(c, doppler_hz, fs, st):
    """Rotate baseband by -doppler_hz with a phase-continuous NCO.

    Compensates a Doppler-shifted carrier into the demod center: a signal
    observed at +doppler_hz (approaching satellite) lands at 0 Hz. The
    scheduler steps doppler_hz every few seconds; a frequency change in an
    NCO must not jump the phase, so the phase (st["nco_phase"]) accumulates
    across blocks AND across Doppler updates. (An absolute-sample-count
    phase like -2*pi*d*k/fs jumps by an arbitrary angle at every update:
    each jump is a full-scale click in the demodulated audio.)

    The per-block rotation is exp(i*phase) * ramp, with ramp[j] =
    exp(i*dphase*j) for j < block length. The ramp only depends on the
    Doppler value and is rebuilt when it changes (every few seconds), so
    no per-sample exp() runs in the capture loop — and unlike a
    fs/gcd-period LUT it is tiny (block length, not up to fs samples) and
    needs no periodicity argument.

    doppler_hz == 0 keeps applying the constant phasor exp(i*phase): going
    from a rotation to "multiply by 1" is itself a phase jump. (A Doppler
    track crossing 0 Hz at closest approach clicked by up to 27 % of full
    scale before this was handled.) The phase is per stream, so the ramp
    cache lives in st and not in a module global shared by both dongles.
    """
    d = int(round(doppler_hz))
    phase = st["nco_phase"]
    if d == 0:
        if phase == 0.0:
            return c
        return c * np.complex64(np.exp(1j * phase))
    n = len(c)
    dphase = -TWO_PI * d / fs
    key = (d, int(fs))
    ramp = st["nco_ramp"]
    if st["nco_key"] != key or ramp is None or len(ramp) < n:
        ramp = np.exp(1j * dphase * np.arange(max(n, 1024))).astype(np.complex64)
        st["nco_key"], st["nco_ramp"] = key, ramp
    rot = ramp[:n] * np.complex64(np.exp(1j * phase))
    st["nco_phase"] = (phase + dphase * n) % TWO_PI
    return c * rot


# ---------- FM discriminator ----------
def fm_demodulate(c, decimation=5, iq_cutoff_hz=22000.0, audio_cutoff_hz=None, st=None):
    """FM discriminate a baseband-centered complex signal, decimate to audio rate.

    iq_cutoff_hz: IQ low-pass before discrimination (default 22 kHz APT
    channel). None skips it — broadcast FM tunes use the whole ±120 kHz
    capture band, because a short FIR cannot shape a clean ~95 kHz passband
    and the ±75 kHz deviation must stay inside. audio_cutoff_hz: anti-alias
    low-pass on the discriminator output before decimation to 48 kHz.

    Output: int16 PCM bytes, ±32767 == ±pi rad/sample (±fs/2 deviation).
    """
    st = st if st is not None else demod_state
    if len(c) == 0:
        return b''
    if iq_cutoff_hz:
        c = lowpass(c, iq_cutoff_hz, st)
    # Discriminator: phase advance of c[n] relative to c[n-1]. The conjugate
    # product + arctan2 yields the advance in (-pi, pi] directly — unlike
    # diff(arctan2(phase)), which produces 2*pi spikes whenever the wrapped
    # phase crosses the branch cut (any FM whose phase excursion exceeds pi,
    # i.e. also the APT signal). The previous block's last sample is carried
    # in st so block processing matches whole-signal processing.
    prev = st["last_c"]
    if prev is None:
        prev = c[0]
    dd = np.empty_like(c)
    dd[0] = c[0] * np.conj(prev)
    np.multiply(c[1:], np.conj(c[:-1]), out=dd[1:])
    st["last_c"] = c[-1].copy()
    audio = np.arctan2(dd.imag, dd.real)
    if audio_cutoff_hz:
        audio = lowpass_audio(audio, audio_cutoff_hz, st)
    if decimation > 1:
        # Decimate on a continuous phase across blocks — a per-block [::n]
        # reset would drop samples at every block boundary (0.2% rate error
        # at IQ_BLOCK size) and make block processing differ from a
        # continuous stream. Sample i of this block is kept when
        # (i + audio_pos) % decimation == 0.
        phase = st["audio_pos"] % decimation
        st["audio_pos"] = (st["audio_pos"] + len(audio)) % decimation
        audio = audio[(-phase) % decimation::decimation]
    return (audio * (32767.0 / np.pi)).astype(np.int16).tobytes()
