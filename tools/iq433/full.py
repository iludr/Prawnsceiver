#!/usr/bin/env python3
"""Full-rate deep dive of one window of a raw IQ recording.

Fine spectral lines, full-rate envelope ON/OFF runs, instantaneous
frequency during ON (OOK vs FSK test), and a spectrogram PNG + ASCII with
a global dB window.

Usage:
  .venv/bin/python tools/iq433/full.py <rec.iq.u8> <t_start_s> <dur_s> <center_mhz> [tag]
"""
import os
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit('/', 2)[0])
from iq433lib import (FS, ascii_spec, frequency_shift, getc, inst_freq, lowpass,
                      merge_close, new_state, open_iq, runs, shift_env, spec_png,
                      spectral_lines, spectro, tuner_hz)


def main():
    iq = sys.argv[1]
    t0 = float(sys.argv[2])
    dur = float(sys.argv[3])
    center_mhz = float(sys.argv[4])
    tag = sys.argv[5] if len(sys.argv) > 5 else 'win'
    tuner = tuner_hz(iq)
    mm = open_iq(iq)
    print('=== %s: t=%.3f s dur=%.3f s center %.6f MHz tuner %.1f Hz ===' % (
        tag, t0, dur, center_mhz, tuner))

    # 1. fine spectral lines
    print('top spectral lines (abs freq, dBFS):')
    for f, d in spectral_lines(mm, t0, t0 + dur, tuner):
        print('  %12.1f Hz  %6.1f dBFS' % (f, d))

    # 2. full-rate envelope runs (10 us smoothing)
    env, fs2 = shift_env(mm, t0, t0 + dur, center_mhz, tuner, lp=0, dec=1)
    k = 24
    envs = np.convolve(env, np.ones(k) / k, mode='same')
    noise = np.median(envs)
    peak = np.percentile(envs, 99.9)
    thr = noise + 0.35 * (peak - noise)
    starts, ends = runs(envs, thr)
    starts, ends = merge_close(starts, ends, int(0.08 * FS / 1e6))
    on_us = (ends - starts) / FS * 1e6
    gap_us = (np.r_[starts[1:], len(envs)] - ends) / FS * 1e6
    print('env: noise %.3f peak %.3f thr %.3f' % (noise, peak, thr))
    print('pulses: %d' % len(starts))
    print('ON us :', ' '.join('%.0f' % v for v in on_us))
    print('GAP us:', ' '.join('%.0f' % v for v in gap_us))

    # 3. instantaneous frequency during ON (OOK vs FSK)
    st = new_state()
    c = frequency_shift(getc(mm, int(t0 * FS), int(dur * FS)), tuner - center_mhz * 1e6, FS, st)
    cbl = lowpass(c, 60000.0, st)
    inst = inst_freq(cbl)
    freqs_p = []
    for s, e in zip(starts, ends):
        a = s + int((e - s) * 0.2)
        b = e - int((e - s) * 0.2)
        if b > a:
            freqs_p.append(float(np.mean(inst[a:b])))
    freqs_p = np.array(freqs_p)
    print('per-pulse mean inst freq (Hz), first 60:')
    print(' '.join('%+.0f' % v for v in freqs_p[:60]))
    hist, edges = np.histogram(freqs_p, bins=40)
    print('inst-freq histogram during ON (Hz bins):')
    for i in range(len(hist)):
        if hist[i]:
            print('  %+7.0f..%+7.0f : %d' % (edges[i], edges[i + 1], hist[i]))

    # 4. spectrogram PNG + ASCII (global dB)
    db, fr, t0s, dt = spectro(mm, t0, t0 + dur, center_mhz, tuner)
    vmax = np.percentile(db, 99.9)
    vmin = vmax - 75
    print('spec dB range: %.1f .. %.1f' % (vmin, vmax))
    png = os.path.join(os.path.dirname(iq) or '.', 'spec_%s.png' % tag)
    spec_png(db, png, vmin, vmax)
    print('PNG saved: %s' % png)
    lo = max(0, int((center_mhz * 1e6 - 80e3 - fr[0]) / (fr[1] - fr[0])))
    hi = min(len(fr), int((center_mhz * 1e6 + 80e3 - fr[0]) / (fr[1] - fr[0])) + 1)
    ascii_spec(db[:, lo:hi], fr[lo:hi], t0s, dt, vmin, vmax)


if __name__ == '__main__':
    main()
