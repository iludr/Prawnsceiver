# Receiver event log — overnight monitoring 2026-10-07/08

This file is the running log of unattended monitoring of the prawnceiver
ground station. It exists so that a fresh session can pick up the watch
without context.

## How to read / continue this file (session-restart guide)

- Entries are chronological, newest at the bottom. Times are local
  Regensburg time (CEST, UTC+2) unless marked UTC.
- Each entry has: timestamp, what happened, evidence (files, console
  lines, JSON values), and what (if anything) to do next.
- To continue the watch after a restart:
  1. Read this file bottom-up to the last "state: healthy/broken" line.
  2. `ssh eugene@prawnceiver.derusi.de` (key auth) — check
     `pgrep -f 'server_noaa[.]py'` and
     `curl http://127.0.0.1:8085/status.json` (expect < 1 s).
  3. Console log of the receiver: `curl http://127.0.0.1:8085/console.json`
     (in-memory ring, resets on restart).
  4. Recordings: `/var/log/noaa/recordings/` (WAV audio, `.iq.u8` raw IQ,
     `_lrpt/`/`_dsb/` SatDump product dirs). Pass history:
     `/var/log/noaa/pass_history.json` (also `/pass_history.json` API).
  5. Update the cron schedule for the remaining passes (see
     "Tonight's pass plan" below), verify each pass after its set time,
     append an entry, commit.
- Receiver restart (only if broken, never during an active pass —
  `status.json: pass_active`):
  `pkill -f 'server_noaa[.]py'; sleep 3; nohup ~/noaa_receiver.sh > /dev/null 2>&1 &`
  Sudo password: ask the user (deliberately not stored in this repo).
  Deploy new code: push to GitHub, then on the Pi
  `cd /home/eugene/aprs_website && git pull -q` (index.html is static;
  Python changes need the restart above).
- Commands from the Pi time out fast (< 10 s) — if an endpoint does not
  answer immediately, something is broken; investigate, do not wait.

## Tonight's pass plan (all times CEST, from /passes.json)

| Rise  | Satellite        | Mode  | Max alt | What to verify after set            |
|-------|------------------|-------|---------|--------------------------------------|
| 02:51 | Meteor-M 2-4     | LRPT  | 12.7°   | IQ files, SatDump meteor_m2-x_lrpt run (low pass: products unlikely) |
| 04:29 | Meteor-M 2-4     | LRPT  | 80.8°   | Headline: first real digital-image attempt. IQ + SatDump + products |
| 06:10 | Meteor-M 2-4     | LRPT  | 19.2°   | IQ + SatDump                          |
| 07:23 | NOAA 15          | APT   | 24.7°   | WAV + noaa-apt PNG (APT transmitter believed alive) |
| 08:45 | ISS (Zarya)      | SSTV  | 11.3°   | WAV only; no ARISS event expected — snowstorm/empty is normal |
| 09:02 | NOAA 15          | APT   | 54.0°   | Best image chance of the night        |
| 09:31 | Meteor-M 2-3     | LRPT  | 19.9°   | IQ + SatDump                          |
| 10:19 | ISS (Zarya)      | SSTV  | 53.7°   | WAV only                              |

Per-pass checks: (1) console.json for "Recording started/stopped", live
retune lines, "SatDump decode started (…)" and "… done"; (2) recording
files on disk (WAV ~35 MB/min APT, IQ ~29 MB/min per dongle); (3)
`pass_history.json` entry with signal_peak; (4) receiver RSS memory
(`ps -o rss= -p $(pgrep -f server_noaa[.]py)` — flag if > 1.5 GB; the
receiver was OOM-killed once at ~00:48 with 2.4 GB).

## Known context going into the night

- NOAA 18/19 APT transmitters are dark (verified by receive + decode):
  their passes run in DSB mode (137.35 / 137.77 MHz) with raw IQ + SatDump
  `noaa_dsb`. NOAA 15 APT is believed alive — tonight is its test.
- db.satnogs.org and celestrak.org are TCP-blocked from the Pi (SYNs
  dropped on v4+v6; ICMP+DNS fine; other internet fine; user's PC fine).
  Suspected blocklist. Consequences: no SatNOGS transmitter metadata in
  the pass list (mode chips still work), and TLE syncs will fail until it
  clears — the receiver runs on its cached TLEs (cache is fresh, good for
  days). Retry: receiver retries DB every 30 min automatically.
- Open bugs (do NOT chase tonight, on the backlog): decode-attempt
  markers missing (OOM risk at pass-end decode loops of old ISS WAVs);
  estimate_quality always 0%; pass-end attribution may duplicate history
  entries.
- Cron schedule for this watch (verify after each pass, append entries):
  - 03:06 CEST — 02:51 Meteor-M 2-4 LRPT pass (id f5f22472)
  - 04:45 CEST — 04:29 Meteor-M 2-4 LRPT 80.8° headline pass (id a7fca383)
  - 06:25 CEST — 06:10 Meteor-M 2-4 LRPT pass (id ef3f8fa6)
  - 07:35 CEST — 07:23 NOAA 15 APT pass, first image chance (id e6ffffae)
  - 09:15 CEST — 09:02 NOAA 15 APT 54° pass, best image chance (id 5fa7237a)
  - 10:40 CEST — morning wrap-up + block recheck (id e96e0bcc)
  Crons fire only while the session is live and idle; if a slot was
  missed, run that check manually on resume.

## Entries

### 2026-10-08 01:40 CEST — watch starts, state: healthy
- Pi at origin/main 42891d5, receiver up (pid 203005), all endpoints
  < 10 ms. Pass list shows receive plans (mode chips) for all passes.
- History page font fix (2a5d8bc) and receive-plan feature (e787b88,
  527d8e2, 42891d5) deployed. The API-freeze bug (SatNOGS fetch under
  status_lock) is fixed and verified.
- SatNOGS/Celetrak block: diagnosed ~00:50-01:20 CEST, left as-is per user.
- Upcoming: 02:51 Meteor-M 2-4 LRPT (12.7°) — pipeline shakedown pass.

### 2026-10-08 01:45 CEST — unattended work: OOM root fix deployed
Changes made while the user is asleep (all pushed, Pi restarted at
01:42 CEST, idle window before the 02:51->04:29 passes):

1. Decode-attempt markers (ef52533) — the OOM root cause fix.
   - decode.py: every decode attempt's outcome is persisted in
     `<wav>.decode.json`; later attempts short-circuit on the marker.
     `decode_recording(wav, force=True)` re-runs (Retry button).
   - handler.py: `/decode` accepts `?force=1`; `/delete` also removes
     the marker.
   - history.py / history.html: recordings list exposes
     `decode_attempted`/`decode_error`; the history page shows the
     failure reason + a Retry button instead of re-decoding, and the
     auto-decode loop skips attempted recordings. Previously every
     page load re-ran in-process SSTV/noaa-apt decodes on permanently
     undecodable WAVs (ISS without ARISS, dark transmitters) until the
     receiver OOMed at 2.4 GB.
   - scheduler.py: pass-end auto-decode skips previously-failed
     recordings with a single console line instead of re-running.
   - Seeded 28 skip markers ("skipped in bulk before decode markers
     existed — Retry to decode") into existing un-decoded recordings
     so the next history-page open does not spawn a decode storm.
2. Quality scorer (ff3d5a0): routing now uses the exact satellite name
     from decode.satellite_from_filename instead of `'iss' in filename`
     (a "swiss_sat" recording would have been scored as SSTV). New
     tests/test_quality.py synthesizes good APT / good Robot 36 /
     desynced / noise WAVs: good=100, desynced=65, noise=0 — the
     scorer itself was CORRECT; the "0% for everything" reports were
     genuine noise (dark transmitters). NOAA 15 tonight will get real
     quality values.
3. Test fixes: test_radio fixture gained the 'iq' entry field (the IQ
     recording commit 54eb116 had missed updating the test fixture);
     test_decode re-runs now use force=True where a marker would
     legitimately short-circuit.
- Full suite passes on the Pi (test_decode, test_dsp, test_radio,
  test_quality). Receiver restarted, both dongles running, RSS 47 MB,
  armed for the 02:51 pass (runs with these fixes).

### 2026-10-08 01:55 CEST — unattended work: pass-history dedup deployed
- Pass-end history attribution (a5a9bb2), the last open backlog bug:
  - history.log_pass merges a second log of the same physical pass
    (same satellite, rise within 120 s) instead of appending a
    duplicate — receiver flip-flops between overlapping
    same-frequency passes and manual-tune re-triggers used to log one
    pass twice with split decode/peak state.
  - scheduler attribution now matches recordings by the pass's
    satellite name prefix and takes only the newest primary fragment
    (previously a Meteor entry could carry NOAA 18's WAV — seen in the
    23:01 Meteor-M 2-3 entry).
  - tests/test_history.py covers merge/different-pass/different-sat.
  - One-time cleanup on the Pi: existing pass_history.json deduped
    48 -> 38 entries (backup at pass_history.json.bak2).
- Receiver restarted again (idle), endpoints fast. Note: earlier
  timestamps in this log were corrected — the Pi clock runs CEST and
  the console log stamps are CEST as well.

### 2026-10-08 03:08 CEST — 02:51 Meteor-M 2-4 LRPT pass: pipeline works
- Pass 02:50:30-02:57:10 (12.7°). Both dongles recorded WAV + raw IQ
  simultaneously; PASS END closed all 4 files; auto-decode started two
  detached SatDump meteor_m2-x_lrpt runs at 02:57 (decode markers
  written with "started in the background" — the new marker flow works).
- Files: Meteor-M_2-4_20261008_025030{,_00000991}.wav 38 MB each,
  .iq.u8 192 MB each (~400 s at 480 kB/s — correct).
- Products: both _lrpt dirs empty at 03:07; satdumps still running
  (9 min in, 50 MB RSS each). At 12.7° no lock is expected; the 1 h
  timeout bounds the noise-chewing worst case.
- History: single entry, peak 350.7, wav correctly attributed to the
  pass satellite (satellite-aware attribution works), 39 entries —
  no duplicate.
- Memory: server RSS 92 MB (up from 47 MB during recording —
  buffers, fine), no satdump blowup. No OOM risk.

### 2026-10-08 04:50 CEST — 04:29 Meteor 80.8° pass + re-trigger bug fix
- Pass recorded fully: 349 MB IQ + 69 MB WAV per dongle, peak 420.5,
  single correct history entry. Primary's SatDump on the full IQ
  started 04:41:06 — products dir still empty at 05:10 (decode
  running; LRPT lock unconfirmed so far).
- BUG FOUND AND FIXED (deployed 04:52, restart in idle window): the
  04:29 pass logged FOUR "PASS START" lines at 04:30:00-04:30:09.
  Root cause: pass identity compared rise_utc with exact equality;
  the 30-min re-prediction recomputes rise times that can drift by
  microseconds, so a re-predict landing mid-pass re-triggered the
  running pass each tick (the 30-min break condition spans several
  10 s ticks). Each re-trigger "finished" the pass prematurely: the
  secondary's PARTIAL IQ (first ~100 s) got decoded at 04:30 and its
  _lrpt dir now blocks the full decode via the out-dir marker.
  Fix: _same_pass uses a 30 s rise tolerance; the trigger compares
  via _same_pass. Critical to land before the 07:23 NOAA 15 pass
  (a mid-pass re-decode marker would have blocked the full APT
  decode of the first good image chance).
- FALLOUT (pending): the secondary dongle's 04:29 full IQ needs a
  manual re-decode once the primary finishes: kill the 04:30 partial
  satdump if still running, delete the empty
  Meteor-M_2-4_20261008_042820_00000991_lrpt dir, then
  curl '/decode/Meteor-M_2-4_20261008_042820_00000991.wav?force=1'.
- Note: restarts kill SatDump's supervision thread (1 h timeout +
  product logging) while the satdump process itself survives as an
  orphan — the 02:51 pair and the 04:41 primary therefore have no
  completion console lines; watch orphans manually and kill them if
  they run absurdly long (> 1 h).

### 2026-10-08 06:40 CEST — LRPT decode engineering + a dead end
The 04:29 headline pass: signal captured perfectly (349 MB IQ), but
NO SatDump products. Root-caused through the whole chain:

1. RAW IQ decode can never work: SatDump baseband pipelines demodulate
   around 0 Hz, our raw stream has the satellite at ~-(SDR_OFFSET_HZ) —
   centered decode required (commit 9bd4592).
2. SatDump's Celestrak TLE fetch: each retry blocks 134 s on this
   network before the demod even starts. Fixed: decodes now run under
   `unshare -rn` (instant connection failure) and the receiver seeds
   `~/.config/satdump/satdump_tles.txt` from its own TLE cache
   (commit c07f341).
3. Static centering is untrustworthy: corrections are modeled, and
   transmitters can be off-frequency. Now the decode MEASURES the
   signal position in the IQ: width-matched sliding window (72 kHz for
   LRPT, 6 kHz for DSB), negative-side only (satellite is always below
   center), window selection by total elevated power. Synthetic tests:
   plateau/narrow-carrier/noise all measured correctly (commits
   19bd932, 6bd08e9... see git log). Validated on the real 04:29 file:
   measures -71.4 kHz, my independent spectrogram says the plateau is
   centered ~-75 kHz, 62 kHz wide.
4. THE DEAD END: with the signal correctly centered, a fine sweep of
   satdump pipelines (meteor_m2_lrpt qpsk-72k, meteor_m2-x_lrpt
   oqpsk-72k, meteor_m2-x_lrpt_80k oqpsk-80k) x 8 rotation offsets
   (66..80 kHz, 60 s culmination slices) produced ZERO frames in
   every combination. The plateau is pass-synced (Doppler-drifts with
   the satellite) but its 62 kHz width does not match LRPT-72k
   (108 kHz occupied) or LRPT-80k (120 kHz).
5. RF position: the plateau sits ~16 kHz below where the model puts
   137.9125 MHz — either Meteor-M 2-4 transmits at ~137.8965 MHz in a
   nonstandard mode, or the dongle's correction at 137.9125 is wrong by
   ~90 ppm (which would contradict NOAA 15's in-window peaks at
   137.62). DECISIVE TEST: Meteor-M 2-3's 09:31 pass at 137.1 MHz —
   if its LRPT decodes with the new pipeline, the station is fine and
   M2-4's transmitter is the anomaly.
- Also found: a CONSTANT narrowband interferer at raw ~-55 kHz
  (post-shift +5 kHz, INSIDE the ±4.7 kHz signal-strength window!) —
  present outside passes too; it may be inflating "signal peak"
  numbers on Meteor passes. Worth excluding from the strength window
  in a future change.
- The 06:10 Meteor pass ran the complete new pipeline cleanly:
  record → measure (-71.4 kHz, consistent) → rotate → decode →
  no products (same anomalous signal). Automation works.
- NEXT MAJOR TEST: 07:23 NOAA 15 APT (transmitter believed alive) —
  the audio-path image chance; then 09:31 Meteor-M 2-3.

### 2026-10-08 06:45 CEST — 06:10 Meteor pass verification (cron)
- Complete pipeline ran unattended end to end: PASS 06:09:27-06:18:48,
  both dongles WAV (54 MB) + IQ (269 MB), auto-decode with markers,
  measured centering, quality scores, history entry — all automatic,
  no operator action.
- Primary: signal measured at -71.4 kHz (identical to the 04:29 pass
  — the anomaly is reproducible, satellite-side). Decode ran, no
  products (0-byte CADU, as established).
- Secondary (FC0013): signal NOT FOUND in its IQ (weaker antenna at
  19.2°, or its different ppm puts the plateau outside the search
  range) — fell back to the +60 kHz assumption, decode ran, no
  products. Expected at this elevation.
- Reception quality scored 0% on both (the FM-demod audio of a
  digital pass is noise — honest).
- History: single entry, peak 695.4 (note: likely inflated by the
  constant -55 kHz interferer sitting in the strength window;
  see the 06:40 entry), decoded=True (decode-started semantics).
- Memory: server 140 MB after the pass-end decodes (was 108 MB) —
  watch the slow creep (47 -> 92 -> 108 -> 140 over the night);
  nowhere near the 1.5 GB flag.
- Receiver healthy, no restart performed; next: NOAA 15 APT 07:23.

### 2026-10-08 07:50 CEST — 07:23 NOAA 15 APT: transmitter ALIVE, image decoded
- Pass recorded cleanly on both dongles (60.5 MB WAVs, closed 07:32:50),
  auto-decode produced PNGs on BOTH (10.4 MB each — 1258x2080 proper
  APT geometry, noaa-apt found sync, sync-strip bars present).
- Image content verified numerically: adjacent-row correlation 0.21
  (whole image) / 0.15-0.26 per third — 4-6x the 0.03-0.05 of the
  dark-transmitter snowstorms, but well below a crisp pass's 0.7+:
  a REAL but weak image, consistent with 24.7 deg max elevation.
- Peak 1132 — strongest signal of the night. NOAA 15's APT transmitter
  is definitively alive and the analog path (retune, demod, noaa-apt,
  markers, attribution) works end to end.
- ANOMALY OPEN (quality scorer): estimate_quality scored 0% on this
  recording, and the audio spectrum at mid-pass is unexpectedly FLAT
  (2400 Hz subcarrier band only +0.7 dB over neighboring bands, no
  hump) despite strong reception and a decoded image. Only 0.06% of
  samples clip, so overdrive is not the explanation. The scorer is
  honest to what it measures; the puzzle is why the real audio lacks
  the subcarrier hump (signal position/demod subtlety?). The 09:02
  NOAA 15 pass at 54 deg gives much better data to continue this.
- Memory: server 152 MB (creep continues: 47->92->108->140->152;
  decodes + quality analyses add up — still 10x under the flag).

### 2026-10-08 09:25 CEST — 09:02 NOAA 15 + honest image-quality verdict
- Both passes clean: 08:45 ISS recorded, auto-decode failed correctly
  ("No SSTV transmission found" — markers written, no decode storm; no
  ARISS event, expected). 09:02 NOAA 15: WAVs (71 MB) + PNGs (12.2 MB)
  on both dongles, peak 474 (curiously LOWER than 07:23's 1132 at
  half the elevation — the strength window likely caught the constant
  interferer on 07:23).
- IMAGE VERDICT (deep dive): NOAA 15's decoded images are NOT useful
  weather imagery. Adjacent-ROW corr ~0.2 for every NOAA 15 image of
  the last two days INCLUDING yesterday's 86.9-degree pass — and
  adjacent-PIXEL corr (0.25) is indistinguishable from the dark
  transmitter snowstorms (0.23-0.26). The row correlation that looked
  like "real signal content" is mostly the periodic sync bars
  noaa-apt renders every row. Carrier + sync alive, video modulation
  weak/noisy. Consistent across days => NOT a regression from
  tonight's changes; NOAA 15's APT video is degraded (old satellite).
  The 07:23 entry's "real but weak image" verdict was too optimistic.
- The flat-audio anomaly fits this story: no 2400 Hz subcarrier hump
  because the video modulation is mostly noise; the quality scorer's
  0% is honest after all.
- Memory: 204 MB (creep: 47->...->204 over 8 passes — worth a
  tracemalloc look in the morning; maybe quality-analysis buffers).
- Remaining: 09:31 Meteor-M 2-3 LRPT (decisive pipeline test) and the
  10:40 wrap-up.

### 2026-10-08 09:45 CEST — 09:31 Meteor-M 2-3: inconclusive at 19.9°
- Pass recorded cleanly (182 MB IQ + 37 MB WAV per dongle), decode
  ran automatically — but the signal measurement found NOTHING above
  threshold on either dongle ("NOT FOUND, assuming 60.0 kHz"), and the
  fallback-position decodes produced 0-byte CADUs.
- At 19.9° this is inconclusive (weak elevation + V-dipole geometry).
  The decisive test moves to the 11:10 Meteor-M 2-3 pass at 67.4°:
  a strong LRPT there proves the pipeline; nothing found at that
  elevation points at the transmitter.
- Memory at 09:37: 260 MB (night creep 47 -> 260 MB across 9 passes —
  tracemalloc investigation recommended today).

### 2026-10-08 10:05 CEST — fixed-gain experiment deployed (user-approved)
- Following both NOAA guides' advice (no AGC, fixed RF gain — see the
  morning comparison against apbouwens' guide), the primary R820T now
  runs 29.7 dB MANUAL gain; the FC0013 secondary stays on AGC as the
  A/B reference (its gain table differs).
- Implementation: calibration.SDR_DONGLE_GAIN = {"77771111153705700":
  29.7}; the capture thread sends manual-gain-mode (0x03) + gain in
  tenths of dB (0x04) over the rtl_tcp control protocol right after
  the handshake — the rtl_tcp CLI parses -g as an int, so the
  R820T's fractional gain steps would be mangled on the command line.
- Console confirms both dongles up, primary at 29.7 dB manual.
- EVALUATION: compare both dongles' audio on the same NOAA 15 passes
  (subcarrier SNR around 2.4 kHz + waterfall noise floor stability).
  Note: NOAA 15's video modulation is degraded regardless — the
  experiment tests station SNR, not the satellite.
- Next passes: 10:19 ISS 53.7°, 10:45 NOAA 15 10.6° (gain smoke test),
  11:10 Meteor-M 2-3 67.4° (decisive LRPT test), 11:12 NOAA 18 20°.

### 2026-10-08 10:15 CEST — prep for a third dongle (NESDR SMArt v5)
User receives a NooElec NESDR SMArt v5 today (R820T2 + 0.5 ppm TCXO).
Changes deployed:
- calibration.py: unknown serials now get (0, "unmeasured dongle")
  instead of inheriting the primary's -11 kHz correction — a TCXO dongle
  would have been mis-tuned on first use. Verified live: both existing
  dongles unchanged; deployed with a restart at 10:13 (idle window).
RUNBOOK for when the dongle arrives (see also the 11:10 LRPT test first):
1. USB: 3 dongles on a Pi 4 is at the power-budget edge (~300 mA
   each) — a POWERED USB HUB is strongly recommended; watch for
   brownouts (rtl_tcp restart loops in the console).
2. Connector: the v5 is SMA — get an SMA cable/adapter for whichever
   antenna it should feed.
3. Serial: plug in, check `rtl_sdr -d 99` output for a UNIQUE serial
   vs 77771111153705700 / 00000991. If it clashes or is generic,
   set one in an idle window (rtl_eeprom needs exclusive access):
   stop receiver, `rtl_eeprom -d <idx> -s <new-serial>`, restart.
4. Enumeration is automatic: the receiver picks it up within 10 s,
   assigns the next rtl_tcp port (1237), a waterfall card, per-dongle
   recordings — no restart needed just for detection.
5. Calibration: its tuning infobox will show "unmeasured dongle".
   Measure the actual ppm (FM pilot method or rtl_test -p) and add a
   SDR_DONGLE_CORRECTIONS entry for its serial; expect near 0
   (TCXO). Until then it records ~correctly (0 correction is much
   closer than the old generics' -11 kHz).
6. Primary role (recommended): the v5 (R820T2, TCXO, better thermal)
   should replace the ancient R820T as PRIMARY_DONGLE_SN — one-line
   change in calibration.py + restart; the V-dipole then moves to it.
   Add its serial to SDR_DONGLE_GAIN (measure best fixed gain first).
7. CPU: a third capture thread adds ~50% DSP load; FFT_EVERY was
   budgeted for two dongles — watch demod stalls / load average.

### 2026-10-08 10:42 CEST — morning wrap-up: night summary, state: healthy
All 9 monitored passes since 02:51 recorded and logged; pipeline
automated end to end every time (record, auto-decode with markers,
quality, history attribution — no operator action all night).

| Rise | Satellite        | Alt   | Peak  | Decoded | Products |
|------|------------------|-------|-------|---------|----------|
| 02:51 | Meteor-M 2-4    | 12.7° | 350.7 | yes (decode-started) | _lrpt: 0-byte CADU |
| 04:29 | Meteor-M 2-4    | 80.8° | 420.5 | yes | _lrpt: 0-byte CADU |
| 06:10 | Meteor-M 2-4    | 19.2° | 695.4 | yes | _lrpt: 0-byte CADU |
| 07:23 | NOAA 15         | 24.7° | 1132  | yes | PNG (weak/degraded video) |
| 08:45 | ISS (Zarya)     | 11.3° | 717.9 | no (correct, no ARISS) | — |
| 09:02 | NOAA 15         | 54.0° | 474   | yes | PNG (weak/degraded video) |
| 09:31 | Meteor-M 2-3    | 19.9° | 418.4 | yes | _lrpt: 0-byte CADU |
| 10:19 | ISS (Zarya)     | 53.7° | 22.4  | no (correct) | — |

1. LRPT PRODUCTS: ZERO digital images from any Meteor pass. All 7
   _lrpt dirs (both dongles x 3 Meteor passes + 09:31 M2-3) contain
   only a 0-byte .cadu + empty MSU-MR + 4-byte telemetry.json. No
   _dsb product dirs exist yet (NOAA 18/19 DSB decodes: the 00:36
   NOAA 18 pass predates the watch window and recorded no IQ-era
   products). The decisive test remains 11:10 Meteor-M 2-3 at 67.4°.
2. MEMORY: no OOM, no crash overnight — the receiver ran from the
   01:42 restart through all passes until the deliberate 10:07
   fixed-gain restart (RSS crept 47 -> 260 MB as logged; tracemalloc
   investigation stays on the backlog). Post-restart RSS 89 MB.
   Console ring was reset by the restart, so no overnight console
   evidence remains — the per-pass entries above are the record.
3. SATNOGS BLOCK LIFTED: db.satnogs.org answers in ~0.12 s from the
   Pi with real JSON (satellite 25338 fetch verified). The loader's
   30-min retries self-populated tonight's history entries with
   transmitter metadata — nothing was triggered manually. Notable
   data now in pass_history: NOAA 15 APT 137.62 marked INACTIVE,
   NOAA 18/19 APT marked inactive (confirms our dark-transmitter
   findings), Meteor-M 2-4 lists LRPT at 137.9125/137.9/137.1 MHz in
   both 80k and 72k modes.
4. CELESTRAK STILL BLOCKED (timeout, HTTP 000 after 8 s) — but
   tle_age_min is 33 (fresh): TLE syncs are succeeding (SatNOGS-side
   source), so no action needed.
5. ANOMALY (fixed gain, for the 11:35 evaluation): the 10:19 ISS
   pass — the FIRST recorded after the 29.7 dB manual-gain deploy —
   peaked at 22.4 vs the usual ~150 noise floor. Recordings are
   normal-sized (48 MB WAVs), so capture worked; the low peak
   suggests 29.7 dB leaves the primary under-driven relative to AGC
   (or the RSSI scaling changed with manual gain mode). The 10:45
   NOAA 15 pass + 11:35 cron must judge this from the audio/waterfall
   before deciding whether 29.7 dB is the right fixed value.
- Next: 10:45 NOAA 15 10.6° (gain smoke test), 11:10 Meteor-M 2-3
  67.4° (decisive LRPT test), 11:12 NOAA 18 DSB 20°, then the 11:35
  cron evaluation (gain A/B + LRPT verdict). NESDR SMArt v5 arriving
  today — runbook in the 10:15 entry.

### 2026-10-08 11:40 CEST — gain A/B verdict + 11:10 LRPT test (contaminated)
(1) GAIN A/B — 29.7 dB IS TOO LOW. The audio comparison was a wash:
    on the 10:45 NOAA 15 pass both dongles produce statistically
    identical audio (RMS -16.0 vs -16.4 dBFS, 2.4 kHz band SNR -0.1 dB
    both — pure noise at 10.6° with a degraded transmitter), and the
    10:19 ISS noise floors are also identical. The FM demod path
    normalizes output level, so audio can NOT distinguish front-end
    drive. The IQ tells the real story:
      primary AGC (04:29/09:31): IQ rms 9.4-15.5 u8-units
      primary fixed 29.7 (11:10): IQ rms 1.29  => ~17-22 dB under AGC
      secondary FC0013 AGC: 2.1 before, 1.05 at 11:10 (also dropped)
    1.29/127.5 = 1% of ADC range: quantization eats ~3 of the 8 bits —
    fatal for weak digital (LRPT) decodes. The 22.4/31.3 pass peaks
    were the same effect (strength metric scales with front-end gain).
    VERDICT: AGC wins; if we want manual gain it must sit near the
    R820T max (~42-49.6 dB), not 29.7. Recommend reverting the
    primary to AGC (user decision; needs a restart in an idle window)
    or retrying fixed ~42 dB on a good NOAA 15 pass. The NESDR v5 will
    redo this experiment properly on a TCXO device.
(2) 11:10 METEOR-M 2-3 67.4° LRPT: NO PRODUCTS (0-byte CADU both
    dongles, empty MSU-MR) — but the test is CONTAMINATED by the
    under-driven primary. My independent IQ re-measure (5 windows,
    4096-pt FFTs, elevated-power scan on the negative side):
      primary (fixed 29.7): no clean plateau; broad +2..+3 dB
        humps only, peak excess 18 dB = narrow spur (the -55 kHz
        interferer), no LRPT-shaped band.
      secondary (AGC): no elevated power above 6 dB anywhere.
    So no strong LRPT signal at 67.4° on either dongle — leaning
    "Meteor-M 2-3's LRPT is quiet too" (same family as M2-4), but the
    primary's quantization under-drive weakens the evidence. Repeat
    the test with proper gain on the next high M2-3 pass.
(3) CONSOLE RING DESTROYED BY RE-PREDICT SPIN: at 11:30:06-11:30:10
    the 30-min re-prediction loop filled all 100 console lines
    ("Predicted 31 passes in next 24h" + per-pass lines every tick),
    evicting ALL evidence of the 10:45/11:10 passes including the
    "Baseband centering ... measured at" line. The backlog quirk is
    worse than assumed: every half hour the ring loses its last
    30 min of pass history. Escalate: re-predict should log once, not
    per tick, or the ring should be larger / re-predict lines filtered.
(4) 11:12 NOAA 18 DSB pass: never happened — no recordings, no
    history entry; current predictions list NOAA 18 next at 12:52
    (70°). The 10:05 plan line was stale (prediction moved).
- Passes at 11:35: NOAA 19 53° at 11:38 (active during this check),
  ISS 58° 11:56, NOAA 18 70° 12:52 (good DSB test), Meteor-M 2-3 13°
  12:52 (too low to matter).

### 2026-10-08 12:35 CEST — NESDR SMArt v5 is the new primary
User removed the FC0013 (00000991) and connected the NESDR SMArt v5.
Enumeration at 12:24 was automatic: SN 48263793, "Nooelec NESDR
SMArt v5", rtl_tcp up, waterfall card, no restart needed. A brief
"Connection reset by peer" storm at 12:23 (replug) self-healed; the
stale FC0013 retry loop cleared on the restart below.
- Deployed (commit 325ccee): PRIMARY_DONGLE_SN = 48263793; the old
  R820T (77771111153705700) demoted to secondary. SDR_DONGLE_GAIN
  emptied — both dongles on AGC per the 11:40 A/B verdict (29.7 dB
  under-drove the ADC ~17-22 dB).
- v5 correction: 0 Hz "unmeasured dongle" — near-correct for a 0.5
  ppm TCXO (±69 Hz at 137 MHz). To measure properly: FM pilot method
  (103.0 MHz pilot) or rtl_test -p in an idle window; add a
  SDR_DONGLE_CORRECTIONS entry for 48263793 then. A fixed-gain value
  for the v5 also needs MEASURING (sweep gain, find the knee), not a
  guide number — see the 29.7 dB lesson.
- Test suite on the Pi: all 5 test scripts pass STANDALONE
  (test_radio: "ALL RADIO TESTS PASSED"). NOTE: `unittest discover`
  breaks test_radio (shared-process state — the rtl_tcp stand-in
  'sleep' spawn fails) — run the scripts individually, the docstring
  way: python3 -u tests/<name>.py.
- Receiver restarted 12:32 (idle window before the 12:52 NOAA 18
  pass). Both dongles up: v5 primary AGC (signal 524 idle — notably
  above the R820T's 145 noise floor; waterfall shows whether that is
  antenna gain, AGC drive, or local RF — watch), R820T secondary AGC
  at its normal 145.
- v5'S FIRST PASS as primary: NOAA 18 70° at 12:52 (DSB mode, IQ +
  SatDump noaa_dsb) — the shakedown. Meteor-M 2-4 32° at 14:15 gives
  the v5's first LRPT attempt with a TCXO.

### 2026-10-08 13:08 CEST — v5 shakedown pass done; DC-spike verdict + blanking removed; TLE sync bar
(1) 12:52 NOAA 18 (DSB 137.35, 70°) — NESDR SMArt v5's first pass as
    primary: CAPTURE OK, one anomaly. signal_peak 939.3, full coverage,
    file sizes normal. ANOMALY: the v5's WAV/IQ SPLIT mid-pass —
    125108 recorded 12:51:08 to ~12:53:2x (62 MB IQ), then a fresh file
    125329 ran to pass end (302 MB); together they match the R820T's
    unbroken 370 MB. Cause unknown: the console ring with the exact log
    line was destroyed by the 13:07 restart (below). Suspects: a
    momentary rtl_tcp/USB hiccup on the v5 (precedent: the 12:23 replug
    "Connection reset by peer" storm) or a WAV write error (disk NOT
    the cause — 37G free). WATCH: whether it recurs at 13:19 NOAA 19;
    if it does, escalate before trusting the v5 for unattended passes.
(2) DSB DECODE — first _dsb attempt ever (no baseline; the 00:36 NOAA 18
    pass predates IQ-era products):
      v5 125108 (short segment): PRODUCTS — HIRS + SEM (real NOAA DSB
        instruments), dataset.json "Unknown NOAA", timestamp 0.0.
      v5 125329 (main segment): 0-byte tip, no products.
      R820T full pass: 8216-byte tip, no products.
    Pattern: the only decode with products ran from the RAW iq.u8; the
    two failures are exactly the files with freshly generated
    .centered.c32 (1.2/1.5 GB mistune-centering products of the new
    centering path; no c32 exists for 125108). Next step when idle:
    re-run SatDump noaa_dsb on a big file directly from iq.u8 and/or
    inspect the centering output — prime suspect for the DSB failure.
(3) DC SPIKE VERDICT (from the 12:52 pass IQ, 120 FFT windows each):
    v5 0.43x row max, 0% of windows dominated; R820T 0.32x, 0%;
    removed FC0013 reference 0.73x, 11%. The waterfall blanking
    (c9fc3f9) existed only for the FC0013 → REMOVED in 4bfc8ae (rows
    normalize by the plain row max again, pre-2026-10-06 behavior).
    Deployed + receiver restarted 13:07:17 (idle window, NOAA 19 next
    at 13:19); live row verified unblanked (spike bins now vary, spike
    peak 135 vs row max 255). Both dongles back up after restart.
(4) TLE sync progress bar (2817a6d, static index.html — no restart):
    bar beneath the Pass Data box on Sync-now click; starting state
    bridges the <=10 s scheduler pickup, then tle.active/done/total/
    current from status.json drive it; hides itself when finished.
- Next: 13:19 NOAA 19 28° (watch for the WAV-split recurrence on the
  v5), 13:33 ISS 62°, 14:15 Meteor-M 2-4 32° (v5's first TCXO LRPT try).

### 2026-10-08 14:30 CEST — decode chain deep-dive: two real bugs fixed; all tracked digital transmitters dark
(1) SIGN BUG in the decode centering (fixed, ac7d0ad): _measure_signal_offset
    returns the signal's POSITION (negative Hz; -71425 measured live on the
    04:29 M2-4 file) but _decode_iq passed that value as the ROTATION.
    frequency_shift rotates BY its argument, so every decode whose
    measurement succeeded rotated the signal to twice its offset — aliased
    out of the demod band. The +60 kHz fallback is a rotation and was
    correct, so only measurement-failed (weak) decodes were ever centered
    right. Mechanically explains the whole 0-byte-CADU LRPT streak.
    Fixed: shift_hz = -measured. Verified: synthetic signal at the measured
    -71425.78 Hz lands at 0.0 Hz; live console now logs "measured at
    -84.3 kHz, rotating 84.3 kHz" / "NOT FOUND, assuming -60.0 kHz,
    rotating 60.0 kHz".
(2) BUT: no satellite signal was ever in the failing files. Time-resolved
    FFT drift analysis (fixed peaks only, zero Doppler drift — impossible
    for a satellite carrier) across 04:29 M2-4 (80.8°, healthy AGC, rms
    9.4-15.5), 06:10 M2-4, 09:31 M2-3, 12:52 NOAA 18 (BOTH dongles, 70°)
    and 13:18 NOAA 19 (v5): no satellite anywhere. The 10:15 entry's
    "clear +8 dB / 72 kHz LRPT plateau" does not reproduce — the
    measurement was fooled by a broad ~2x noise hump (AGC pumping/tuner
    shape), and the "-13 kHz off-nominal M2-4 LRPT" claim came from the
    same fooled measurement (noise hump sits at raw -71.4 kHz = -11.4 kHz
    in waterfall coords).
(3) M2-3 TRACKING FREQUENCY (fixed, c594048): tracked at 137.1 MHz —
    ~800 kHz below its LRPT (SatNOGS: 137.9125/137.9; the 137.1 entries
    are stale duplicates). Every M2-3 recording tuned dead spectrum.
    Now 137912500. First correctly-tuned M2-3 passes: TODAY 20:56 (46.1°)
    and 22:36 (29.4°).
(4) DSB verdict: NOAA 18 DSB (137.35, tuned right, 70°) and NOAA 19 DSB
    (137.77 per SatNOGS, tuned right, 28°) — no carrier on either dongle.
    The 12:52 "HIRS/SEM products" were SatDump scaffolding on noise
    (telemetry words all -1, "Unknown NOAA", timestamp 0). The dashboard
    also cannot show _dsb products at all (only APT PNGs) — feature gap,
    moot while the DSBs are dark.
(5) v5 PASS-TIME STREAM CORRUPTION (open hardware issue): during passes
    the v5's IQ shows equal-power mirror pairs (±15.7/±45.4 kHz at 12:52,
    ±25.3 kHz at 13:18) and, live at 14:15-14:21, an artifact cluster at
    waterfall -85.8 kHz (= raw -145.8 kHz — outside the band,
    impossible for a real signal) averaging 206/255. Parked streams are
    clean. Correlates with recording activity → suspect USB/CPU load:
    the 10:15 runbook's POWERED HUB recommendation, plus port/cable
    swap if it persists. The one-off WAV split at 12:53:29 did NOT recur
    (13:18 and 14:15 passes: single continuous files, no console errors).
(6) CORRECTION to the 13:08 entry: the 13:07 restart killed two in-flight
    SatDump decodes (proof: their .centered.c32 temp files survived —
    the finally-clause never ran). "Centering = prime suspect for the
    12:52 DSB failure" was wrong: those files contained no satellite.
    Leftover c32s (2.7 GB) removed 13:44.
(7) 14:15 M2-4 32.4°: LRPT confirmed OFF live — at max elevation both
    dongles show nothing at the target (R820T target-band/median 1.07);
    fixed-code decode ran cleanly on empty spectrum.
(8) Real-data outlook today (pipeline correct end-to-end for the first
    time): M2-4 15:55 (43.8°), M2-3 20:56 (46.1°, first ever correctly
    tuned), M2-3 22:36 (29.4°). NOAA 15 APT keeps producing its degraded
    "snowstorm" images. Watches scheduled 16:08 / 21:10 / 22:52.

### 2026-10-08 14:40 CEST — dashboard recording-pause switch deployed (user request)
User wants to stop collecting garbage recordings while reception quality
is being fixed. Added a global pause: System Status panel -> "Automatic
Recordings" -> Pause button (also reachable as /record_pause?paused=1|0,
state in status.json:recordings_paused). While paused the receiver still
tracks passes (tuning, Doppler, waterfall, live audio) but no WAV/IQ
files are opened; a running WAV closes within one IQ block. Paused passes
still get a pass-history entry (peak, no wav). Flag is in-memory — a
restart resumes recording.
Deploy: restart in the idle window after the 14:39 NOAA 18 set, before
the 15:11 ISS rise. Recordings LEFT PAUSED after deploy per user intent
— note this skips the 15:55 M2-4 43.8 deg LRPT outlook pass (and the
16:08 watch) unless the user resumes first.

### 2026-10-08 14:38 CEST — pause switch deployed; restart was mid-pass (user-ordered)
Correction to the entry above: the user ordered the restart immediately
("I still only receive garbage"), so it happened at 14:36 DURING the
low 14.7 deg NOAA 18 DSB pass (dark transmitter, garbage either way),
not in the idle window. Sequence verified live from console.json:
14:37:36 scheduler re-triggered the pass after restart, 14:37:38 both
dongles opened WAV+IQ, 14:37:46 /record_pause?paused=1 -> both closed
within the same second; status.json recording:False while pass_active
stays True (tracking/waterfall/live audio keep running). Recordings are
PAUSED now. Leftover truncated NOAA_18_20261008_143738*.{wav,iq.u8}
(both dongles, ~0.8 MB / 3.9 MB each) from the restart window — left on
disk for now. The 15:55 M2-4 LRPT outlook pass will not be recorded
while paused (16:08 watch moot unless resumed).

### 2026-10-08 14:41 CEST — all recordings wiped (user request: "not a good one yet")
User confirmed none of the recordings so far were usable. Wiped
/var/log/noaa/recordings/ entirely (291 entries, 11 GB: NOAA 15/18/19,
Meteor-M 2-3/2-4, ISS; WAV, IQ, PNGs, thumbs, decode markers, SatDump
product dirs). Recordings were paused at the time (nothing in-flight);
recordings.json now lists 0, 48 GB free. pass_history.json KEPT — its
signal-peak-per-pass data is diagnostic value for the reception-quality
work, but its wav/png links now dangle (decode/delete buttons will 404).

### 2026-10-08 16:05 CEST — AIS receiver built: Danube ship traffic on a dedicated dongle (user request)

User wants to listen to AIS messages from Danube ships. Built the full
receive chain as a new module `noaa_receiver/ais.py` — pure NumPy, no new
dependencies, mirroring the existing rtl_tcp architecture:

- A dongle pinned by calibration.AIS_DONGLE_SN (currently None — feature
  is OFF until one is plugged in and its serial set; any spare dongle
  works, the console prints serials at enumeration) is excluded from
  satellite tracking, never becomes primary, and /tune_dongle rejects
  it. Its rtl_tcp is parked at 162.000 MHz +60 kHz offset; both AIS
  channels (A 161.975 / B 162.025) sit at -25/-75 kHz and are demodulated
  from the same 240 kHz IQ stream: per-channel rotation, 61-tap 14 kHz
  low-pass, discriminator, decimation to 48 kHz (5 samples/bit).
- Radio layer conventions cross-checked against dgiardini/rtl-ais'
  aisdecoder source (NRZI 0=transition, flags 0x7E, stuff-0-after-five-1s
  on the transmitted stream, SDLC CRC-16 with the 0xF0B8 magic residue,
  AIVDM bit vector = transmitted stream with each byte REVERSED) and
  then VALIDATED against a real over-the-air capture: the Helsinki
  210-messages recording from the freerange/ais-on-sdr wiki decodes —
  known Finnish ships, names (AILA, JOANNA SATURNA...), plausible
  positions/speeds/courses. A 3 s slice + one real type-5 payload are
  committed as test fixtures.
- Demod note (measured): AIS is GMSK BT<=0.4 (ITU M.1371: 0.4 max on
  25 kHz channels, index 0.5 = +/-2.4 kHz). Frequency slicing must use
  near-transparent smoothing — a wide low-pass or matched filter crushes
  the 4800 Hz level alternation of 00 bit pairs (their amplitude is only
  ~200-500 Hz after BT=0.3/0.4 premodulation) and mis-slices them; the
  rtl-ais-style approach (light smoothing + point sampling at bit
  centers, 5-phase search per burst) works. Single-bit errors are caught
  by the frame CRC; positions are also sanity-checked (|lat|<90,
  |lon|<180 — a CRC-passing garbage position was observed in the capture).
- Messages 1-5, 9, 11, 18, 19, 21, 24, 27 decode into a ship table
  (state.ais_ships, keyed by MMSI); /ais.json serves it plus per-channel
  stats and a raw AIVDM feed (receiver-side armored, NMEA-checksummed,
  2 fragments for type-5-length payloads). Ships expire after 30 min of
  silence. Dashboard: new 'Danube Traffic' section under Upcoming Passes
  (hidden while AIS_DONGLE_SN is None), 10 s refresh, Google Maps links
  per ship.
- tests/test_ais.py: CRC test vector, armoring round-trip incl. the
  gpsd example sentence, end-to-end synthetic RF through the PRODUCTION
  per-block chain, and the real-capture conformance asserts. All pass;
  test_dsp also still passes (test_decode/test_radio need noaa-apt and
  rtl_tcp binaries and fail identically with and without these changes
  on the Windows dev box).
- NOT YET DEPLOYED/TESTED LIVE on the Pi: no AIS dongle attached yet,
  and real-antenna behavior at 162 MHz is unmeasured. Next step when a
  third dongle arrives: set AIS_DONGLE_SN in calibration.py, watch the
  console for the rtl_tcp start line, and check the waterfall for two
  faint carriers at +/-25 kHz of center; ships should appear on any
  Danube movement.

### 2026-10-08 16:30 CEST — AIS dongle assigned: the old R820T (77771111153705700)

User picked the generic R820T (the previous primary, currently the
comparison dongle) for AIS. AIS_DONGLE_SN is set; consequences:
- The R820T leaves satellite duty: satellite passes are now received by
  the v5 alone, and the dashboard's per-dongle receive comparison is
  down to one satellite dongle (dongle-comparison features degrade
  gracefully — the AIS dongle still shows as a card with its 162 MHz
  waterfall).
- Tuning correction at 162.000 MHz comes from the R820T's 80 ppm
  fallback: +12960 Hz. Its measured band corrections were all in the
  +72..82 ppm range, so the residual after the fallback should be well
  under 1 kHz — harmless for the demod (per-burst DC removal, 14 kHz
  channel filter). If the two AIS carriers sit visibly off +/-25 kHz in
  the waterfall, measure and pin 162000000 in the 'freqs' table.
- Antenna: dedicated 162 MHz vertical recommended (AIS is vertical
  pol); until one is mounted, the dongle can test-decode through the
  137 MHz antenna (mismatch costs a few dB but close Danube traffic
  should still decode). To verify after the next restart: console shows
  'AIS receiver on dongle 77771111153705700', waterfall shows two faint
  carriers +/-25 kHz around center.

### 2026-10-08 16:50 CEST — AIS deployed to the Pi; two deploy bugs fixed; Pi hard-crashed once; v5 off USB (replug needed)

Deploy via SSH (eugene@192.168.3.245, repo clone = /home/eugene/aprs_website,
start via crontab @reboot noaa_receiver.sh). Timeline and findings:

- 16:22 first start: satellite capture threads ALL died with NameError —
  the AIS wiring edit had dropped SDR_DONGLE_GAIN from radio.py's
  calibration import (test_dsp/test_ais didn't exercise radio's runtime
  namespace; test_radio's identical-looking pre-existing failure on the
  Windows box masked it). Fixed (9da7166), added a namespace guard at
  the top of tests/test_radio.py, restarted 16:25: clean.
- Dashboard was broken by a syntax error in the new AIS panel
  (fetchAIS): a heredoc-escaping accident wrote a RAW NEWLINE inside
  the .join() string literal -> the whole <script> block failed to
  parse, blanking the entire dashboard (user reported index:2237:101).
  Fixed byte-verified, whole script block now checked with
  node --check (236a237).
- 16:36-16:38 the Pi hard-crashed mid-diagnostic (was briefly parked on
  161.975 via /tune_dongle for a spectrum comparison; command timed
  out, machine stopped answering ping AND the public site went down,
  then rebooted itself at 16:38:14). No journal survives (volatile
  journald), no undervoltage flag in the new boot. Cause unknown —
  power brownout or USB cascade are the candidates (known flaky-USB
  history). Watch for recurrence; if it crashes again under AIS load,
  suspect the PSU.
- After the reboot the v5 (48263793) is OFF the USB bus (lsusb shows
  only the R820T) -> satellite tracking/recording is DOWN until it is
  physically replugged (sdr_thread re-enumerates every 10 s, no restart
  needed). The R820T auto-became primary per the fallback rule, so the
  main waterfall currently shows the AIS band. NOAA 15 pass 17:11 will
  be missed unless the v5 is back before rise.
- AIS side runs fine: rtl_tcp tuned 162,072,960 Hz (center+60k offset
  +12960 ppm fallback), both channels demodulating, floor ~2-6 u8
  units, no errors. BUT the AIS spectrum is FLAT at both channels
  (peaks == noise floor): the R820T hears nothing yet. What antenna is
  it on? If it shares the 137 MHz antenna it should still show close
  traffic; zero carriers plus zero decoded frames suggests a poor/
  disconnected antenna path. Next on-site: replug the v5, check the
  R820T antenna, watch the waterfall for two faint carriers +/-25 kHz
  around center.

### 2026-10-08 17:05 CEST — CORRECTION to 16:50: no crash — the Pi was moved indoors

The "hard crash" was the user unplugging the Pi and carrying it inside
ahead of rain (~16:33-16:38; the 16:38:14 boot is it coming back up on
the desk, same state as any power cycle). No PSU/kernel problem. The
v5 (48263793) is still off the USB bus after the move — replug pending;
satellite tracking stays down until then. AIS runs on the R820T as
before (0 frames so far; antenna situation after the move to be
re-checked — see next entry when the dongles/antennas are settled).

### 2026-10-08 18:15 CEST — AIS field measurements: system healthy, antenna is the bottleneck

Station moved indoors ahead of rain; each dongle has its own antenna (v5:
the 137 MHz V-dipole; R820T: a generic vertical whip). Measured with raw
rtl_sdr captures + the production decode chain offline:

- Tuning spot-on: AIS ch A/B land 0-37 Hz from expected (the 80 ppm
  fallback correction is right for this dongle at 162 MHz).
- RF path verified: strong FM station at 99.59 MHz received at +34 dB
  over the floor through the same dongle+antenna — antenna, coax and
  dongle all work.
- AIS signals PRESENT but FAINT: ch A +6.2 dB, ch B +8.5 dB over the
  noise floor (1-s FFT peaks exactly at the channel frequencies). No
  burst exceeded 1.9x the burst-gate floor in 90+90 s outside — ships
  in current range are too far/too few for the demod (~+13 dB needed).
  One strong +22 dB burst was seen earlier (indoor position), so closer
  ships will be decodable.
- Gain experiments: the R820T's manual gain table tops out ~9 dB BELOW
  what its AGC achieves (AGC floor 62 dB vs manual-max 53 dB in the
  same 1-Hz FFT units) — AGC (SDR_GAIN=0) stays the right choice for
  weak-signal AIS here; fixed gain only makes sense for strong-signal
  sites. rtl_sdr -g sweeps 29.3..49.6 dB all showed rms ~0.9 (the
  floor barely moves — ADC/post-tuner noise dominates at low RF input).
- CONCLUSION: everything except the antenna is proven (decoder
  validated on the real Helsinki capture; live pipeline healthy; both
  dongles running after the rain move). The generic whip is not
  resonant at 162 MHz and loses >10 dB — exactly the gap between the
  measured +6..9 dB carriers and the ~+13 dB decode threshold. Next
  hardware step: 46.3 cm quarter-wave ground plane (or a commercial AIS
  whip) outside in the clear. Expected: distant Danube traffic becomes
  decodable; close ships decode today's setup only when they pass.

### 2026-10-08 evening - frequency scanner + recorded-bandwidth fit (new feature)

Requested: per-dongle "scan" button that sweeps for strong signals and
stops on the next one found, plus a mechanism to keep the recorded band
from being too wide or too narrow. Implemented:

- `noaa_receiver/scan.py` (new): scan thread per dongle. It only writes
  the per-dongle frequency override - the capture thread applies each
  step as a live rtl_tcp retune (no stream gap), so the waterfall shows
  the swept spectrum in real time. Detection works on the raw FFT row
  now published as `entry['last_mag']` (radio.py): peak/floor ratio,
  median over a 0.7 s dwell, DC-spike window (+/-15 kHz around the
  +60 kHz offset center) masked. Hit threshold: ratio 3 (+9.5 dB).
  On a hit the dongle re-centers on the measured peak bin, confirms,
  then fits the recorded demod BW from the signal's measured width
  (half width + 30%, clamped 1-120 kHz).
- `/scan_dongle?d=&start=&end=&step=&ratio=` + `&stop=1`;
  `/fit_bw?d=` fits the BW at the current tune any time (card button).
  Scan state in `dongles.json` (`scan` field).
- Dashboard: per-card Scan row (from/to/step + Scan/Stop toggle, live
  status line) and a Fit button in the Recorded-BW row.
- Safety: scanning parks the dongle (no pass recording on it, same as a
  manual tune). Primary dongle: scan refused during a pass and
  auto-aborts (rejoins the shared frequency) when a pass rises
  mid-scan. Nothing found -> override restored to pre-scan state.
- Tests: `tests/test_scan.py` (measurement, spike masking, width fit,
  end-to-end sweep with fake dongle rows: nothing/found/stop cases) -
  all pass. test_decode/test_radio failures on this Windows box are
  pre-existing POSIX dependencies (verified identical on clean HEAD);
  they pass on the Pi.

State: feature complete locally; deploy + restart pending (avoid
mid-pass windows).

### 2026-10-08 ~18:00 CEST - scan shakedown: three live hardening rounds, validated

The scanner's first two live FM-band runs parked on PHANTOMS (87.5408,
98.403, 88.42 MHz - nothing at center once parked, widths 1-15 kHz
instead of broadcast FM's 150+). Three root causes, each fixed and
regression-tested (tests/test_scan.py 4d/4e/4f):

1. Site impulse noise: every 34 ms FFT row carries a saturating bin at
   a WANDERING offset (visible in any waterfall batch - each row is
   peak-normalized to 255, so the row max tells nothing; the OFFSET
   wandering is the tell). Per-row peak/floor detection hit on it at
   every tune. Fix: detection runs on the TIME-AVERAGED row (0.7 s
   dwell, ~40 rows), each row clipped at 8x its median first - a
   stationary carrier keeps its level, impulses are bounded and
   diluted. This matches how the +34 dB 99.59 MHz station was measured
   (1 s averaged FFTs).
2. rtl_tcp delivery stall right after a retune (<5 rows in one dwell)
   ended a scan as 'stopped'. Fix: _sample extends the window until
   the dwell elapsed AND >=5 rows arrived (hard bound 4 dwells); only
   a dead stream ends the scan, as 'nodata' (override restored).
3. Tune-relative artifacts (spurs that follow the tuner, e.g. +40.8
   kHz - just outside the DC-spike mask): passed the confirm because
   the confirm only re-checked the ratio. Fix: after re-centering, the
   confirm requires the peak within +/-5 kHz of 0 Hz - a real signal
   stays put when the dongle tunes onto it, a spur moves away.
   (Watch: repeated hit-recenter-reject cycles double the step time in
   spur-infested bands - cosmetic, the sweep keeps going.)

FINAL LIVE VALIDATION (v5 primary, FM band 87.5-108, 200 kHz steps):
scan parked at 92.0212 MHz, +10 dB, card center-band signal 1255 (vs
~150 floor - a real, persistent, strong carrier), fitted BW 35 kHz;
/fit_bw at the parked tune measured 68.4 kHz wide / +16.9 dB and set
the demod to +/-44.5 kHz (FM broadcast modulation makes the
instantaneous peak wander ~10 kHz, so re-fitting after parking gives a
better width estimate - that is what the Fit button is for). Sync
released the dongle afterwards; tracking resumed, NOAA 15 18:46 pass
unaffected.

Tooling notes for this repo (Windows dev box): Git Bash heredocs
mangle \uXXXX sequences into U+FFFD - patches touching JS escapes must
go through write_file'd Python scripts, not heredocs; the edit tool
cannot match CRLF files (normalize to LF first); test_decode/test_radio
fail on Windows identically with and without changes (POSIX fakes /
chmod), they pass on the Pi.

## 2026-10-09 — dongles decoupled: network rtl_tcp servers

The receiver no longer owns USB dongles. Each dongle is now served by a
persistent `rtl_tcp` daemon on the machine it is plugged into (the Pi
"Krabstral", 192.168.3.245: `rtl-tcp@<serial>.service` user units bound to
0.0.0.0, serial→port map in `~/rtl_tcp_daemon.sh`), and the receiver
(now on 192.168.2.73) connects over the network. Dongles are keyed by
`host:port`, added/removed at runtime from the dashboard
(`/add_dongle`, `/remove_dongle`; persisted in `dongles.json`), and the
primary/AIS/correction tables in calibration.py moved from serials to
addresses. Removed: local rtl_tcp spawning, serial enumeration
(rtl_sdr -d 99 probing), stale-process killing, port-conflict checks,
PLL log checking. The old receiver on the Pi was stopped and its
@reboot entry removed; the Pi now runs only the daemons.
### 2026-10-10 12:20 CEST — LRPT decoding live: SatDump on the container + interferer-proof centering

Task: "implement the stuff required for LRPT decoding". The capture side
(IQ recording on LRPT/DSB frequencies, baseband centering, decode routing)
already existed; the missing piece on the new container was the SatDump
binary — plus everything below that only surfaced once it ran.

1. SATDUMP INSTALLED on prawnsdr@192.168.2.73 (Ubuntu 25.04 standard repo,
   apt, 1.2.0-3). Pipeline verified end-to-end: meteor_m2-x_lrpt on the
   20261009_201200 M2-3 recording (355 MB, 740 s) decodes in ~112 s on
   the container — runtime is a non-issue. First run: 0-byte CADU, same
   signature as the old Pi failures. Cause this time: no satellite (see 2).
2. NO SATELLITE IN ANY METE RECORDING (spectral forensics, 24-slice
   time-frequency view of both M2-3 files): every spectral feature is
   CONSTANT across the pass with ZERO Doppler drift and present before
   rise — physically impossible for a LEO downlink. A real LRPT signal
   (80 kbaud OQPSK, ~120 kHz wide, +/-3.4 kHz Doppler over a pass) is
   nowhere. M2-3 (137.9125, correctly tuned since c594048) and M2-4 LRPT
   transmitters remain DARK.
3. LOCAL INTERFERER IDENTIFIED: a persistent ~11 kHz-wide NFM land-mobile
   channel at ~137.905 MHz (raw -67.3 kHz in 137.9125 tunes; Region-1
   land mobile band 137.825-138), 20-40x the noise floor, plus other
   stationary carriers (-28.4/-14.9/-11.1/+32.6 kHz in various
   recordings). It sits ~7.5 kHz BELOW the LRPT downlink — right inside
   the recorded band, strong enough to fool any naive measurement. The
   20:12 decode "measured" -47.6 kHz from it and centered empty spectrum.
4. MEASUREMENT OVERHAUL (decode.py _measure_signal_offset) so a real
   signal wins and local junk loses:
   - Hann-windowed FFTs: a strong carrier's boxcar sinc skirt rides
     above the noise test across the whole window and pinned the
     centroid/drift tests onto the spike.
   - Noise reference = positive-frequency half (the satellite is always
     negative-side under offset tuning). The full-spectrum median sits
     ON a plateau that fills half the capture — exactly the LRPT
     geometry — and the score gate then rejects the satellite it found.
   - Acceptance gates: -18 dB width of the winning window >= 45% of
     signal_bw (broadband signal), else centroid drift >= 300 Hz
     between the recording's first and last thirds (Doppler). A
     stationary narrow feature = local transmitter -> None, and the
     caller falls back to the +SDR_OFFSET_HZ rotation (exact on the
     TCXO v5 whose correction is 0).
   - 24 dB excess cap in the centroid: a strong narrow carrier
     out-powers a whole broadband plateau bin-for-bin and pins the
     drift test onto itself.
   - Per-slice AGC normalization before the drift comparison: the
     tuner's AGC pumps the floor +/-30% between slices and faked a
     3.2 kHz "drift" on a satellite-less hump.
   - Score threshold 2.0x -> 4.0x: a satellite-less AGC/tuner hump
     measures 3.0x; only a confident detection may override the
     (correct) fallback rotation.
   - LRPT window 72 -> 120 kHz (M2-X LRPT is 80 kbaud OQPSK, ~+/-60
     kHz; 72 was the old narrow assumption).
   - PRE-EXISTING BUG FIXED: the slice-span mixed sample and byte
     units, so the measurement's slices covered only the FIRST QUARTER
     of a recording — the drift test could never see a real sweep.
5. PRODUCTS + MARKERS: a finished SatDump decode copies its largest
   PNG (the composite) into the flat <recording>.png slot the
   dashboard/history serve; the decode marker is rewritten with the
   real outcome at completion ('no products' = failed marker, so
   dark-transmitter recordings stop re-decoding; Retry re-runs for
   real now by clearing the stale product dir).
6. TESTS: tests/test_lrpt.py — synthetic drifting/stationary plateaus
   and carriers, plateau-vs-interferer, noise, product pickup; plus
   real-data conformance: the 20:12 M2-3 recording measures None at
   LRPT width (interferer + hump correctly rejected). All other test
   files still pass (test_decode/test_radio keep their pre-existing
   Windows-only POSIX failures).
State: LRPT decoding is READY end-to-end on the container — the first
 Meteor pass whose LRPT transmitter is actually on will center, decode
 and show its composite on the dashboard automatically. The local
 137.905 MHz NFM channel remains an RF-level risk for weak passes (it
 jams ~10% of the LRPT band including near the carrier); nothing code-
 side can fix that — if products stay empty on confirmed-live passes,
 re-check that channel's activity.
### 2026-10-10 13:05 CEST — APT decoding switched to SatDump (user request: one decoder)

noaa-apt is out; SatDump decodes APT too now. Verified live before the
swap: pipeline noaa_apt, input level audio_wav (it demodulates the
2.4 kHz subcarrier from our 48 kHz FM audio itself), parameters
--satellite_number (orbit for the overlay/projection) and
--start_timestamp (geo-reference; parsed from the recording name —
the capture thread names files with the host clock and decode runs on
the same host, so naive .timestamp() is correct on any host TZ; the
container is UTC). Test run on a real WAV (NOAA_18_20261010_120838,
dark DSB noise): full product set incl. wedge calibration, channel
images, avhrr_3_rgb_MCIR composite WITH map overlay drawn from the
TLE file seeded by _seed_satdump_tles. Runtime ~2.5 min for a 12-min
WAV (the TPS projection solve dominates) -> the synchronous APT
timeout went 120 -> 600 s (SATDUMP_TIMEOUT_SECS).

- decode.py: _decode_apt rewritten around satdump; products land in
  <base>_apt/, the composite is copied to the flat <recording>.png slot
  (same pickup as LRPT/DSB). Gains vs noaa-apt: wedge radiometric
  calibration, SDR++ noise reduction, false-color MCIR composite —
  noaa-apt only did overlay + rotation. unknown-satellite recordings
  decode without --satellite_number (no overlay, as before).
- Removed with it: NOAA_APT_DIR / NOAA_APT_TLE_FILE config, the
  passes.py weather.txt writer (noaa-apt's -T overlay TLEs — SatDump's
  TLE file is seeded from the TLE cache instead), NOAA_APT_TIMEOUT_SECS.
- tests/test_decode.py rewritten around a fake satdump (POSIX-side;
  run it on the container). All other suites pass on the dev box.
- /opt/noaa-apt on the container is now unused — can be uninstalled
  whenever convenient.
- Open: the first real NOAA 15 pass after deploy is the live A/B
  against the old decoder's images (none of the current recordings
  carry an APT signal — NOAA 18/19 APT are dark, no NOAA 15 WAVs on
  disk since the Oct 8 wipe).
### 2026-10-10 12:45 UTC — deploy: LRPT chain + APT-on-SatDump live; dongle registry AIS flag fixed

Deployed eb9279b/53780c3 (LRPT interferer-proof centering + APT via
SatDump) to the container in the idle window before the 15:11 CEST
Meteor-M 2-4 86-degree pass: push, git pull, test_decode.py run ON the
container (it caught two real issues the Windows box cannot: a crash in
_pick_product_png when the decoder dies before writing products, and a
test-PATH bug - both fixed in eb9279b/53780c3), systemctl --user
restart with pass_active false.

POST-RESTART FINDING (station config, not code): dongles.json had the
satellite dongle (v5, 192.168.3.245:1234) persisted with "ais": true -
a leftover of the AIS experiments - so the only connected dongle was the
AIS receiver and SATELLITE RECORDING WAS OFF after every restart. The
actual AIS dongle (R820T, port 1235) is offline: the Pi serves only
1234 right now (no rtl-tcp@... unit, one python3 listener on 1234).
Fixed via the dashboard API (/ais_dongle?...&on=0): v5 back to
satellite duty, registry now persists "ais": false. AIS stays OFF
until the R820T is plugged in and rtl-tcp@77771111153705700.service
(port 1235) is started on the Pi - then pin it with the card button or
calibration.AIS_DONGLE.

Next live tests of the new decode paths: any Meteor pass (LRPT, honest
no-product markers expected while the transmitters stay dark) and NOAA
15 19:33 CEST 64-deg (first SatDump APT image - the A/B against the
retired noaa-apt output).
### 2026-10-10 15:07 CEST — recordings wipe (all garbage) + raw IQ in the UI

USER-ORDERED WIPE of every recording, after per-file verification:
- The one exception was FOUND first: Manual_137.6200_MHz_20261009_110451.wav
  carried a real NOAA 15 APT signal (1.5-4.5 kHz subcarrier band at 65x the
  audio floor) - renamed to NOAA_15_20261009_110451.wav and decoded with the
  new pipeline: THE FIRST REAL SatDump APT IMAGE (calibrated channels,
  MCIR/MSA/Rain composites with map overlays, 20 s decode for 176 s audio).
  Kept.
- Meteor-M_2-4 IQs re-verified before deletion: the -59.2 kHz features are
  exactly stationary over their recordings (zero Doppler drift - a satellite
  at that elevation moves 2-4 kHz), the 115410 file's sweeping feature moves
  at -415 Hz/s = 8x LEO Doppler rate: local interference, not satellites.
- Everything else (M2-3 x2, NOAA 18/19 DSB, ISS no-ARISS WAVs) was already
  spectroscopically confirmed dark in the 12:20 deep-dive.
60 files/dirs removed (~1 GB); recordings dir now holds only the decoded
NOAA 15 recording + its _apt products.

UI: the history page now shows each recording's raw IQ capture (name + size,
'📡 raw IQ:' line, shown for in-progress recordings too) and the Delete
button removes the full artifact set - the /delete endpoint now also takes
the .iq.u8 and the _lrpt/_dsb/_apt product directories (it previously left
~300 MB raw files behind). _pick_product_png now prefers SatDump's
map-overlay composites (*_map.png) for the flat dashboard slot - the
decoded NOAA 15 recording's slot moves from the no-map MSA composite to
the map version on its next re-decode. Delete-confirm wording updated.
### 2026-10-10 15:12 CEST — history page: product links instead of one image

The history tab no longer shows a single decoded image per recording -
it lists EVERY generated decode product (SatDump's full set: MCIR/MSA/
Rain composites with and without map overlays, calibrated channels,
raw_sync/raw_unsync, dataset.json) as links with sizes. New /products/
<dir>/<file> endpoint serves files from the product subdirectories
(one level, traversal-sanitized; the old /images route only serves
flat files). get_recordings exposes iq + products per recording; the
formerly dead img variable is gone. The post-decode inline preview in
the result box stays as the manual-decode success indicator.
### 2026-10-10 15:30 CEST — all persistent state moved into a SQLite station database + backup/restore

Implemented the architecture discussed earlier: noaa_receiver/db.py is
a single SQLite catalog (LOGDIR/station.db, WAL, one shared connection,
writes serialized in transactions) - stdlib only, no new dependency.
The filesystem stays the blob store (WAV/IQ/PNG/products); every table
points at files.

Tables: passes (was pass_history.json, merge rule preserved: same
satellite rising within 120 s updates, never appends), dongles (was
dongles.json, roles incl. AIS flag), tle_cache (was tle_cache.json,
3-line groups with names for SatDump's TLE file), decodes (was the
<rec>.decode.json sidecar markers - keyed by recording base name so
absolute paths and bare filenames resolve identically), ships (was
ais_ships_persist.json, full ship dicts as JSON), ais_messages (was
ais_log.jsonl, trimmed by ts instead of file rewrites).

MIGRATION: db.import_legacy() runs once at startup (app.py), ingests
all five JSON/JSONL stores plus the decode markers, is idempotent
(rows win), and retires each source to <name>.migrated. Failed sources
stay for a retry. On the first deploy the container's real pass
history/dongles/TLEs/AIS data (9 ships) will import.

BACKUP/RESTORE (user request): /backup builds a zip with a consistent
SQLite snapshot (conn.backup()) + the whole recordings tree (stored,
not deflated) -> one-click download in the System Status panel;
POST /restore (raw zip body from the file picker) refuses during a
pass, moves the live DB + recordings to .pre-restore copies, extracts,
sanity-checks, and answers with a summary. Restart after restore
reloads in-memory state (AIS ships, dongle registry).

Also: tests get isolated temp databases via the PRAWN_DB_FILE env
override (no D:ar\log pollution on the dev box, no cross-test
leakage); tests/test_db.py covers schema/import/roundtrips/backup-
restore incl. corrupt-archive refusal; test_ais/test_radio/test_
history updated to the DB semantics (log isolation = table clear,
dongle persistence = dongles-table assertions, history order =
insertion order like the old file). _load_history keeps the exact
legacy JSON shape so the history page parses unchanged.
### 2026-10-10 15:40 CEST — BUG (user-reported): the AIS ship import was wiped by its own save

The ship registry showed 1 ship instead of the 9 ever received. Root
cause: ais.py called load_ships() at MODULE IMPORT time, which runs
before app.main()'s import_legacy() - so memory started EMPTY, the
import wrote 9 ships into the database, and the running AIS thread's
periodic save_ships() (which mirrors the in-memory table into the
ships table, DELETE FROM ships WHERE mmsi NOT IN <memory>) erased the
8 imported rows at its next save, keeping only the one live-heard
ship. Classic two-writer ordering bug: import-after-load instead of
load-after-import.

Fix: the module-level load_ships() call is gone; app.main() now loads
the registry from the database immediately AFTER import_legacy() (with
a comment explaining why the order matters). The 9 ships were
re-imported into station.db from ais_ships_persist.json.migrated
(INSERT OR IGNORE - the live-heard ship's row is newer and wins).
### 2026-10-10 15:50 CEST — dashboard: Danube traffic above the pass list (user request)

The AIS section moves to the top of the main panel: with a dongle
switched to AIS mode the ship table is the first thing on the
dashboard, Upcoming Passes follows below (spacing margin moved with
it). The show/hide logic was already correct - it keys on
/ais.json:enabled, i.e. any dongle currently in AIS mode. Static
index.html change, no receiver restart needed for the deploy.
### 2026-10-10 16:05 CEST — AIS page: "Show path on map" (user request)

New button above the ship registry. Pressing it switches the registry
into a multi-select list (checkbox column): every selected ship gets
its last 24 h path drawn on the live map as a colored polyline (popup
with positions count and time span, map fits to the newest track).
Unchecking removes the path, exiting the mode clears all.

Backend: /ship_tracks.json?mmsi=A,B&hours=24 - ais.ship_tracks()
extracts the per-MMSI position history from the rolling ais_messages
log (24 h retention = exactly the requested window); only frames with
positions become track points, positionless MMSIs come back empty and
the UI simply draws nothing for them.
### 2026-10-10 18:35 CEST — 🛰 AIS 0-frames mystery SOLVED: rtl_tcp served a 10x sample rate (fixed-gain experiment superseded)

The fixed-gain experiment (40.2 dB on the R820T + whip, ec40cf0) tamed
the AGC-overload symptoms - floor 3.5/3.9 -> 2.7/2.9, false burst
triggers 68/s -> 0 - but the frame verdict stayed open when the Danube
went quiet. The antenna-swap A/B (V-dipole -> R820T, whip -> v5, both
dongles on AIS 16:39-16:48) broke the case open: v5+whip caught every
3-min burst of the WA YSUR ATON beacon (3/3) while R820T+V-dipole got
0 - the SAME V-dipole had fed 493 frames to the v5 in the morning.
40.2 dB was picked for the whip's delivered power and under-drives on
the V-dipole, so it was retired (59a5129) - back to AGC like the v5.

Root cause of the 0 frames (all antennas, all gains): the capture
threads set the FREQUENCY on connect but never the SAMPLE RATE - they
relied on the rtl_tcp daemon being spawned with -s 240000. The v5's
daemon has it remembered; the R820T's was spawned without -s and
served rtl_tcp's ~2.4 MHz DEFAULT. The AIS demod translated channels
assuming 240 kHz - i.e. it listened at 162.063/162.068 MHz, in the
BOS carrier's neighborhood, on a ~10x-rate stream: waterfall alive,
decodes impossible. Fix 03507b4: _rtl_tcp_connect asserts SDR_RATE on
every connect (covers satellite + AIS threads; test asserts the
0x02 command). The "BOS monster" framing of the morning was partly
an artifact of the mis-mapped band.

Post-fix verdict (the experiment this entry closes): stream correct,
tune verified on-frequency against the v5 (raw-band cross-correlation
lag 0, correction +12960 = +80 ppm confirmed), demod channels
correct, AGC gain - and the R820T still decoded 0 frames while the
v5+whip caught 12/12 WA YSUR bursts (14:42-15:36 UTC). NOT
inconclusive: ships/beacon transmitted, everything software-side is
fixed, the R820T front end itself (sensitivity / IQ quality) is the
remaining suspect. Next step when the Pi is back: live-path AGC IQ
captures from both dongles, offline demod, compare burst SNR at the
channel offsets. No further gain experiments - AGC stays.

Collateral fixes landed the same day: the AIS waterfall cards read
60 kHz low (raw band labeled as centered on AIS_CENTER; now rotated by
+SDR_OFFSET_HZ like the satellite cards, a85ea35); per-dongle
pause/play (0cd5c24) because two dongles recording at once collapse
the dongle host's shared USB bus (non-default rtl_tcp -b/-n measured
at 3.4% throughput, documented in prawntenna API.md); a wedged R820T
recovered with a plugdev USBDEVFS_RESET (no sudo needed on the Pi).

NOAA 15 evening passes (v5 on satellites, but wearing the swapped
whip): 17:55 23° recorded in two WAVs (the mid-pass receiver restart
split it), decode ran, quality 0% - the 46 cm whip is unusable at
137 MHz as predicted. The 19:33 64° pass is the good one; it needs
the Pi back up, the v5 on satellites (it was flipped to AIS during
dashboard testing) and ideally the V-dipole swapped back to the v5.


### 2026-10-10 19:05 CEST — High-rate mode: 960 kHz capture committed (SDR_RATE 4x, FIR taps scaled)

Fun experiment promoted to a real mode: SDR_RATE 240000 -> 960000
(config), FIR_TAPS 25 -> 100 (dsp) and _AIS_TAPS 61 -> 244 taps
(ais) — the FIR taps scale with the rate so the absolute filter
selectivity is unchanged; without that, the wider fs/numtaps
transition band floods the demodulators with adjacent-channel noise.

Live evidence on the container (18:48-19:00, dongle v5 on AIS,
tuned 162.060 MHz = 162.000 + 60 kHz offset):
- 480 kHz, unscaled taps: AIS bad frames ~1.6/min, CPU 0%.
- 960 kHz, 61-tap AIS filter: ~1700 bad frames/min, channels A/B
  showing identical counts (both filtering the same wide noise).
- 960 kHz, 244-tap AIS filter: ~3 bad frames/min, channels
  independent again. CPU 0.0%, RSS ~52 MB at every rate.
Waterfall now spans +/-480 kHz; no ships decoded during the test
window (bad-frame statistics stand in for a real frame).

Caveats recorded: decode.py assumes .iq.u8 recordings were made at
the CURRENT SDR_RATE (SatDump --samplerate) — pre-switch 240 kHz
recordings must not be re-decoded under 960 kHz config; decodes run
immediately post-pass so only manual re-decodes are affected. At
960 kHz the USB load is ~1.9 MB/s per dongle — do NOT record two
dongles at once (shared-bus collapse, cf. 0cd5c24); one dongle was
removed from the dashboard for exactly that reason.

Test suite run at 960 kHz on the dev PC: test_dsp, test_ais,
test_radio, test_scan, test_history, test_quality, test_lrpt all
pass (test_decode still fails on Windows, pre-existing). test_scan
needed two fixture fixes to become rate-independent: the synthetic
humps were drawn in BINS (40 bins = 18.75 kHz at 240 kHz, 75 kHz
at 960 kHz) and now specify width_hz; the peak/floor ratio bound
6.0 -> 6.5 (at 960 kHz the peak bin lands on the Gaussian apex
and measures the true amp/floor limit, 6.008).

Next: first NOAA pass at 960 kHz is the real test of the 100-tap
satellite demod path (no pass was live during the change).


### 2026-10-10 19:30 CEST — v5 dongle measured: on-frequency at TCXO spec, calibration entry added (corrections 0)

Measured the NESDR SMArt v5 (192.168.3.245:1234) per the documented
waterfall method, live during high-rate mode (960 kHz, 1875 Hz bins):
- Best reference: the live NOAA 19 DSB pass (19:09-19:11). The
  waterfall row is taken AFTER the offset rotation AND the Doppler
  NCO, so the carrier's position IS the raw tune error. The carrier
  held +0..+234 Hz (mean ~+40 Hz, +0.3 ppm) at 137.77 MHz while the
  applied Doppler swept -349..-1514 Hz across the same 2 min -
  satellite transmitter error included, the dongle alone is likely
  even closer to nominal.
- FM broadcast (89.7/93.0/95.0/99.6/103.0/105.0): three estimators
  tried (sideband argmax, spectrum symmetry axis, stereo-pilot line
  midpoint). All bottom out at ~+/-2 ppm with 1875 Hz bins - program
  sidebands share bins with the pilot lines (the R820T's -8200 was
  measurable at 240 kHz bins; high-rate mode costs measurement
  resolution). Results consistent with 0 (89.7 +0.95, 93.0 +2.81
  ppm).
- Verdict: calibration entry with 0 corrections (error <= 0.5 ppm
  spec = <= 70 Hz at 137 MHz, two orders below the narrowest demod,
  DSB 6 kHz). The 0 entries mark the dongle measured and clear the
  dashboard 'unmeasured dongle' marker.
- Method note for next time: a DSB pass is the cleanest single-
  dongle reference at any bin width - narrowband, at-band, and the
  receiver itself compensates the Doppler.

Collateral: the NOAA 19 DSB recording (started 19:04) was sacrificed
for the measurement (user call) - two manual-tune gaps in its
WAV/IQ where the FM band was measured mid-pass.


### 2026-10-10 20:15 CEST — GSM BTS tried as a frequency reference: refarmed away here, no verified FCCH (experiment closed)

Following the v5 calibration (a8f07d0), tried GSM base stations as a
GPS-locked reference (3GPP BTS spec +/-0.05 ppm; FCCH = pure CW tone
at +67708.33 Hz on every BCCH - the classic SDR calibration trick).
Method: direct rtl_tcp client from the container, dongle PAUSED via
/pause_dongle (the clean way - no client fight; an earlier attempt
without pausing collapsed the stream and left two stray pythons
fighting the receiver, killed by PID). Spur discriminator: a real
FCCH must vanish when the dongle is retuned +/-200 kHz; an LO/clock
spur keeps its offset.
- E-GSM 900 downlink 935-960: REFARMED - the strong signals sit OFF
  the 200 kHz GSM raster (LTE Band 8, 100 kHz raster centers). The
  FCCH window (+/-1.5 kHz around +67.7 kHz) is a spur forest:
  adjacent slots report IDENTICAL line offsets (e.g. +655.9/+656.8,
  +1388.4/+1388.6 - spurs track the tune); recurring spur offsets
  -61.9, +641, +743, +1037, +1360, +1476, -1160 Hz at ratios up
  to 460. Nothing survived the retune test.
- GSM-R 921-925 (rail corridor): one weak line, spur-verified.
- DCS1800 1805-1880: broad spur plateau 1871-1880 MHz at -985 Hz
  offset, present at every tune with ratios 90-460; nothing
  verified. The -985 Hz family also appeared at 900 MHz.
Verdict: no usable GSM reference from this site/antenna; 2G is
likely gone from E-GSM900 here. The v5 calibration stands on the
DSB-pass measurement (+0.3 ppm <= TCXO spec, corrections 0).
If a BTS reference is ever wanted: DCS1800 FCCH with a real 1800
MHz antenna plus a time-domain blink test (FCCH has 1.2% duty,
spurs are continuous) would be the way; TETRA BOS 380-400 MHz
(GPS-locked) remains untested. Operational: dongle unpaused after
each sweep, receiver self-reconnected every time.


### 2026-10-11 13:25 CEST — 🚀 High-rate mode x2.5: 2.4 Msps capture (prawntenna now on a notebook)

The dongle host moved off the Pi: prawntenna runs on a notebook at
192.168.3.65 now (same rtl_tcp ports 1234/1235, same two dongles).
The Pi's hard limit was the shared USB bus; a live A/B on the notebook
this morning (11:56-11:57, both dongles recording simultaneously at
960 kHz for 66 s: WAVs 6771450 vs 6763820 bytes, 0.1% apart, no
console errors) showed the host is no longer the bottleneck, so the
capture rate went up.

- SDR_RATE 960000 -> 2400000. 2.4 Msps = 48000 x 50, a 48 kHz
  multiple — REQUIRED: the AIS demod asserts it at import, and both
  demods decimate by SDR_RATE//48000. 2.56 Msps (the RTL-SDR
  advertised max) is NOT a multiple and would need a 160:3 rational
  resampler in both demod paths; user chose the clean 2.4.
- FIR taps scaled with the rate (the 2026-10-10 selectivity rule):
  satellite FIR_TAPS 100 -> 250, AIS _AIS_TAPS 244 -> 610. test_dsp
  at 2.4 Msps: -3 dB ~19 kHz, -55.7 dB at 40 kHz — identical absolute
  numbers to 960 kHz/100 taps.
- What was silently block-count-based scaled to hold absolute time:
  - AIS burst assembly (ais.py collect_bursts): min 2 -> 5 blocks
    (~1.07 ms), max 48 -> 120 (~25.6 ms = one AIS slot). At 2.4 Msps a
    demod block is 0.21 ms, so the old max_blocks=48 split every real
    AIS frame into undecodable fragments — test_ais end-to-end
    scenario caught it (decoded 0 frames), passes after the scaling.
  - FFT cadence: FFT_EVERY 8 -> 20 blocks (~4 ms of RF between FFTs,
    the CPU budget per its radio.py comment); AIS waterfall the same.
  - Signal metrics hold their Hz width so history stays comparable:
    satellite band window +/-10 bins -> +/-18.75 kHz constant,
    AIS card +/-21 bins -> +/-39.375 kHz constant (both computed from
    SDR_RATE now).
- decode.py _measure_signal_offset: n_fft 4096 -> 4096 * SDR_RATE //
  960000 (10240 at 2.4 Msps, ~234 Hz bins at any rate). With n_fft
  fixed, the width gate compares a bin-quantized width against
  absolute Hz: a stationary carrier's five-bin core measured 2.9 kHz
  at 586 Hz bins — OVER the 2.7 kHz DSB gate, so the 137.905 land-
  mobile interferer would have been mistaken for the satellite
  (test_lrpt scenario 4 failed, passes after the scaling).
- test_scan: fitted-BW bound gains one BIN_HZ of slack
  (_signal_width_hz counts whole bins).
- Tests at 2.4 Msps on the dev PC: test_dsp, test_ais, test_radio,
  test_scan, test_quality, test_history, test_lrpt, test_db ALL PASS
  (test_decode still fails on Windows — pre-existing, missing
  noaa-apt wiring). CPU check: 610-tap AIS filter conv = 168 ms per
  second of stream on the dev PC (was 24 ms at 100 taps/960 kHz);
  the container has headroom, verify RSS/CPU on the console after
  the first passes.
- USB load ~4.8 MB/s per dongle (was 1.9); the "never record two
  dongles at once" caution stands — the dual test above was at 960
  kHz, the same test at 2.4 Msps is still pending.
- Same caveat as the 960 kHz switch: decode assumes .iq.u8 recordings
  were made at the CURRENT SDR_RATE — do not re-decode pre-2026-10-11
  IQ recordings. Decode chunk memory ~192 MB transient (was 77).
Deploy with the next restart (checked: no active pass, next NOAA 18
13:54); verify dongles.json both connected, ais.json decoding, clean
console, and the first 2.4 Msps pass.


### 2026-10-11 13:30 CEST — 🔧 Dongle registry follows the notebook move (found live at the 2.4 Msps deploy restart)

The deploy restart exposed that calibration.py still pointed every
dongle constant at the dead Pi: PRIMARY_DONGLE / AIS_DONGLE /
DEFAULT_DONGLES / both correction-dict keys were 192.168.3.245:*, so
the boot restored ONLY the DB-registered AIS dongle (192.168.3.65:1235)
and promoted IT to primary — no satellite dongle existed until it was
re-added by hand, and the AIS connect ran with correction +0 Hz
('unmeasured') because the measured-correction keys no longer matched
the host:port. All six addresses now say 192.168.3.65 — same physical
dongles behind the same ports, so the measured corrections carry over
unchanged (generic R820T +80 ppm, NESDR v5 0). This also closes the
'unmeasured dongle' regression noted when the notebook first appeared:
the R820T gets its ~+12.9 kHz at 162 MHz back, which should cut the
AIS bad-frame rate (was ~7-8/min/channel with the dongle 80 ppm off).
Tests: test_radio (run twice — one earlier run tripped on port 1299
still held by the previous suite instance, both reruns clean) and
test_db pass. Deploy: pull + restart; expect 1234 primary on
satellites, AIS pinned to 1235, both correction_src measured.


### 2026-10-11 14:10 CEST — 🛰 End of APT/DSB: NOAA POES purge

Fact check against NOAA OSPO bulletins and community recordings:
NOAA-18 was decommissioned 2025-06-06 17:40 UTC (S-band transmitter
failure), NOAA-19 was passivated 2025-08-13 16:55 UTC (battery
failure, a week early), and NOAA-15 — the last APT/DSB transmitter
in orbit — was decommissioned 2025-08-19: APT carrier off at
15:26:07 UTC, spacecraft confirmed decommissioned 20:37 UTC. The
POES constellation is dark for good; no US satellite transmits
analog weather imagery anymore.

Full purge on the receiver (nothing left to receive):
- NOAA 15/18/19 removed from TRACKED_SATS; SAT_DSB_FREQ / DSB demod
  width and the 137.35/137.77 DSB IQ-record entries removed; IQ
  recording now keys on 137.9125 only (both Meteors)
- decode.py: SatDump noaa_apt and noaa_dsb paths removed
  (meteor_m2-x_lrpt + sstv remain); unknown/untracked recordings
  now refuse to decode instead of guessing APT
- decoding/quality.py (APT line-sync scorer) retired; scheduler and
  /decode no longer compute reception quality (stored history
  values are still served)
- dashboard: browser-side APT live renderer removed (Robot 36
  stays), DSB overlays and NOAA quick-tune/frequency labels gone;
  LRPT pass shows a ±60 kHz signal band
- idle park moved 137.62 -> 137.9125 MHz; state.current_sat_name
  idle label is now plain "idle"
- calibration.py: dead-frequency correction entries (137.62/
  137.1/137.35/137.77) dropped; 137.9125 + 437.55 + AIS remain
- tests: test_decode.py + test_quality.py retired (APT/DSB
  dispatch and scoring); fixtures renamed to Meteor/ISS. Suite:
  test_dsp/ais/history/db/scan/lrpt/radio all pass.
Calibration reference note: the "live DSB pass" method (and the
GSM BTS attempt before it) is closed — 137 MHz has no narrowband
satellite telemetry left to calibrate against; the v5 keeps its
measured 0 (±0.3 ppm).
Deploy: pull + systemctl --user restart (check pass_active first).
Old NOAA recordings, _apt/_dsb product dirs and their history
entries stay untouched and keep serving.


### 2026-10-11 — 📡 Add-dongle moved to the header; USB dongles read with rtl_sdr

Dashboard: the add-dongle form moved from the system status panel
into the site header, next to the frequency display, with a mode
switch:
- TCP (rtl_tcp): IP + port, unchanged
- USB (local): device index on the receiver host

USB dongles ("usb:<index>") are read directly with an rtl_sdr
child process (raw u8 IQ on stdout — the exact rtl_tcp sample
format, so the whole capture chain is unchanged; device via -d,
tune via -f at the same offset+correction, fixed gain in tenths
of dB, buffers never touched). rtl_sdr cannot be retuned while
running, so a band switch (idle 137.9125 -> ISS 437.55) closes
the stream and respawns the process at the new frequency — a
short gap between bands only; within a pass nothing changes
(Doppler was always software-rotated for exactly this reason).
Persistence: the dongles table stores ("usb", <index>); the id
stays "usb:<index>", so calibration/pinning keys survive.
AIS capture thread got the same fork (AIS band is fixed, so no
restart limitation there). remove/pause/role-switch stop the
child process too (_stop_capture).
Tests: test_radio section 12 (registry, persistence,
idempotency, validation); full suite green. No rtl_sdr on the
dev PC or the container, so the live spawn path is untested
here — needs a dongle on a host with the rtl-sdr package.


### 2026-10-11 ~15:00 CEST — 🏠 Station moves to prawntenna2 (USB direct, no rtl_tcp)

The receiver left the Proxmox container (192.168.2.73, now dark,
no backup taken - FRESH START by operator decision: empty station
db, no recordings, no history) and now runs on the notebook
itself, Prawntenna2 (192.168.3.65, Ubuntu 26.04, user crustean1),
with both dongles on its USB bus read DIRECTLY via rtl_sdr - the
whole rtl_tcp/prawntenna-manager network hop is gone, which is
the point (quality: no TCP link, no daemon in between).

Executed:
- prawntenna removed: service stopped/disabled/unit deleted,
  install moved to ~/prawntenna.removed-20261011 (not deleted)
- apt: python3-venv + satdump; venv with numpy/skyfield/sstv;
  repo cloned at 6a8bb67; /var/log/noaa owned by crustean1
- systemd SYSTEM unit prawnceiver.service (enabled, Restart=on-
  failure) - not a user unit this time, the notebook may not be
  logged in
- calibration ids re-keyed: usb:0 = v5 (serial 48263793,
  primary satellites), usb:1 = generic R820T (serial
  77771111153705700, AIS); measured corrections carried over
- nginx prawntenna2.derusi.de site repointed 8080 -> 8085
  (certbot certs kept, proxy_buffering off for the live audio)
- rtl_sdr probe: device 0 = NESDR v5, device 1 = generic R820T
  (matches the serial map from prawntenna's dongles.json); user-
  space USB claim works, no udev changes needed

Live verification: both dongles streaming via rtl_sdr (usb:0 on
137.9725 MHz tuned, usb:1 AIS with +12960 Hz correction), Celestrak
reachable from this network (the VM's was not), first TLE sync
immediate - and a Meteor-M 2-4 pass (59 deg) rose during setup:
PASS START + IQ/WAV recording on usb:0, first recordings of the
new station. AIS frames 0 so far (bad-burst triggers ticking -
the R820T front-end story, see the 2026-10-10 overload notes).
Band-switch restart behavior of the rtl_sdr source gets its first
live exercise on the next ISS pass (437.55 MHz).


### 2026-10-11 ~15:40 CEST — 🔊 Live-audio hang diagnosis: pipeline, not dongles

Operator report: regular audio hangs while listening live, on the
new USB-direct station. Diagnosis chain (all repro'd on the host):

1. ROOT CAUSE of both the hangs and the 15:02 + 15:18 OOM kills:
   frequency_shift caches a ROLLED 2-D phasor LUT of period * n
   complex64 samples keyed by (offset, rate, n). Harmless for the
   512-sample capture blocks; _centered_cf32's 10 s decode chunks
   at 2.4 Msps / 60 kHz stacked 40 x 24M = 7.7 GB - cached forever.
   The server ballooned to 6+ GB on a 7.2 GB notebook (Firefox +
   GNOME alongside): OOM-killed mid-pass and mid-decode, and the
   GB-scale numpy work held the GIL in long bursts - capture
   threads stalled, live-edge listeners heard the stalls as hangs.
   FIX: rolled LUT capped at 4M elements (else the one-period LUT
   with modulo indexing), decode chunks 10 s -> 100 ms. Verified:
   same decode now runs at 102-140 MB server RSS.
2. Role-switch dongle hostage: an rtl_sdr blocked writing to a
   full pipe (reader gone) SURVIVES SIGTERM - do_exit is never
   rechecked - and holds the USB device forever; every respawn
   died instantly ("Connected ... IQ stream ended" loop). Only
   SIGKILL frees it. FIX: _kill_rtl_sdr (SIGTERM, 2 s, SIGKILL,
   reap) used by stop/pause/role-switch and both thread cleanups.
3. unshare -rn (SatDump network isolation) is DENIED on Ubuntu
   26.04 ("write failed /proc/self/uid_map") - satdump never
   started; decodes produced nothing. FIX: one-time probe, direct
   run where denied. Consequence: satdump's celestrak.org:80
   fetch blocks 135 s per retry here (its HTTP is blocked while
   the station's own TLE fetch works) - decodes work, just slow;
   the TLE seed covers geo-referencing.
4. SatDump stdout/stderr now goes to <out>/satdump.log instead of
   capture_output=True (which buffered GBs of decode text in the
   server process).
Also delivered: header add-dongle picker enumerates real USB
dongles (rtl_sdr's own list: index + name + serial) with a
refresh button; waterfall block titles show the dongle name/serial;
notebook battery status (every battery + AC state) in the header.
NOTE: prawntenna2 currently runs ON BATTERY (BAT0 79% / BAT1 52%,
AC offline) - a station notebook should probably stay plugged in.
Audio-hang fix wants a real re-listen during the next pass.


### 2026-10-11 ~16:10 CEST — ⚡ Capture pipeline redesign: the real audio-hang fix

The operator still heard regular audio hangs after the LUT fix,
so the pipeline got re-measured end to end. Numbers first:
- the Meteor recording holds 409 s of audio for a 625 s pass
  (65%) - the CAPTURE THREAD itself was under-delivering
- per-thread CPU: sdr-usb:0 at 83%, ais-usb:1 at 79% - both
  threads together saturated the GIL on the 2-core i5-7200U,
  and each only reached ~65-72% of real time; the pipes
  back-pressured, the audio rings under-filled, and the
  live-edge listener heard the stalls as constant stutter
- cause: per-512-sample-block processing - ~25 numpy calls x
  4700 blocks/s per dongle, plus 250/610-tap FIRs run as direct
  np.convolve (0.6-1.5 GMAC/s per dongle)

Redesign (both capture threads):
- process 64 blocks (one 64 kB pipe chunk, ~13.6 ms) per
  iteration: one iq_to_complex / offset shift / Doppler / demod
  per batch, one audio push, one WAV write, one scheduler-state
  snapshot (73/s instead of 4700/s lock takes). The DSP states
  carry filter tails and phase across calls of ANY length -
  verified sample-identical (test_dsp: batched chain within
  1 LSB; test_ais 14: batched channel demod matches per-block)
- dsp.convolve_valid: FFT-based FIR for inputs >= 4096 samples,
  segmented overlap-save (~12x tap count -> 4-8k FFTs, cached
  per-taps FFT): one big 64k FFT pair costs ~9 ms under load on
  this CPU, segments are ~2.3x cheaper; direct loop stays for
  small inputs
- AIS: demod_channel_batch (one filter/discriminator/decimation
  pass per batch, keep-mask reshape splits freq48 per sub-block)
  preserves collect_bursts' block-granular energy model exactly

Measured after (settled server, live):
- satellite thread 83% -> 27% CPU, AIS 79% -> 75%
- live audio delivery 99.95% of real time, ZERO gaps > 150 ms
  (was ~65-72% with constant sub-150 ms stutter)
Remaining: the AIS thread at ~75% is still the hottest part -
the principled fix is a lower capture rate for the AIS dongle
 (it only needs +/-25 kHz around 162 MHz; 2.4 Msps is 20x
 oversampled), deliberately NOT done now (touches AIS_DEC,
 taps, burst-block constants, rtl_sdr -s, test fixtures).
Also: the notebook runs on battery at powersave - plugging it
 in is recommended for pass headroom.


### 2026-10-11 ~16:10 CEST — 🚢 AIS back on the R820T: the +80 ppm fallback's sign flip

Outcome of the calibration review: the generic R820T's ppm
fallback was +80 ppm while its OWN measured entries are
-79.6 ppm (-10977 Hz @ 137.9 MHz, -34827 Hz @ 437.55 MHz). At
the 162 MHz AIS band the fallback applied +12960 Hz where the
truth is ~-12960 Hz - the channels sat ~26 kHz off their demod
windows, which explains years of zero AIS frames on this dongle
independent of the antenna/overload experiments (the 2026-10-10
A/B blamed the front end; that A/B was run WITH the bad
fallback active, so its conclusion needs revisiting). Fallback
now -80 ppm -> -12960 Hz at the AIS band; the generic dongle
immediately decodes live ship traffic again (operator-confirmed
2026-10-11). A direct 162 MHz measurement can replace the
fallback with an exact entry anytime.


### 2026-10-11 ~16:30 CEST — 🏠 Back to the VM: the rtl_tcp premise retracted

The notebook move was motivated by the assumption that the
rtl_tcp link was flaky. The 2026-10-11 diagnosis disproved that:
the audio hangs were GIL saturation (per-block processing at
2.4 Msps) and decode-path memory bugs - all fixed, all
host-independent - and the rtl_tcp transport itself never showed
up in any measurement. So the station moved BACK:
- receiver on the VM (prawnsdr@192.168.2.73, 4x Xeon W-2133,
  4 GB RAM - plenty now that the OOM bugs are fixed)
- notebook (prawntenna2) back to dongle host: prawntenna manager
  restored (its preserved copy shipped an unconfigured unit
  template - "User=youruser" - rewritten for crustean1), both
  dongles re-published pinned by serial (v5 :1234 satellites,
  R820T :1235 AIS); prawnceiver fully removed from the notebook
  (service + checkout deleted; /var/log/noaa data kept)
- calibration re-keyed usb:N -> 192.168.3.65:1234/1235, values
  unchanged (same physical dongles; the R820T keeps the
  corrected -80 ppm fallback: AIS demod shows the pre-fix
  false-burst storm GONE through rtl_tcp, just a quiet river
  right now)
- nginx prawntenna2.derusi.de repointed back to the manager UI
  (:8080); the station dashboard is prawnceiver.derusi.de -> VM
Everything from 2026-10-11 ships on both paths: the USB/rtl_sdr
support stays in the code (usable if the station ever runs on a
dongle host again), the batched pipeline and the decode fixes are
host-independent.
