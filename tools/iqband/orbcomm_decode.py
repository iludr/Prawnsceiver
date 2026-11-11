#!/usr/bin/env python3
"""Offline Orbcomm decode of a prawnceiver raw-IQ recording (u8 + sidecar).

Drives the repo's own Orbcomm demodulator (noaa_receiver.decoding.orbcomm:
per-channel polyphase SDPSK demod + sync-anchored packet scan) over a
recorded file instead of a live rtl_tcp stream. Channels come from the
recording's analysis.json emitters (the DC/LO spike is skipped); each is
demodulated at its measured frequency, packets are deduped and printed
with type + satellite id (Sync packets) + parsed ephemeris (0x1F).

Usage:
  cd /home/prawnsdr/prawnceiver
  .venv/bin/python -u tools/iqband/orbcomm_decode.py <recording-base>
  e.g. ... /mnt/nfs/prawnceiver/recordings/manual/Manual_137.7137MHz_20261111_180528
"""
import os, sys, json, time
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from noaa_receiver.sdr.dsp import iq_to_complex, frequency_shift
from noaa_receiver.config import SDR_RATE, SDR_OFFSET_HZ, IQ_BLOCK
from noaa_receiver.decoding import orbcomm
from noaa_receiver.decoding.orbcomm import (new_channel_state, demod_batch,
                                             PACKET_TYPES, EPHEMERIS,
                                             parse_ephemeris)

FS = SDR_RATE


def main():
    base = sys.argv[1].rstrip('/')
    meta = json.load(open(base + '.json'))
    iq = base + '.iq.u8'
    center = meta['radio']['center_freq_hz']
    tuner = meta['radio']['tuner_freq_hz']
    dur = os.path.getsize(iq) / 2 / FS
    print(f"recording {os.path.basename(base)}: {dur:.1f} s, center {center/1e6:.4f} MHz, "
          f"tuner {tuner/1e6:.4f} MHz")

    apath = base + '.analysis.json'
    emitters = []
    if os.path.exists(apath):
        for e in json.load(open(apath)).get('emitters', []):
            if not e.get('dc_or_lo_spike'):
                emitters.append(e['abs_mhz'] * 1e6)
    if not emitters:   # fall back: single band center
        emitters = [center]
    print("channels: " + ", ".join(f"{ef/1e6:.5f}" for ef in emitters))

    # channel state takes the absolute RF freq; _demod_channels rotates raw baseband by shift_hz
    chans = []
    for ef in emitters:
        st = new_channel_state('offline', f"{ef/1e3:.0f}k", ef)
        # exact per-channel rotator for THIS recording: raw baseband has DC
        # at the tuner freq, so shift = tuner - channel (new_channel_state's
        # own shift_hz assumes display center == ORBCOMM_CENTER_HZ)
        st['shift_hz'] = int(tuner - ef)
        chans.append((ef, st))

    mm = np.memmap(iq, dtype=np.uint8, mode='r')
    n = len(mm) // 2
    packets = []
    t0 = time.time()
    k = 0
    while k + IQ_BLOCK <= n:
        c = iq_to_complex(bytes(mm[k*2:(k+IQ_BLOCK)*2]))
        for ef, st in chans:
            cc = frequency_shift(c, st['shift_hz'], FS, st)
            for data in demod_batch(cc, st):
                hexs = bytes(data).hex()
                if hexs in st['seen']:
                    continue
                st['seen'].append(hexs)
                packets.append((ef, data, st))
        k += IQ_BLOCK
        if (k // IQ_BLOCK) % 2000 == 0:
            print(f"  ... {k/FS:.0f}/{dur:.0f} s, {len(packets)} packets "
                  f"({time.time()-t0:.0f}s wall)", flush=True)
    print(f"done in {time.time()-t0:.0f}s wall: {len(packets)} unique packets")

    by_type = {}
    for ef, data, st in packets:
        t = PACKET_TYPES.get(data[0], f"0x{data[0]:02x}")
        by_type[t] = by_type.get(t, 0) + 1
    print("by type: " + ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())))
    for ef, data, st in packets[:40]:
        t = PACKET_TYPES.get(data[0], '?')
        line = f"  {ef/1e6:.5f} MHz {t:10s} {bytes(data).hex()}"
        if data[0] == 0x65:
            line += f"  sat_id={bytes(data[6:8]).hex()}"
        if data[0] == EPHEMERIS:
            try:
                eph = parse_ephemeris(bytes(data))
                line += f"  eph={json.dumps(eph)[:160]}"
            except Exception as e:
                line += f"  eph parse err {e}"
        print(line)


if __name__ == '__main__':
    main()
