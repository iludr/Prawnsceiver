#!/usr/bin/env python3
"""Whole-capture narrowband envelope of a raw IQ recording.

Shifts a chosen RF center to 0 Hz, low-passes, decimates to 100 kS/s and
saves the float32 envelope for structure.py. The shift/LP/decimation are
phase-continuous across chunks (noaa_receiver.sdr.dsp state).

Usage:
  .venv/bin/python tools/iq433/envelope.py <rec.iq.u8> <center_mhz> <out.npy> [lp_hz=25000] [dec=24]

The tuner center frequency is read from the sidecar .json (radio.tuner_freq_hz).
"""
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit('/', 2)[0])
from iq433lib import FS, open_iq, nsamples, shift_env, tuner_hz


def main():
    iq = sys.argv[1]
    center_mhz = float(sys.argv[2])
    out = sys.argv[3]
    lp = float(sys.argv[4]) if len(sys.argv) > 4 else 25000.0
    dec = int(sys.argv[5]) if len(sys.argv) > 5 else 24
    tuner = tuner_hz(iq)
    mm = open_iq(iq)
    n = nsamples(mm)
    print('capture %s: %d complex samples (%.2f s), tuner %.1f Hz, center %.6f MHz, LP %.0f Hz, dec %d' % (
        iq, n, n / FS, tuner, center_mhz, lp, dec))
    env, fs2 = shift_env(mm, 0, n / FS, center_mhz, tuner, lp=lp, dec=dec)
    np.save(out, env)
    floor = np.median(env)
    print('saved %s: %d samples @ %.0f Hz (%.2f s)' % (out, len(env), fs2, len(env) / fs2))
    print('envelope: floor(med) %.4f  p99 %.3f  p99.9 %.3f  max %.3f' % (
        floor, np.percentile(env, 99), np.percentile(env, 99.9), env.max()))


if __name__ == '__main__':
    main()
