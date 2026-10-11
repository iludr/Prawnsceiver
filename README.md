# Prawnsceiver — NOAAh's CrabArk

A Raspberry Pi and a cheap RTL-SDR dongle turned into a fully automated
weather-satellite ground station. It predicts passes, tunes the SDR to the
right satellite at the right time, records the downlink, decodes it into
weather images (Meteor-M LRPT, ISS SSTV), and serves everything on a live
web dashboard.

The NOAA POES satellites this station was originally built around (NOAA
15/18/19) were decommissioned Jun-Aug 2025 - the last APT carrier went
silent on NOAA-15 on 2025-08-19 15:26 UTC and DSB with it, so the NOAA
receive/decode paths were removed from the code on 2026-10-11. APT-era
passes, recordings and decoded images remain in the pass history.

Live at: https://prawnceiver.derusi.de

## Features

- 🛰️ Auto pass tracking — skyfield-based pass prediction for the Russian
  Meteor-M 2-3/2-4 weather satellites and the ISS, with automatic
  frequency switching and recording during passes (above 10°)
- 📡 Live FFT waterfall — real-time RF spectrum from raw IQ samples, with a
  frequency-feature overlay (signal band, demod filter, DC spike)
- 🔊 Live audio — software FM demodulation streams the downlink as a
  browser-playable WAV
- 🖼️ LRPT digital decoding — Meteor LRPT (OQPSK) recordings cannot be
  FM-demodulated: the raw IQ is decoded with SatDump (meteor_m2-x_lrpt)
  and the composite image (wedge-calibrated channels, map overlay) lands
  next to the recording, one click from the dashboard
- 🛰️ ISS SSTV — ISS (Zarya) passes on 437.550 MHz are tracked and recorded;
  Robot 36 images from ARISS events are decoded automatically with the sstv
  tool (`RECORD_ISS` in config.py turns ISS recording off outside events)
- 🧭 Polar pass tracker — live az/el ground-track view of the active pass
- 📚 Pass history — every pass and recording is kept in a browsable history
  with decoded images
- 🦀 Crabs caught — every successfully decoded satellite image counts as a crab
- 🚢 AIS ship traffic — the dongle decodes Danube vessels on
  the marine AIS channels (161.975/162.025 MHz, GMSK 9600 baud) in software:
  name, position, speed and course of every ship within VHF range show up
  live on the dashboard. Any dongle can be switched to AIS and back from
  its own dashboard card (`🚢 Listen for AIS ships`) — with a single dongle
  the satellites pause while it listens for ships; a second dongle pinned
  via `AIS_DONGLE` in calibration.py keeps both running at the same time
- 🖥️ Dongles, networked or local — each remote dongle is served by
  an `rtl_tcp` daemon on the machine it is plugged into and added by
  IP address + port; a dongle plugged into the receiver host itself is
  added by USB device index and read directly with `rtl_sdr` (raw IQ on
  stdout, restarted on band switches — it cannot retune live). Both
  kinds are added/removed at runtime from the dashboard header


## Hardware

- Raspberry Pi 4
- RTL-SDR dongle (RTL2832U with R820T tuner)
- Antenna for 137 MHz (VHF 137 MHz SATCOM or a crossed dipole works)
- Optional: any spare RTL-SDR dongle as a dedicated AIS receiver (the
  137 MHz antenna hears 162 MHz ships fine at close range; a ~46 cm
  quarter-wave whip is better)

## Software Stack

- rtl_tcp — one daemon per dongle, running on the machine the dongle is
  plugged into: raw IQ stream + tuning commands over TCP, offset-tuned
  +60 kHz to dodge the center DC spike
- Python 3 + NumPy — FFT waterfall and software FM demodulation
- Skyfield — TLE-based pass prediction (TLEs refreshed from SatNOGS with a
  Celestrak fallback every 3 h, plus a local cache)
- SatDump — LRPT (Meteor-M) downlink decoding
- SQLite (Python stdlib) — station database: passes, dongles, TLEs, decode
  markers, AIS ships/messages; one-click full backup download/restore
  from the dashboard
- sstv — ISS Slow-Scan TV decoding (Robot 36)
- nginx — HTTPS reverse proxy (Let's Encrypt) in front of the Python server
- noaa_receiver/decoding/ais.py — AIS demodulation/decoding (GMSK discriminator,
  HDLC deframing, CRC-16/SDLC, ship database) — pure NumPy, no extra deps

## Architecture

The dongles are decoupled from the receiver: each one is served by an
`rtl_tcp` daemon on the machine it is plugged into (see Setup below), and
the receiver is just a network client. Dongles are identified by their
`host:port` address and can be added and removed at runtime from the
dashboard (persisted across restarts in `dongles.json`).

The receiver itself runs in a single Python process (`server_noaa.py` → the
`noaa_receiver/` package) with three threads:

- `scheduler_thread` — TLE refresh + pass prediction + frequency switching
- `sdr_thread` — one capture thread per dongle: rtl_tcp IQ stream → FFT
  waterfall + FM demodulated audio (retunes in-band while streaming)
- HTTP server — dashboard, JSON API, recordings, decode/delete endpoints

```
rtl_tcp (dongle host) ──TCP IQ──▶ capture thread ──▶ FFT ──▶ live waterfall
                                        │
                                        └─▶ FM demod ──▶ live audio + WAV recording ──▶ SatDump ──▶ weather PNG
```

The scheduler tunes the dongle to the next satellite 60 s before each pass
rise and records until 60 s after set. Between passes it parks on the
Meteor LRPT band (137.9125 MHz).

### Repo layout

The `noaa_receiver/` package is grouped by topic:

```
noaa_receiver/
├── config.py, calibration.py, state.py   # station core: config, dongle calibration, shared state
│              # db.py (SQLite catalog: passes, dongles, TLEs, decode markers,
│              #  AIS ships/messages + full backup/restore)
├── decoding/   # decode.py (dispatch: SatDump meteor_m2-x_lrpt + sstv),
│              # ais.py (AIS demod + ship database)
├── sdr/         # radio.py (rtl_tcp capture + recording), dsp.py (per-block DSP chain),
│              # scan.py (frequency scanner)
├── tracking/    # scheduler.py, passes.py (TLE prediction), plan.py, satnogs.py
└── web/         # app.py (HTTP server), handler.py (endpoints), pages.py (+html/),
               # history.py (pass history + recordings), thumbs.py
```

## Setup

```bash
# Install dependencies
sudo apt install rtl-sdr python3-numpy nginx
pip install skyfield

# SatDump (LRPT decoder)
sudo apt install satdump

# sstv (ISS SSTV decoder)
pip install sstv

# Clone and run
git clone https://github.com/Derusi/Prawnsceiver.git
cd Prawnsceiver
python3 server_noaa.py
```

Then open http://your-pi:8085 in your browser.

### Dongle host: the prawntenna manager

The dongles live on a separate machine (a Raspberry Pi in this station)
running [prawntenna](https://github.com/iludr/Prawntenna), a small web
manager that enumerates the attached RTL-SDRs and publishes each one as
a standard `rtl_tcp` server on the LAN (ports are remembered per dongle
serial; a wedged `rtl_tcp` is killed and republished automatically — see
its API.md for the HTTP control API and the rtl_tcp protocol).

The receiver picks dongles up by address: open the dashboard and use
the **Add** field under "SDR Dongles (rtl_tcp)" (IP + port), or start
with a default list in `calibration.py` (`DEFAULT_DONGLES`).

For unattended operation, start the receiver at boot:

```bash
crontab -e
# add:
@reboot /path/to/noaa_receiver.sh
```

Put nginx with a proxy_pass to 127.0.0.1:8085 in front for HTTPS.

## Configuration

Station parameters live in `noaa_receiver/config.py`: coordinates
(`LAT`, `LON`), timezone offset (`UTC_OFFSET`), pass selection
(`PASS_MIN_ALT`, `PASS_PREDICT_HOURS`), SDR settings (`SDR_RATE`, `SDR_GAIN`,
`SDR_OFFSET_HZ`), log/record directories and the web port.

## Why "Prawnsceiver"?

Because it's a prawn-ceiver — a transceiver with claws. Built with OpenClaw. 🦐
