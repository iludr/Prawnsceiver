#!/usr/bin/env python3
"""Pulse/group structure of a saved envelope (from envelope.py).

Prints the envelope dB histogram (to pick a noise-safe threshold), pulse
ON/OFF run statistics at several thresholds, packet grouping, group timing,
a per-0.5 s duty timeline, and the ON/OFF width histograms inside long
groups.

Usage:
  .venv/bin/python tools/iq433/structure.py <env.npy> [rate_hz=100000]
"""
import sys

import numpy as np


def runs(on):
    d = np.diff(on.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if on[0]:
        starts = [0] + starts
    if on[-1]:
        ends = ends + [len(on)]
    return np.array(starts), np.array(ends)


def main():
    env = np.load(sys.argv[1])
    fs = float(sys.argv[2]) if len(sys.argv) > 2 else 100000.0
    dt = 1.0 / fs
    floor = np.median(env)
    print('envelope: %d samples @ %.0f Hz (%.2f s), floor %.4f' % (len(env), fs, len(env) / fs, floor))
    db = 20 * np.log10(env / floor + 1e-12)
    print('dB rel floor: p50 %.1f p90 %.1f p99 %.1f p99.9 %.1f p99.99 %.1f max %.1f' % tuple(
        np.percentile(db, [50, 90, 99, 99.9, 99.99, 100])))
    h, edges = np.histogram(db, bins=np.arange(0, 40, 1.0))
    print('dB histogram (1 dB bins, counts > 200):')
    for i in range(len(h)):
        if h[i] > 200:
            print('  %2.0f dB : %d' % (edges[i], h[i]))

    for dbthr in (15, 20, 25):
        thr = floor * 10 ** (dbthr / 20)
        on = env > thr
        starts, ends = runs(on)
        on_s = (ends - starts) * dt
        print('thr +%d dB: pulses %d  ON%% %.3f  ONus med %.0f p90 %.0f max %.0f' % (
            dbthr, len(starts), 100 * on.mean(), np.median(on_s) * 1e6,
            np.percentile(on_s, 90) * 1e6, on_s.max() * 1e6))

    # main threshold: +20 dB
    thr = floor * 10 ** (20 / 20)
    on = env > thr
    starts, ends = runs(on)
    on_s = (ends - starts) * dt
    off_s = (np.r_[starts[1:], len(on)] - ends) * dt
    if len(starts) == 0:
        print('no pulses above +20 dB')
        return
    print('pulses above +20 dB: %d, first/last t: %.3f / %.3f s' % (
        len(starts), starts[0] * dt, ends[-1] * dt))
    if len(starts) > 1:
        gaps = np.diff(starts) * dt
        print('start-to-start s: med %.4f min %.4f max %.4f' % tuple(
            np.percentile(gaps, [50, 0, 100])))

    # groups (gap < 3 ms)
    groups = []
    g = [0]
    for i in range(1, len(starts)):
        if off_s[i - 1] < 3e-3:
            g.append(i)
        else:
            groups.append(g)
            g = [i]
    groups.append(g)
    print('groups (gap<3ms): %d' % len(groups))
    ginfo = []
    for g in groups:
        t0 = starts[g[0]] * dt
        t1 = ends[g[-1]] * dt
        ginfo.append((t0, t1 - t0, len(g), on_s[g].sum()))
    durs = np.array([x[1] for x in ginfo])
    npul = np.array([x[2] for x in ginfo])
    print('group dur ms: med %.3f p90 %.3f max %.3f' % tuple(np.percentile(durs, [50, 90, 100]) * 1e3))
    print('group pulses: med %.0f p90 %.0f max %.0f' % tuple(np.percentile(npul, [50, 90, 100])))
    order = np.argsort(npul)[::-1]
    print('top groups by pulse count:')
    for gi in order[:15]:
        t0, dur, n, ons = ginfo[gi]
        print('  t=%7.3f s  dur=%8.1f us  pulses=%3d  ONsum=%8.1f us' % (
            t0, dur * 1e6, n, ons * 1e6))

    # timeline: ON duty per 0.5 s
    nb = int(len(on) / (fs * 0.5))
    tl = on[:nb * int(fs * 0.5)].reshape(nb, int(fs * 0.5)).mean(axis=1)
    print('ON duty per 0.5 s bin:')
    print(' '.join('%.3f' % v for v in tl))

    # ON/OFF histograms inside long groups (> 20 ms)
    long_idx = [i for i, x in enumerate(ginfo) if x[1] > 20e-3]
    inside = np.zeros(len(starts), bool)
    for gi in long_idx:
        inside[groups[gi]] = True
    if inside.any():
        on_in = on_s[inside]
        off_in = off_s[inside]
        print('pulses inside long groups: %d' % inside.sum())
        bins = np.array([0, 20, 40, 60, 80, 100, 150, 200, 300, 500, 1000, 2000, 5000, 1e9]) * 1e-6
        h1, _ = np.histogram(on_in, bins)
        h2, _ = np.histogram(off_in, bins)
        print('  ON us :', ' '.join('%d:%d' % (bins[i] * 1e6, h1[i]) for i in range(len(h1)) if h1[i]))
        print('  OFF us:', ' '.join('%d:%d' % (bins[i] * 1e6, h2[i]) for i in range(len(h2)) if h2[i]))


if __name__ == '__main__':
    main()
