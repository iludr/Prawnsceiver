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
        "bpf_tail": None,    # audio-rate band-pass (SSB/CW sideband select)
        "dcfir_tail": None,  # AM: carrier-DC estimator low-pass
        "afir48_tail": None, # AM: 3.4 kHz audio low-pass at 48 kHz
        "bfo_phase": 0.0,    # CW beat oscillator NCO phase
        "agc_gain": None,    # SSB/CW AGC: current smoothed gain
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


def _windowed_sinc(cutoff_hz, numtaps, fs=SDR_RATE):
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / fs * m) * np.hamming(numtaps)
    return (h / h.sum()).astype(np.float32)


def sinc_taps(cutoff_hz, numtaps, fs=SDR_RATE):
    """Windowed-sinc low-pass FIR taps in float64 — the shared channel
    filter builder of the decode roles (AIS/APRS/Orbcomm; each calls it
    once at import with its own cutoff and tap count). Same kernel as
    _windowed_sinc, which keeps its float32 for the audio path."""
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / fs * m) * np.hamming(numtaps)
    return (h / h.sum()).astype(np.float64)


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
    direct loop (a whole CPU core), far cheaper via FFT. Large inputs
    use segmented overlap-save with small FFTs (~12x the tap count):
    FFT cost grows with N*log2(N), so several 4-8k FFTs beat one big
    one ~2.3x (measured on the notebook's i5-7200U, where one 64k FFT
    pair costs ~9 ms under load). Small inputs (per-block callers,
    tests) keep the direct path. `key` identifies the taps set for the
    FFT cache (taps objects are long-lived). The input is expected to
    carry the filter tail prepended, exactly like the direct callers
    assemble it - the returned samples align with convolve(x, taps,
    'valid') of that assembled input.
    """
    n, m = len(x), len(taps)
    if n == 0:
        return x[:0]
    if n < 4096 or m > n:
        return np.convolve(x, taps, mode='valid')
    seg = 1 << max(12, (12 * m - 1).bit_length())
    hop = seg - m + 1
    H = _fft_filter_cache.get((key, seg))
    if H is None:
        H = np.fft.fft(taps, seg)
        _fft_filter_cache[(key, seg)] = H
    total = n - m + 1
    out = np.empty(total, dtype=x.dtype)
    real = not np.iscomplexobj(x)
    produced = 0
    pos = 0
    while produced < total:
        block = x[pos:pos + seg]
        if len(block) < seg:
            block = np.concatenate(
                [block, np.zeros(seg - len(block), dtype=x.dtype)])
        y = np.fft.ifft(np.fft.fft(block, seg) * H)
        if real:
            y = y.real
        take = min(hop, total - produced)
        out[produced:produced + take] = y[m - 1:m - 1 + take]
        produced += take
        pos += hop
    return out


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


# ---------- AM / SSB / CW demodulators ----------
#
# All three decimate the channel-filtered IQ to the 48 kHz audio rate
# FIRST (the +-bw IQ low-pass is the anti-alias), then do the mode
# work there: a 2.7 kHz-sideband or 500 Hz-cw filter is impossible with
# short FIRs at 2.4 Msps, but cheap at 48 kHz. Sideband selection uses
# a COMPLEX-taps band-pass (a windowed-sinc low-pass rotated to the
# band center): its response is asymmetric, so it passes [lo, hi] and
# rejects the mirror band - which is exactly what taking the real
# part afterwards needs, or the unwanted sideband would fold onto the
# audio. Blocks carry state like the FM path: decimation phase, FIR
# tails, the CW BFO phase and the AGC gain all live in st.


def decimate_cont(a, decimation, st):
    """Decimate keeping the phase across blocks (fm_demodulate logic,
    shared): sample i of this block is kept when (i + audio_pos) %
    decimation == 0, so block processing equals whole-signal [::n]."""
    if decimation <= 1:
        return a
    phase = st["audio_pos"] % decimation
    st["audio_pos"] = (st["audio_pos"] + len(a)) % decimation
    return a[(-phase) % decimation::decimation]


_bp_taps = {}


def get_bandpass_taps(lo_hz, hi_hz, fs, numtaps=1024):
    """Complex band-pass FIR taps for the 48 kHz audio path: windowed-
    sinc low-pass of (hi-lo)/2 rotated to the band center, cached per
    (lo, hi). numtaps=1024 at 48 kHz -> ~375 Hz main lobe: voice-
    grade SSB selectivity and a soft but usable cw filter."""
    key = (round(lo_hz, 1), round(hi_hz, 1), int(fs), int(numtaps))
    if key not in _bp_taps:
        center = (lo_hz + hi_hz) / 2.0
        half = (hi_hz - lo_hz) / 2.0
        h = _windowed_sinc(half, numtaps, fs).astype(np.complex64)
        m = np.arange(numtaps) - (numtaps - 1) / 2.0
        _bp_taps[key] = h * np.exp(1j * 2 * np.pi * center / fs * m).astype(np.complex64)
    return _bp_taps[key]


def bandpass(c, lo_hz, hi_hz, fs, st):
    """Complex band-pass with the tail carried across blocks (the audio-
    rate sibling of lowpass())."""
    taps = get_bandpass_taps(lo_hz, hi_hz, fs)
    tail = st["bpf_tail"]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.complex64)
    x = np.concatenate([tail, c])
    st["bpf_tail"] = x[-(len(taps) - 1):].copy()
    return convolve_valid(x, taps, f"bp{round(lo_hz)}:{round(hi_hz)}")


def _real_lp(a, cutoff_hz, fs, st, tail_key, numtaps=512):
    """Real low-pass at an arbitrary rate with its own tail slot in st
    (lowpass_audio() assumes the 2.4 Msps capture rate)."""
    key = (round(cutoff_hz, 1), int(fs), int(numtaps))
    taps = _real_taps.get(key)
    if taps is None:
        taps = _real_taps[key] = _windowed_sinc(cutoff_hz, numtaps, fs)
    tail = st[tail_key]
    if tail is None or len(tail) != len(taps) - 1:
        tail = np.zeros(len(taps) - 1, dtype=np.float32)
    x = np.concatenate([tail, np.asarray(a, dtype=np.float32)])
    st[tail_key] = x[-(len(taps) - 1):].copy()
    return convolve_valid(x, taps, f"rl{tail_key}{round(cutoff_hz)}")


def _bfo_shift(c, freq_hz, fs, st):
    """Phase-continuous constant rotation (the CW beat oscillator)."""
    n = len(c)
    if freq_hz == 0 or n == 0:
        return c
    phase = st.get("bfo_phase", 0.0)
    dphase = TWO_PI * freq_hz / fs
    rot = (np.exp(1j * dphase * np.arange(n)) * np.complex64(np.exp(1j * phase))).astype(np.complex64)
    st["bfo_phase"] = (phase + dphase * n) % TWO_PI
    return c * rot


def _agc(a, st, fs, target=0.25, att_secs=0.006, rel_secs=0.4, max_gain=3000.0):
    """Feedforward AGC for the carrier-less modes: per-chunk target gain
    from the chunk rms, one-pole smoothed (fast attack, slow release)
    and linearly ramped across the chunk so the gain never steps -
    a step is a click. Chunks are ~13 ms, so the smoothing runs at
    chunk granularity: attack ~6 ms, release ~0.4 s."""
    n = len(a)
    if n == 0:
        return a
    rms = float(np.sqrt(np.mean(np.asarray(a, dtype=np.float64) ** 2)))
    if rms < 1e-12:
        rms = 1e-12
    g_t = min(max_gain, target / rms)
    g0 = st.get("agc_gain")
    if g0 is None:
        g0 = min(max_gain, target / max(rms, 1e-3))
    tau = att_secs if g_t < g0 else rel_secs
    a_e = np.exp(-n / (tau * fs))
    g = g0 * a_e + g_t * (1 - a_e)
    st["agc_gain"] = g
    if n == 1:
        return a * g
    return a * np.linspace(g0, g, n).astype(np.float32)


def am_demodulate(c, decimation, iq_cutoff_hz=10000.0, st=None):
    """AM envelope detector: channel low-pass -> |.| envelope -> decimate
    -> carrier-DC block (a 100 Hz low-pass subtracts the carrier level;
    it doubles as a ~300 Hz high-pass, standard on airband audio) ->
    3.4 kHz audio low-pass -> normalized by the carrier level (a weak
    or absent carrier clamps the gain so band noise does not blast).

    Output: int16 PCM at fs/decimation."""
    st = st if st is not None else demod_state
    if len(c) == 0:
        return b''
    if iq_cutoff_hz:
        c = lowpass(c, iq_cutoff_hz, st)
    env = np.abs(c).astype(np.float32)
    env = decimate_cont(env, decimation, st)
    fs = SDR_RATE // decimation
    dc = _real_lp(env, 100.0, fs, st, "dcfir_tail")
    audio = env - dc
    audio = _real_lp(audio, 3400.0, fs, st, "afir48_tail")
    carrier = max(float(np.mean(dc)), 6.0)
    audio = audio * (0.75 * 32767.0 / carrier)
    np.clip(audio, -32767, 32767, out=audio)
    return audio.astype(np.int16).tobytes()


# SSB/CW voice and cw filter bands in the 48 kHz audio path. The BFO
# moves the cw carrier off 0 Hz into the band; usb/lsb need no BFO -
# the suppressed carrier sits at 0 and the band IS the sideband.
_SSB_BANDS = {
    "usb": (0.0, 100.0, 3300.0),          # (bfo_hz, lo_hz, hi_hz)
    "lsb": (0.0, -3300.0, -100.0),
    "cw": (700.0, 500.0, 1000.0),          # beat note ~700 Hz
}


def ssb_demodulate(c, decimation, sideband="usb", iq_cutoff_hz=6000.0, st=None, agc=True):
    """SSB/CW product detector: channel low-pass -> decimate to the audio
    rate -> BFO shift (cw only) -> complex band-pass sideband select
    -> Re{} -> AGC. The band-pass rejects the mirror sideband before
    the real part folds it onto the audio; without it usb and lsb
    would sound identical.

    Output: int16 PCM at fs/decimation, AGC target rms ~0.25 FS."""
    st = st if st is not None else demod_state
    if len(c) == 0:
        return b''
    bfo, lo, hi = _SSB_BANDS[sideband]
    if iq_cutoff_hz:
        c = lowpass(c, iq_cutoff_hz, st)
    c = decimate_cont(c, decimation, st)
    fs = SDR_RATE // decimation
    c = _bfo_shift(c, bfo, fs, st)
    c = bandpass(c, lo, hi, fs, st)
    audio = c.real.astype(np.float32)
    if agc:
        audio = _agc(audio, st, fs)
    audio = audio * (0.9 * 32767.0)
    np.clip(audio, -32767, 32767, out=audio)
    return audio.astype(np.int16).tobytes()
