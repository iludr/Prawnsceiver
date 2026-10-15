"""Pre-flight analysis for raw IQ recordings (the Record button's zip).

Everything an analyst - human or AI - needs to orient in a recording,
computed server-side so the multi-hundred-MB raw file only needs to be
touched for deep dives: the time-averaged spectrum, the emitters found
in it with per-signal timing, and a modulation probe that recognizes
the PT2262/EV1527-family fixed-code OOK remotes and pre-decodes their
repeated codes.

The spectrum center is the TUNER frequency (display center plus the
offset tuning and the calibration correction): in the raw stream
the dongle's LO/DC spike sits at 0 Hz and the displayed center at
-offset. Sparse bursts (radar, DME, impulsive noise) average away,
so a per-bin max-hold is tracked as well and peaks that only stand
out there are reported as sparse_signals; the LO spike is flagged.

Memory-safe by design: the uint8 IQ file is memmapped and processed
FFT-window by FFT-window, and the number of windows is capped
(max_windows) regardless of recording length - a 120 s capture costs
the same as a 10 s one.
"""
import numpy as np

_N = 4096                          # analysis FFT size
_LP_TAPS = 128                     # modulation-probe low-pass length
_LP_CUT = 40_000.0                 # probe low-pass cutoff, Hz


def analyze_recording(iq_path, center_hz, fs, max_windows=4000):
    """Analyze a raw uint8 IQ recording (the untouched dongle stream).

    Returns a JSON-ready dict: the averaged spectrum (dBFS*10 per bin,
    offset from center_freq_hz), the noise floor, and every detected
    emitter with absolute frequency, peak level, occupied bandwidth,
    duty cycle and burst period - plus, for the strongest ones, the
    modulation probe (pulse timing, gap-length bit encoding, decoded
    fixed codes)."""
    out = {
        "center_freq_hz": center_hz,
        "center_note": ("the raw stream's center = the tuner command "
                        "frequency (display center + offset tuning + "
                        "calibration correction)"),
        "sample_rate_hz": fs,
        "how_to_use": (
            "Read this first: 'emitters' lists every signal found in "
            "the capture with absolute RF frequency, level, duty cycle "
            "and modulation; 'sparse_signals' the ones only visible in "
            "the max-hold (radar, DME, impulsive interference - "
            "averaged away in the average spectrum). "
            "'spectrum.dbfs_x10' is the time-averaged spectrum, "
            "'spectrum.peak_dbfs_x10' the per-bin max-hold (both "
            "dBFS*10, bins of 'spectrum.bin_hz', offsets from "
            "center_freq_hz = the tuner frequency, the raw stream's "
            "center). 'dc_or_lo_spike' marks the dongle's own LO "
            "leakage at the tuner frequency. raw_iq.u8 is only needed "
            "for deep dives; decoded codes are heuristic - verify "
            "against the raw file before acting on them."),
        "spectrum": {"bins": _N, "bin_hz": fs // _N, "dbfs_x10": [],
               "peak_dbfs_x10": []},
        "noise_floor_dbfs": None,
        "emitters": [],
        "sparse_signals": [],
    }
    try:
        mm = np.memmap(iq_path, dtype=np.uint8, mode="r")
    except Exception:
        return out
    n_cplx = len(mm) // 2
    if n_cplx < 4 * _N:
        return out

    def getc(k0, n):
        """Complex float slice of the memmapped uint8 IQ stream."""
        b = np.asarray(mm[2 * k0:2 * (k0 + n)])
        return ((b[0::2].astype(np.float32) - 127.5)
                + 1j * (b[1::2].astype(np.float32) - 127.5))

    win = np.hanning(_N).astype(np.float32)
    step = max(_N, (n_cplx - _N) // max_windows + 1)
    # Full scale: a Hann-windowed sine spanning the uint8 range
    # (+/-127.5 after the DC offset) - the same dBFS convention as the
    # live waterfall's dBFS rows
    full_scale = 0.25 * _N * 127.5

    # Capped spectrogram rows + averaged power spectrum + max-hold
    rows, acc, acc_n = [], np.zeros(_N), 0
    peak = np.full(_N, -999.0)
    k = 0
    while k + _N <= n_cplx:
        m = np.abs(np.fft.fftshift(np.fft.fft(getc(k, _N) * win)))
        rows.append(m)
        acc += m * m
        acc_n += 1
        np.maximum(peak, 20.0 * np.log10(m / full_scale + 1e-9),
                   out=peak)
        k += step
    S = np.asarray(rows)
    # acc holds summed POWER: RMS amplitude = sqrt(mean power), and
    # dBFS is 20*log10(amplitude / full_scale)
    avg_db = 20.0 * np.log10(np.sqrt(acc / acc_n) / full_scale + 1e-9)
    floor = float(np.median(avg_db))
    out["noise_floor_dbfs"] = round(floor, 1)
    out["spectrum"]["dbfs_x10"] = [int(v) for v in
                                   np.clip(avg_db * 10, -3276, 3276)]
    out["spectrum"]["peak_dbfs_x10"] = [int(v) for v in
                                        np.clip(peak * 10, -3276, 3276)]

    # Emitter clustering: bins 12 dB over the floor, dilated a few bins
    hot = avg_db > floor + 12.0
    dil = hot.copy()
    for d in range(1, 5):
        dil[d:] |= hot[:-d]
        dil[:-d] |= hot[d:]
    covered = np.zeros(_N, dtype=bool)   # avg-detected bins (excluded
    emitters = []                        # from the sparse pass)
    i = 0
    while i < _N:
        if not dil[i]:
            i += 1
            continue
        j = i
        while j < _N and dil[j]:
            j += 1
        pk = i + int(np.argmax(avg_db[i:j]))
        band = S[:, max(0, pk - 8):pk + 9].max(axis=1)
        # Activity from the emitter's OWN band statistics: a nearly
        # constant band is a continuous carrier (always on), otherwise
        # twice the band's quiet level marks the active rows. A global
        # row-median threshold would be dominated by any strong carrier
        # elsewhere in the capture.
        b_med = float(np.median(band)) or 1e-9
        if float(band.max()) < 4.0 * b_med:
            act = np.ones(len(band), dtype=bool)
        else:
            act = band > b_med * 2.0
        # Burst segmentation: active rows merged while separated by
        # less than ~20 ms of quiet (fast OOK speckles row-by-row -
        # one pulse pattern is shorter than one FFT row). A nearly
        # constant band is a continuous carrier: one always-on segment.
        merge_rows = max(1, int(0.02 * fs / step))
        act_idx = np.where(act)[0]
        segs = []
        if len(act_idx):
            s0 = prev = act_idx[0]
            for r in act_idx[1:]:
                if r - prev > merge_rows:
                    segs.append((s0, prev))
                    s0 = r
                prev = r
            segs.append((s0, prev))
        else:
            segs = [(0, len(act) - 1)]
        r_strong = int(np.argmax(band))
        r_start = r_strong
        for a, b in segs:
            if a <= r_strong <= b:
                r_start = a          # burst containing the strongest row
                break
        span_rows = sum(b - a + 1 for a, b in segs)
        starts = [a for a, b in segs]
        e = {
            "abs_mhz": round(center_hz / 1e6
                             + (pk - _N // 2) * (fs / _N) / 1e6, 5),
            "offset_khz": round((pk - _N // 2) * (fs / _N) / 1000.0, 1),
            "peak_dbfs": round(float(avg_db[pk]), 1),
            "occupied_bw_khz": round((j - i) * (fs / _N) / 1000.0, 1),
            "duty_pct": round(100.0 * span_rows / len(act), 1),
            "bursts": len(segs),
            "period_s": (round(float(np.median(np.diff(starts))) * step / fs, 2)
                         if len(starts) > 2 else None),
            "modulation": None,
            # the LO spike's peak bin can wander a few bins off DC
            # (noise on the skirts) - flag the whole DC cluster
            "dc_or_lo_spike": i <= _N // 2 < j,
            "_pk": pk,
            "_row": r_start,
        }
        covered[i:j] = True
        emitters.append(e)
        i = j
    emitters.sort(key=lambda x: -x["peak_dbfs"])

    # Modulation probe for the strongest emitters
    for e in emitters[:6]:
        e["modulation"] = _probe(getc, n_cplx, e, fs, step)
        e.pop("_pk", None)
        e.pop("_row", None)
    for e in emitters[6:]:
        e.pop("_pk", None)
        e.pop("_row", None)
    out["emitters"] = emitters

    # Sparse signals: peaks standing out only in the max-hold -
    # radar, DME, impulsive interference average below the emitter
    # threshold but are unmistakable per-bin peaks (observed live:
    # 2026-10-15, the L-band recordings). Bins already covered by an
    # avg-detected emitter are excluded; the dongle's own LO spike
    # is flagged instead of reported as a signal.
    sparse = []
    ph = peak > floor + 15.0
    ph &= ~covered
    pd = ph.copy()
    for d in range(1, 5):
        pd[d:] |= ph[:-d]
        pd[:-d] |= ph[d:]
    i = 0
    while i < _N:
        if not pd[i]:
            i += 1
            continue
        j = i
        while j < _N and pd[j]:
            j += 1
        pk = i + int(np.argmax(peak[i:j]))
        if ph[i:j].sum() < 3:
            i = j
            continue     # single-bin outlier: max-hold noise extreme
        sparse.append({
            "abs_mhz": round(center_hz / 1e6
                             + (pk - _N // 2) * (fs / _N) / 1e6, 5),
            "offset_khz": round((pk - _N // 2) * (fs / _N) / 1000.0, 1),
            "peak_dbfs": round(float(peak[pk]), 1),
            "width_khz": round((j - i) * (fs / _N) / 1000.0, 1),
            "dc_or_lo_spike": i <= _N // 2 < j,
        })
        if len(sparse) >= 12:
            break
        i = j
    out["sparse_signals"] = sparse
    return out


def _probe(getc, n_cplx, e, fs, step, span_s=1.2):
    """Classify one emitter from a short window at its strongest
    spectrogram row: a clean pulse train -> OOK with gap-length bits
    and the repeated codes; otherwise a continuous or frequency-
    modulated carrier."""
    t0 = max(0, e["_row"] * step - int(0.02 * fs))   # incl. the burst rise
    span = min(int(span_s * fs), n_cplx - t0 - _N)
    if span < _N:
        return {"type": "too_short"}
    x = getc(t0, span)
    f_off = e["offset_khz"] * 1000.0
    n0 = np.arange(t0, t0 + span, dtype=np.float64)
    bb = x * np.exp(-2j * np.pi * f_off * n0 / fs)
    taps = (np.sinc(2 * _LP_CUT / fs * (np.arange(_LP_TAPS)
                                        - (_LP_TAPS - 1) / 2))
            * np.hamming(_LP_TAPS))
    taps /= taps.sum()
    lp = np.convolve(bb, taps, "same")[::8]
    fsd = fs / 8
    env = np.abs(lp)
    emax = float(env.max())
    if emax <= 0:
        return {"type": "silent"}
    on = env > emax * 0.4
    d = np.diff(on.astype(int))
    starts = np.where(d == 1)[0] + 1
    ends = np.where(d == -1)[0] + 1
    if on[0]:
        starts = np.r_[0, starts]
    if on[-1]:
        ends = np.r_[ends, len(on)]
    widths = (ends - starts) / fsd * 1000.0
    gaps = (starts[1:] - ends[:-1]) / fsd * 1000.0
    if len(widths) >= 8 and float(np.std(widths)) < 0.3 * float(np.median(widths)) \
            and float(on.mean()) < 0.6:
        m = {"type": "ook",
             "pulse_ms": round(float(np.median(widths)), 3)}
        if len(gaps):
            p10 = float(np.percentile(gaps, 10))
            bit_thr = 1.25 * p10
            longs = [g for g in gaps if g > bit_thr]
            sync_thr = max(2.0, 1.8 * float(np.median(longs))) if longs \
                else 1e9
            short = [g for g in gaps if g <= bit_thr]
            m["gap_short_ms"] = (round(float(np.median(short)), 3)
                                 if short else None)
            m["gap_long_ms"] = (round(float(np.median(longs)), 3)
                                if longs else None)
            m["sync_gap_ms"] = round(sync_thr, 2)
            m["carrier"] = ("unmodulated (ASK on/off)"
                            if _pulse_freq_stable(lp, starts, ends, fsd)
                            else "frequency-shifted during pulses")
            frames, cur = [], []
            for i2 in range(len(widths)):
                cur.append(i2)
                if i2 < len(gaps) and gaps[i2] > sync_thr:
                    if len(cur) >= 8:
                        frames.append(_bits(cur, gaps, bit_thr))
                    cur = []
            if len(cur) >= 8:
                frames.append(_bits(cur, gaps, bit_thr))
            codes = {}
            for b in frames:
                codes[b] = codes.get(b, 0) + 1
            m["codes"] = [{"bits": b, "count": c}
                          for b, c in sorted(codes.items(),
                                             key=lambda kv: -kv[1])[:4]]
        return m
    # No clean pulse train: classify by the frequency spread
    strong = env > float(np.median(env)) * 3
    inst = np.angle(lp[1:] * np.conj(lp[:-1])) * fsd / (2 * np.pi)
    spread = float(np.std(inst[strong[:len(inst)]])) if strong.any() else 0.0
    return {"type": "fm_or_fsk" if spread > 5000 else "continuous_carrier",
            "freq_spread_khz": round(spread / 1000.0, 1)}


def _bits(cur, gaps, bit_thr):
    """Gap-length bits for one frame's pulse indices."""
    return "".join("1" if gaps[j] > bit_thr else "0"
                   for j in cur if j < len(gaps))


def _pulse_freq_stable(lp, starts, ends, fsd, khz_thr=3.0):
    """True when the carrier inside the pulses stays within a few kHz
    (plain ASK) instead of hopping (FSK within the pulses)."""
    vals = []
    for s, e in list(zip(starts, ends))[:32]:
        p = lp[s:e]
        if len(p) < 4:
            continue
        inst = np.angle(p[1:] * np.conj(p[:-1])) * fsd / (2 * np.pi)
        vals.append(float(np.std(inst)))
    if not vals:
        return True
    return float(np.median(vals)) < khz_thr * 1000.0
