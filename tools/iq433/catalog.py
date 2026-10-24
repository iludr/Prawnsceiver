#!/usr/bin/env python3
"""Packet catalog of a raw IQ recording: who is transmitting, where, what.

For each time window: find the spectral peaks (max-hold, floor+12 dB),
demodulate a narrow channel at each peak, split the envelope into packets
(gaps > 3 ms), and report the dominant repeated bit pattern per signal
(identical repeats = a fixed-ID device), plus the raw ON/GAP runs of one
packet. Packets with >= 12 symbols only.

Usage:
  .venv/bin/python tools/iq433/catalog.py <rec.iq.u8> <t0> <t1> [<t0> <t1> ...]
  (no windows: the whole capture)
"""
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit('/', 2)[0])
from iq433lib import (find_peaks, merge_close, nsamples, open_iq, ppm_bits,
                      runs, shift_env, spectrum, split_packets, tuner_hz)


def catalog(mm, tuner, t0, t1, f_lo=433_820_000, f_hi=434_040_000):
    print('########## window t=%.2f..%.2f ##########' % (t0, t1))
    fr, dba, dbm = spectrum(mm, t0, t1, tuner)
    band = (fr > f_lo) & (fr < f_hi)
    frb, dbab, dbmb = fr[band], dba[band], dbm[band]
    floor = np.median(dbab)
    peaks = find_peaks(frb, dbmb, floor, min_raise=12.0)
    peaks = [(f, v) for f, v in peaks if abs(f - tuner) > 4000.0]  # skip the DC/LO spike
    print('strong peaks (maxhold > floor+12 dB, DC spike skipped):')
    for f, v in peaks:
        i = np.argmin(np.abs(frb - f))
        print('   %12.1f Hz  max %6.1f (floor+%4.1f)  avg %6.1f' % (
            f, dbmb[i], dbmb[i] - floor, dbab[i]))
    for f, v in peaks:
        env, fs2 = shift_env(mm, t0, t1, f / 1e6, tuner, lp=8000.0, dec=24)
        med = np.median(env)
        p99 = np.percentile(env, 99)
        if p99 < med * 1.8:
            continue
        thr = med + 0.45 * (p99 - med)
        starts, ends = runs(env, thr)
        starts, ends = merge_close(starts, ends, int(80 * fs2 / 1e6))
        pkts = split_packets(starts, ends, fs2, len(env))
        good = [p for p in pkts if len(p[1]) >= 12]
        if not good:
            continue
        sig = {}
        for tpk, ons, gps in good:
            bits = ppm_bits(gps) if len(gps) else ''
            sig.setdefault((len(ons), bits), []).append(tpk)
        (n, bits), tlist = max(sig.items(), key=lambda kv: len(kv[1]))
        if len(tlist) < 2 and n < 30:
            continue  # not a repeating device and not a long packet: skip
        print('--- signal @ %.1f Hz: %d packets (>=12 sym); dominant pattern n=%d repeated %d' % (
            f, len(good), n, len(tlist)))
        print('    PPM bits (%d): %s%s' % (len(bits), bits[:96], '...' if len(bits) > 96 else ''))
        bb = bits[:len(bits) // 8 * 8]
        print('    hex: %s' % ' '.join('%02X' % int(bb[i:i + 8], 2)
                                       for i in range(0, len(bb), 8)))
        for tpk, ons, gps in good:
            if (len(ons), ppm_bits(gps) if len(gps) else '') == (n, bits):
                print('    raw ON us :', ' '.join('%.0f' % v for v in ons))
                print('    raw GAP us:', ' '.join('%.0f' % v for v in gps))
                break
        print('    packet times: %s' % ' '.join('%.3f' % (t0 + t) for t in tlist[:14]))
    print()


def main():
    iq = sys.argv[1]
    tuner = tuner_hz(iq)
    mm = open_iq(iq)
    n = nsamples(mm)
    dur = n / 2_400_000
    if len(sys.argv) > 3:
        args = [float(a) for a in sys.argv[2:]]
        wins = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    else:
        wins = [(0.0, dur)]
    print('capture %s: %.2f s, tuner %.1f Hz' % (iq, dur, tuner))
    for t0, t1 in wins:
        catalog(mm, tuner, t0, t1)


if __name__ == '__main__':
    main()
