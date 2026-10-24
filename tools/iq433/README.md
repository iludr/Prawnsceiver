# tools/iq433 — offline analysis of raw IQ captures

Scripts for deep-diving a prawnceiver raw IQ recording (`.iq.u8`, interleaved
unsigned 8-bit I/Q, DC 127.5, 2.4 Msps complex) — e.g. identifying an unknown
signal in a manual recording. All DSP reuses `noaa_receiver.sdr.dsp`
(`frequency_shift` / `lowpass` / `decimate_cont`), so block-wise processing is
phase-continuous, exactly like the live demod paths. No scipy — numpy + PIL only.

Run everything with the project venv (system python3 has no numpy):

```
.venv/bin/python tools/iq433/<script>.py ...
```

## Conventions

- The raw stream is centered on the dongle's **tuner frequency**
  (`radio.tuner_freq_hz` in the sidecar `.json` next to the recording; every
  script reads it automatically). Absolute RF = baseband offset + tuner freq.
- The dashboard display value = absolute RF − `offset_tuning_hz` −
  `frequency_correction_hz` (e.g. display 433.9208 → tuner 433.981668 MHz).
- dBFS in the spectra = tone amplitude relative to full scale (u8 127.5).
- Run one script at a time (12 GB RAM; a capture is ~180 MB, the full-rate
  windows are a few hundred MB of float32).

## Workflow

1. `envelope.py <rec.iq.u8> <center_mhz> <out.npy>` — whole-capture
   narrowband envelope (25 kHz LP, 100 kS/s) around a center frequency.
2. `structure.py <env.npy>` — envelope dB histogram (find the noise/signal
   gap and pick a threshold), pulse/group statistics, per-0.5 s duty
   timeline, top groups. This is the coarse "when is anything on" scan.
3. `catalog.py <rec.iq.u8> [t0 t1 ...]` — per window: spectral peaks
   (max-hold, floor+12 dB), narrow-channel demod at each peak, packet
   splitting (gaps > 3 ms), and the dominant repeated bit pattern per
   signal (identical repeats = fixed-ID device). The "who is in the band"
   tool. No windows = whole capture.
4. `full.py <rec.iq.u8> <t0> <dur> <center_mhz> [tag]` — full-rate deep dive
   of one burst: spectral lines, ON/OFF runs at µs resolution,
   instantaneous frequency during ON (OOK vs FSK test), spectrogram PNG
   (`spec_<tag>.png`, written next to the recording) + ASCII with a global
   dB window.
5. `decode.py <rec.iq.u8> <t0> <dur> <center_mhz> [tag]` — packet bit
   decode: auto-detects PWM (two ON widths) vs PPM (two gap widths),
   prints bits under both polarities, hex dumps, checksum and
   temperature/humidity plausibility search.

## Example — the 2026-10-23 433.92 MHz manual recording

```
R=/mnt/nfs/prawnceiver/recordings/manual/Manual_433.9208MHz_20261023_234155.iq.u8
.venv/bin/python tools/iq433/envelope.py $R 433.930985 /tmp/env433.npy
.venv/bin/python tools/iq433/structure.py /tmp/env433.npy
.venv/bin/python tools/iq433/catalog.py $R 21.4 22.6 25.4 26.5 26.9 28.1 30.9 32.1 37.4 38.69
.venv/bin/python tools/iq433/full.py $R 9.02 0.14 433.930985 strong
.venv/bin/python tools/iq433/decode.py $R 9.030 0.116 433.930985 strong
```

What it found there (2026-10-23): a strong OOK/PWM packet at 433.930957 MHz
(55 symbols, ON 488/1467 µs, gap 975 µs, payload 4C 80 58 47 5D C8 with a
sum checksum under S=1/L=0 and a 7-symbol preamble), plus several weaker
repeating PPM packet streams (pulse ~470 µs, gaps ~980/1970 µs) at
433.9295, 433.9008 and 433.9389 MHz — all classic 433.92 MHz ISM
sensor/remote-style OOK telemetry. See EVENTLOG.md 2026-10-23.

Protocol identification (2026-10-24, checked against rtl_433 layouts):
the three weak PPM streams are **Nexus/Rubicson-family** temperature (and
humidity) sensors — 36-bit rows `[id0][id1][flags][temp0..2][0xF][humi/crc]`,
const nibble 0xF present in all three, sync gap ~3.9 ms between repeats:

- 433.9295 MHz — Nexus-style temp/humidity: id 0x03, battery OK, channel 3,
  19.8 °C, 57 %RH (Rubicson CRC does not match → humidity byte is real).
- 433.9008 MHz — Nexus-style temperature-only: id 0x26, battery LOW,
  channel 1, 19.0 °C (humidity byte 0x00).
- 433.9389 MHz — Rubicson/Solight TE44/EMOS E0107T family: id 0xA2,
  battery OK, channel 1, 7.6 °C; last byte matches CRC-8 poly 0x31 init
  0x6c (so it is a checksum, temperature-only sensor).

(rtl_433 brands for this family: Nexus, FreeTec NC-7345, infactory
NX-3980, Solight TE82S, TFA 30.3209, Rubicson, TFA 30.3197, InFactory
PT-310.) Netatmo weather modules are NOT among them: they talk to their
base station on 868 MHz (EU) / 915 MHz (US), not 433.92 MHz — outside this
capture's 432.72–435.12 MHz band entirely (rtl_433 decodes them as
protocol 336 "NetAtmo temp/hum and wind sensors" when tuned to 868/915).
The strong packet stays unnamed: LaCrosse-TX-like PWM timing
(ON 550/1400, constant ~1 ms gap) but 48-bit sum-checksummed payload with
temp/humidity-plausible fields (8.8 °C / 71 %RH); ruled out F007TH
(Manchester — a 3×half-bit pulse is impossible there, LFSR digest fails),
OSv1 (Manchester + 5780 µs sync absent) and LaCrosse TX framing
(44 bits, starts 0000 1010).
