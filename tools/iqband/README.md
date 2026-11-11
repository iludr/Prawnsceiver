# tools/iqband/ — identify emitters in a raw-IQ recording

General-purpose companion to `tools/iq433/` (which is 433 MHz-specific):
takes any prawnceiver manual/pass recording (u8 IQ + sidecar `.json`) and,
for every emitter the server-side preflight (`analysis.json`) found:

- power-vs-time (burstiness / varying strength),
- fine frequency track (Doppler drift in Hz/s and total span),
- POCSAG 1200 lock attempt (repo decoder `noaa_receiver.decoding.pocsag`),
- match against the 137.0–138.0 MHz **Orbcomm S-channel raster**
  (S1..S12 + gateway, 25 kHz grid), reporting the implied Doppler offset —
  several S-channels sharing one Doppler offset = one Orbcomm satellite
  passing (the classic "varying-strength signal around 137 MHz").

Run (venv python — system python3 has no numpy; one script at a time):

    cd /home/prawnsdr/prawnceiver
    .venv/bin/python -u tools/iqband/analyze.py \
        /mnt/nfs/prawnceiver/recordings/manual/Manual_137.7137MHz_20261111_180528

(archiver moves recordings from `/var/log/noaa/recordings/manual/` to the
NFS share — analyse the NFS copy.)

## orbcomm_decode.py — offline Orbcomm decode of a recording

    .venv/bin/python -u tools/iqband/orbcomm_decode.py \
        /mnt/nfs/prawnceiver/recordings/manual/Manual_137.7137MHz_20261111_180528

Drives the repo's production Orbcomm demodulator
(`noaa_receiver.decoding.orbcomm`: per-channel polyphase SDPSK demod,
sync-anchored 96-bit packet scan) over the file. Channel rotator:
raw baseband has DC at the tuner freq, so each channel is shifted by
`tuner - channel_freq` (exact; `new_channel_state`'s own `shift_hz`
assumes display center == ORBCOMM_CENTER_HZ). Packets print with type,
hex, sat_id (Sync) and parsed ephemeris (0x1F).

2026-11-11: identified the two emitters in
`Manual_137.7137MHz_20261111_180249/180528` (137.7137 MHz tune) as Orbcomm
channels **S6 (137.6625)** and **S10 (137.8000)**, both shifted by the same
~+1.1 kHz Doppler — one Orbcomm LEO passing, bursty SDPSK packets =
the varying strength. The POCSAG sync "lock" on S10 is a false positive
on the Orbcomm waveform at low SNR (recurring tone "pages", no text).
NOTE: these two manual captures are too weak to decode (emitters only
~12 dB over the noise floor, bursty; the demod never locks symbol phase
— 0 packets in 48 s and 120 s). For decodable Orbcomm, use the live
Orbcomm role (137.5 MHz) during a higher pass; this tool validates on
synthetic packets but finds nothing in these edge-of-pass captures.
