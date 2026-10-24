#!/usr/bin/env python3
"""Decode the bit payload of one packet in a raw IQ recording.

Demodulates a narrow channel (8 kHz LP, 100 kS/s) around a chosen center,
extracts ON/OFF runs, auto-detects the symbol encoding (PWM: two ON widths;
PPM: two gap widths), prints the bit stream under both polarities, hex
dumps, exact checksum matches (last byte = sum / xor / negated sum of the
earlier bytes), and — only for readings with a checksum match — plausible
temperature/humidity layouts (12-bit temp, offset 400, 0.1 C; humidity byte).

Usage:
  .venv/bin/python tools/iq433/decode.py <rec.iq.u8> <t_start_s> <dur_s> <center_mhz> [tag]

Pick t_start/dur to cover exactly one packet (see catalog.py for packet times).
"""
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit('/', 2)[0])
from iq433lib import bits_to_hex, bits_to_hex_lsb, merge_close, open_iq, runs, shift_env, tuner_hz


def two_cluster_bits(values, ratio=None):
    """Split values into two clusters at the largest gap of the sorted
    values; return a bit string (lower cluster = 0, upper = 1) and the
    split threshold. Falls back to the median*ratio rule when the values
    do not separate."""
    v = np.sort(np.asarray(values, dtype=float))
    if len(v) < 2:
        return '0' * len(v), 0.0
    gaps = np.diff(v)
    i = int(np.argmax(gaps))
    if gaps[i] > 0.25 * v[-1]:  # a real separation
        thr = (v[i] + v[i + 1]) / 2.0
    else:
        thr = np.median(v) * (ratio or 1.5)
    return ''.join('1' if x > thr else '0' for x in values), thr


def readings(bits):
    """All useful readings of a bit string: (label, bits) with polarity and
    a 0/7-bit preamble skip."""
    out = []
    for inv in (False, True):
        b = bits.translate(str.maketrans('01', '10')) if inv else bits
        for pre in (0, 7):
            out.append((('inv ' if inv else '    ') + ('pre=%d' % pre), b[pre:]))
    return out


def main():
    iq = sys.argv[1]
    t0 = float(sys.argv[2])
    dur = float(sys.argv[3])
    center_mhz = float(sys.argv[4])
    tag = sys.argv[5] if len(sys.argv) > 5 else 'pkt'
    tuner = tuner_hz(iq)
    mm = open_iq(iq)
    env, fs2 = shift_env(mm, t0, t0 + dur, center_mhz, tuner, lp=8000.0, dec=24)
    med = np.median(env)
    p99 = np.percentile(env, 99)
    thr = med + 0.45 * (p99 - med)
    starts, ends = runs(env, thr)
    starts, ends = merge_close(starts, ends, int(80 * fs2 / 1e6))
    on_us = (ends - starts) / fs2 * 1e6
    gap_us = np.empty(len(starts))
    for i in range(len(starts) - 1):
        gap_us[i] = (starts[i + 1] - ends[i]) / fs2 * 1e6
    gap_us[-1] = (len(env) - ends[-1]) / fs2 * 1e6
    print('=== %s: t=%.3f dur=%.3f center %.6f MHz ===' % (tag, t0, dur, center_mhz))
    print('env med %.3f p99 %.3f thr %.3f, pulses %d' % (med, p99, thr, len(starts)))
    print('ON us :', ' '.join('%.0f' % v for v in on_us))
    print('GAP us:', ' '.join('%.0f' % v for v in gap_us))

    intra = gap_us[gap_us < 3000]  # us: gaps inside the packet (inter-packet gap excluded)
    pwm_bits, pwm_thr = two_cluster_bits(on_us)
    ppm_bits, ppm_thr = two_cluster_bits(intra)
    on_lo, on_hi = np.percentile(on_us, 10), np.percentile(on_us, 90)
    gap_lo, gap_hi = np.percentile(intra, 10), np.percentile(intra, 90)
    print('ON widths: p10 %.0f p90 %.0f us (split %.0f); gaps: p10 %.0f p90 %.0f us (split %.0f)' % (
        on_lo, on_hi, pwm_thr, gap_lo, gap_hi, ppm_thr))
    kinds = []
    if on_hi > on_lo * 1.6:
        kinds.append(('PWM (ON width, %d bits)' % len(pwm_bits), pwm_bits))
    if gap_hi > gap_lo * 1.6:
        kinds.append(('PPM (gap width, %d bits)' % len(ppm_bits), ppm_bits))
    if not kinds:
        kinds.append(('single-level (constant ON and gap)', '?' * len(on_us)))
    print('encoding: %s' % '; '.join(k for k, _ in kinds))

    for name, bits in kinds:
        print('%s: %s' % (name, bits))
        anchored = []
        for label, b in readings(bits):
            bb = b[:len(b) // 8 * 8]
            if len(bb) < 8:
                continue
            print('  %s MSB: %s | LSB: %s' % (label, bits_to_hex(bb), bits_to_hex_lsb(bb)))
            if len(bb) < 24:
                continue
            by = [int(bb[i:i + 8], 2) for i in range(0, len(bb), 8)]
            s = sum(by[:-1]) & 0xFF
            x = 0
            for v in by[:-1]:
                x ^= v
            if by[-1] in (s, x, (s + 1) & 0xFF, x ^ 0xFF, (-s) & 0xFF):
                print('  *** CHECKSUM %s bytes %s: last=%02X sum=%02X xor=%02X' % (
                    label, ' '.join('%02X' % v for v in by), by[-1], s, x))
                anchored.append((label, by))
        for label, by in anchored:
            for i in range(len(by) - 2):
                v = (by[i] << 4) | (by[i + 1] >> 4)
                t = (v - 400) * 0.1
                if -10 <= t <= 45:
                    for j, h in enumerate(by):
                        if 20 <= h <= 90:
                            print('    %s: temp~%.1fC (bytes %d-%d), hum~%d%% (byte %d)' % (
                                label, t, i, i + 1, h, j))


if __name__ == '__main__':
    main()
