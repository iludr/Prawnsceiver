#!/usr/bin/env python3
"""Identify emitters in a prawnceiver raw-IQ recording (u8, sidecar .json).

For every emitter found in the recording's analysis.json (or computed from
an averaged spectrum): power-vs-time (burstiness), fine frequency track
(Doppler drift), POCSAG 1200 lock attempt, and a match against the known
137.0-138.0 MHz Orbcomm S-channel raster (S1..S10 + gateway) — the common
way a "varying-strength signal around 137 MHz" turns out to be an Orbcomm
satellite pass: several S-channels up at once, all shifted by the same
Doppler offset.

Usage:
  cd /home/prawnsdr/prawnceiver
  .venv/bin/python -u tools/iqband/analyze.py <recording-base-without-ext>
  e.g. .venv/bin/python -u tools/iqband/analyze.py \
       /mnt/nfs/prawnceiver/recordings/manual/Manual_137.7137MHz_20261111_180528
"""
import os, sys, json
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from noaa_receiver.sdr.dsp import iq_to_complex, frequency_shift, new_state
from noaa_receiver.config import SDR_RATE
from noaa_receiver.decoding.pocsag import decode_band

FS = SDR_RATE

# ITU-R1 137-138 MHz Orbcomm downlink channel raster (25 kHz grid)
ORBCOMM_CHANNELS = {  # name -> Hz
    'S1': 137_200_000, 'S2': 137_225_000, 'S3': 137_250_000, 'S11': 137_287_500,
    'S12': 137_312_500, 'S4': 137_435_000, 'S5': 137_460_000,
    'gateway': 137_560_000, 'S6': 137_662_500, 'S7': 137_678_000,
    'S8': 137_712_500, 'S9': 137_737_500, 'S10': 137_800_000,
}


def load_meta(base):
    meta = json.load(open(base + '.json'))
    return meta


def emitters_of(base, meta):
    apath = base + '.analysis.json'
    if os.path.exists(apath):
        a = json.load(open(apath))
        return [(e['abs_mhz'] * 1e6, e) for e in a.get('emitters', [])
                if not e.get('dc_or_lo_spike')]
    return []


def track(path, center_hz, tuner_hz, emitters, slice_s=1.0):
    mm = np.memmap(path, dtype=np.uint8, mode='r')
    n = len(mm) // 2
    sl = int(slice_s * FS)
    nfft = 8192
    win = np.hanning(nfft)
    st = new_state()
    out = {}
    for ef, _ in emitters:
        out[round(ef)] = {'t': [], 'dbfs': []}
    fine = {round(ef): [] for ef, _ in emitters}   # 0.25 s fine freq track
    stf = new_state()
    nfft2 = 16384
    win2 = np.hanning(nfft2)
    k = 0
    t = 0.0
    while k + sl <= n:
        c = frequency_shift(iq_to_complex(bytes(mm[k*2:(k+sl)*2])),
                            tuner_hz - center_hz, FS, st)
        m = (len(c) - nfft) // 2
        if m >= 0:
            sp = np.abs(np.fft.fftshift(np.fft.fft(c[m:m+nfft] * win)))
            freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS)) + center_hz
            for ef, _ in emitters:
                key = round(ef)
                sel = np.abs(freqs - ef) < 8000
                ii = np.nonzero(sel)[0]
                out[key]['t'].append(t)
                out[key]['dbfs'].append(20 * np.log10(sp[ii].max() / nfft + 1e-12))
        t += slice_s
        k += sl
    # fine 0.25 s track on a second pass (cheap enough at these sizes)
    k = 0
    t = 0.0
    sl2 = int(0.25 * FS)
    while k + sl2 <= n:
        c = frequency_shift(iq_to_complex(bytes(mm[k*2:(k+sl2)*2])),
                            tuner_hz - center_hz, FS, stf)
        m = (len(c) - nfft2) // 2
        if m >= 0:
            sp = np.abs(np.fft.fftshift(np.fft.fft(c[m:m+nfft2] * win2)))
            freqs = np.fft.fftshift(np.fft.fftfreq(nfft2, 1 / FS)) + center_hz
            for ef, _ in emitters:
                sel = np.abs(freqs - ef) < 8000
                ii = np.nonzero(sel)[0]
                fine[round(ef)].append((t, freqs[ii[np.argmax(sp[ii])]]))
        t += 0.25
        k += sl2
    return out, fine


def pocsag_lock(path, center_hz, tuner_hz, ef):
    mm = np.memmap(path, dtype=np.uint8, mode='r')
    n = len(mm) // 2
    st = new_state()
    def getc(k0, nn):
        return frequency_shift(iq_to_complex(bytes(mm[k0*2:(k0+nn)*2])),
                               tuner_hz - center_hz, FS, st)
    return decode_band(getc, n, ef - center_hz, FS, baud=1200)


def main():
    base = sys.argv[1].rstrip('/')
    meta = load_meta(base)
    iq = base + '.iq.u8'
    center = meta['radio']['center_freq_hz']
    tuner = meta['radio']['tuner_freq_hz']
    print(f"recording {os.path.basename(base)}: {os.path.getsize(iq)/2/FS:.1f} s, "
          f"display center {center/1e6:.4f} MHz, tuner {tuner/1e6:.4f} MHz")
    em = emitters_of(base, meta)
    if not em:
        print("no analysis.json emitters; nothing to track")
        return
    series, fine = track(iq, center, tuner, em)
    for ef, info in em:
        key = round(ef)
        s = np.array(series[key]['dbfs'])
        fr = np.array([f for _, f in fine[key]])
        tt = np.array([t for t, _ in fine[key]])
        slope = np.polyfit(tt, fr, 1)[0] if len(tt) > 5 else 0.0
        # nearest Orbcomm raster channel
        ch = min(ORBCOMM_CHANNELS.items(), key=lambda kv: abs(kv[1] - ef))
        dop = ef - ch[1]
        p = pocsag_lock(iq, center, tuner, ef)
        print(f"\nemitter {ef/1e6:.5f} MHz  (preflight: {info.get('modulation', {}).get('type')}, "
              f"bw {info.get('occupied_bw_khz')} kHz)")
        print(f"  power: min {s.min():.1f} / med {np.median(s):.1f} / max {s.max():.1f} dBFS "
              f"(swing {s.max()-s.min():.1f} dB over {len(s)} s)")
        print(f"  freq drift {slope:+.1f} Hz/s, span {(fr.max()-fr.min()):.0f} Hz over {tt[-1]:.0f} s")
        print(f"  nearest Orbcomm channel: {ch[0]} @ {ch[1]/1e6:.4f} MHz -> Doppler offset {dop:+.0f} Hz")
        print(f"  POCSAG 1200: {'LOCK, ' + json.dumps(p)[:200] if p else 'no sync'}")
    dobs = [ef - min(ORBCOMM_CHANNELS.values(), key=lambda c: abs(c - ef)) for ef, _ in em]
    if len(em) > 1 and max(dobs) - min(dobs) < 1500:
        print(f"\n--> all emitters share one Doppler offset ({np.mean(dobs):+.0f} Hz): "
              f"one Orbcomm satellite passing on channels "
              + ", ".join(min(ORBCOMM_CHANNELS.items(), key=lambda kv: abs(kv[1]-ef))[0] for ef, _ in em))


if __name__ == '__main__':
    main()
