"""LRPT decode chain: baseband-offset measurement gates and SatDump
product pickup.

_measure_signal_offset must find real satellite signals (wide drifting
or stationary plateaus, drifting narrow carriers) while rejecting the
local land-mobile carriers that park near the downlink bands (strong,
narrow, ZERO Doppler drift - see the 2026-10-10 EVENTLOG entry).

All synthetic signals ride on a broadband noise floor like a real
recording; a floorless plateau covering half the capture would put the
spectrum median ON the signal and is not a realistic case.

Run: python -u tests/test_lrpt.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import tempfile  # noqa: E402
os.environ.setdefault("PRAWN_DB_FILE",
                     os.path.join(tempfile.mkdtemp(), "station.db"))
from noaa_receiver.decoding.decode import _measure_signal_offset, _pick_product_png
from noaa_receiver.config import (LOCAL_CARRIER_BASEBAND_HZ,
                                   LOCAL_CARRIER_NOTCH_HZ, METEOR_LRPT_HZ,
                                   SDR_OFFSET_HZ, SDR_RATE)

FS = SDR_RATE
DUR = 24.0  # seconds per synthetic recording


def write_iq(path, c):
    """Complex baseband -> raw u8 IQ bytes (the rtl_tcp recording format)."""
    a = np.empty((len(c), 2), dtype=np.uint8)
    a[:, 0] = np.clip(np.round(c.real + 127.5), 0, 255)
    a[:, 1] = np.clip(np.round(c.imag + 127.5), 0, 255)
    with open(path, 'wb') as f:
        f.write(a.tobytes())


def floor(seed):
    """Broadband receiver noise floor (full 2.4 MHz capture)."""
    rng = np.random.default_rng(seed)
    n = int(DUR * FS)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 10.0


def plateau(width_hz, lo_hz, hi_hz, amp=28.0, seed=1):
    """Band-limited noise whose center ramps lo_hz -> hi_hz over the clip
    (a wide digital downlink under Doppler), on the noise floor."""
    rng = np.random.default_rng(seed + 100)
    n = int(DUR * FS)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * amp
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / FS)
    X[np.abs(f) > width_hz / 2] = 0
    x = np.fft.ifft(X)
    t = np.arange(n) / FS
    sweep = lo_hz + (hi_hz - lo_hz) * (t / t[-1])
    return floor(seed) + x * np.exp(2j * np.pi * np.cumsum(sweep) / FS)


def carrier(freq_hz, amp=90.0, lo_hz=None, hi_hz=None, seed=2):
    """A tone, optionally Doppler-swept lo_hz -> hi_hz, on the floor."""
    n = int(DUR * FS)
    t = np.arange(n) / FS
    if lo_hz is None:
        c = amp * np.exp(2j * np.pi * freq_hz * t)
    else:
        sweep = lo_hz + (hi_hz - lo_hz) * (t / t[-1])
        c = amp * np.exp(2j * np.pi * np.cumsum(sweep) / FS)
    return floor(seed) + c


def measure(c, bw):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "SAT_TEST_20261010_120000.wav")
    write_iq(p, c)
    return _measure_signal_offset(p, bw)


def check(name, cond, detail=""):
    assert cond, f"{name}: {detail}"
    print(f"  {name} OK")


print("1. drifting wide plateau (real LRPT-like signal, Doppler-swept)")
m = measure(plateau(120_000, -63_000, -57_000), 120_000)
check("found", m is not None, f"got {m}")
check("centered on the plateau", abs(m - (-60_000)) < 5000, f"measured {m:.0f} Hz")

print("2. stationary wide plateau (low pass, little Doppler: width gate)")
m = measure(plateau(120_000, -60_000, -59_000), 120_000)
check("found", m is not None, f"got {m}")
check("centered", abs(m - (-59_500)) < 5000, f"measured {m:.0f} Hz")

print("3. drifting narrow carrier (narrowband, Doppler-swept)")
m = measure(carrier(0, lo_hz=-61_500, hi_hz=-58_500), 6_000)
check("found", m is not None, f"got {m}")
check("centered", abs(m - (-60_000)) < 3000, f"measured {m:.0f} Hz")

print("4. STATIONARY narrow carrier = local interferer -> rejected")
m = measure(carrier(-67_300), 120_000)
check("rejected (None)", m is None, f"got {m}")
m = measure(carrier(-67_300), 6_000)
check("rejected at the narrow window too", m is None, f"got {m}")

print("5. drifting plateau + stationary interferer -> plateau wins")
m = measure(plateau(120_000, -63_000, -57_000) + carrier(-67_300, amp=60, seed=3), 120_000)
check("found", m is not None, f"got {m}")
check("centered on the plateau, not the carrier",
      abs(m - (-60_000)) < 6000, f"measured {m:.0f} Hz (carrier at -67.3k)")

print("6. pure noise -> rejected")
m = measure(floor(7), 120_000)
check("rejected (None)", m is None, f"got {m}")

print("7. pass-like envelope: plateau strong only near culmination")
# The live failure of 2026-10-17: rise/set slices hold nothing but
# floor and only the mid-pass slices carry the downlink (best slice
# 8.7x its own floor, 6-slice average 2.8x — under the confidence
# gate). A detection that averages the slices misses every real pass.
n = int(DUR * FS)
rng = np.random.default_rng(205)
x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 30.0
X = np.fft.fft(x)
f = np.fft.fftfreq(n, 1.0 / FS)
X[np.abs(f) > 60_000] = 0
x = np.fft.ifft(X)
t = np.arange(n) / n
env = np.clip(1.0 - 4.0 * np.abs(t - 0.5), 0.0, 1.0)   # zero at rise/set
ts = np.arange(n) / FS
sweep = -63_000 + 6_000 * (ts / ts[-1])
m = measure(floor(6) + x * env * np.exp(2j * np.pi * np.cumsum(sweep) / FS),
            120_000)
check("found", m is not None, f"got {m}")
check("centered on the plateau", m is not None and abs(m - (-60_000)) < 5000,
      f"measured {m if m is None else f'{m:.0f}'} Hz")

print("8. NEW TUNE (137.9000): interferer at -55 kHz, inside the LRPT band")
# The station tunes 137.9000 now (dongle at 137.9600 with the 60 kHz
# offset), so the 137.905 MHz land-mobile carrier lands at -55 kHz,
# inside the LRPT band (-64..-60 kHz center, ~72 kHz wide). The old
# interferer geometry above (-67.3 kHz) matched the retired 137.9125
# tune and the old hardcoded notch.
check("notch derivation: baseband = carrier - (target + offset)",
      LOCAL_CARRIER_BASEBAND_HZ == LOCAL_CARRIER_NOTCH_HZ - (METEOR_LRPT_HZ + SDR_OFFSET_HZ),
      f"baseband {LOCAL_CARRIER_BASEBAND_HZ}")
check("carrier lands at -55 kHz for the 137.9000 tune",
      LOCAL_CARRIER_BASEBAND_HZ == -55_000, f"baseband {LOCAL_CARRIER_BASEBAND_HZ}")
m = measure(plateau(72_000, -65_000, -59_000) + carrier(-55_000, amp=60, seed=3), 120_000)
check("found", m is not None, f"got {m}")
check("centered on the plateau, not the carrier",
      m is not None and abs(m - (-62_000)) < 2000,
      f"measured {m} Hz (carrier at -55k, plateau center -62k)")

print("9. _pick_product_png: largest PNG wins, non-images ignored")
d = tempfile.mkdtemp()
for name, size in [("meteor.cadu", 50), ("dataset.json", 300),
                   ("msu_mr_ch1.png", 400), ("msu_mr_rgb_composite.png", 90_000),
                   ("msu_mr_rgb_composite_map.png", 50_000),
                   ("msu_mr_ir.png", 800)]:
    with open(os.path.join(d, name), 'wb') as f:
        f.write(b"\0" * size)
pick = _pick_product_png(d)
check("map overlay preferred over the bigger non-map composite",
      pick is not None and pick.endswith("msu_mr_rgb_composite_map.png"), str(pick))
# SatDump v1.2.0 nests instrument images one level deep (<out>/MSU-MR/)
nested = tempfile.mkdtemp()
sub = os.path.join(nested, "MSU-MR")
os.makedirs(sub)
for name, size in (("meteor_m2-x_lrpt.cadu", 50), ("dataset.json", 300),
                   ("MSU-MR/msu_mr_Channel_1.png", 400),
                   ("MSU-MR/msu_mr_rgb_composite.png", 90_000),
                   ("MSU-MR/msu_mr_rgb_composite_map.png", 50_000)):
    with open(os.path.join(nested, name), 'wb') as f:
        f.write(b"\0" * size)
pick = _pick_product_png(nested)
check("nested MSU-MR images found, map overlay wins",
      pick is not None and pick.endswith("msu_mr_rgb_composite_map.png"), str(pick))
empty = tempfile.mkdtemp()
with open(os.path.join(empty, "meteor.cadu"), 'wb') as f:
    f.write(b"\0")
check("no image -> None", _pick_product_png(empty) is None)

print("test_lrpt: all OK")
os._exit(0)
