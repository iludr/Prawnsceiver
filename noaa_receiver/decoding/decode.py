"""Recording decoding: SatDump (LRPT), sstv (ISS Robot 36)."""
import os
import re
import shutil
import subprocess
import threading
import time

from .. import db
from .. import state

from ..config import (LOCAL_CARRIER_BASEBAND_HZ, RECORD_LOCAL_DIR,
                      SDR_OFFSET_HZ, SDR_RATE, TRACKED_SATS)
from ..sdr.dsp import frequency_shift, iq_to_complex, new_state

# Recording names are '<sat>_<YYYYMMDD>_<HHMMSS>[-<n>][_<serial>].wav', written by
# radio.sdr_capture_thread via sat_short_name(). The satellite part is
# matched lazily up to the first timestamp, so names with underscores work.
_RECORDING_RE = re.compile(r'^(?P<sat>.+?)_(?P<ts>\d{8}_\d{6})(?:-\d+)?(?:_(?P<serial>.+))?\.wav$', re.I)


def sat_short_name(sat_name):
    """Filename-safe satellite name used in recording names."""
    return sat_name.replace(" ", "_").replace("(idle)", "idle")


def satellite_from_filename(wav_path):
    """The tracked satellite a recording belongs to (canonical name from
    TRACKED_SATS), or None if the filename does not name one.

    Exact match on the name part — a substring test ('iss' in name) would
    misroute any satellite whose name merely contains it.
    """
    m = _RECORDING_RE.match(os.path.basename(wav_path))
    if not m:
        return None
    short = m.group('sat').lower()
    if short.endswith('_idle'):
        short = short[:-len('_idle')]
    for name, _freq in TRACKED_SATS.values():
        if sat_short_name(name).lower() == short:
            return name
    return None


def _png_path(wav_path):
    base, _ = os.path.splitext(wav_path)
    return base + '.png'

# ---------- decode-attempt markers ----------
# The dashboard auto-decodes every undecoded recording on every history-page
# load, and a decode that can never succeed (dark transmitter, ISS outside
# an ARISS event, missing raw IQ) was re-run each time — loading big WAVs
# into this process until it OOMed (2.4 GB, killed by the kernel once).
# Each attempt's outcome is therefore persisted next to the recording and
# short-circuits every later attempt unless force=True (Retry button).

def read_decode_marker(wav_path):
    """The recorded outcome of a previous decode attempt, or None.

    Stored in the station database (decodes table), keyed by the
    recording's base name so absolute paths and bare filenames
    resolve to the same row.
    """
    base, _ = os.path.splitext(os.path.basename(wav_path))
    row = db.query_one("SELECT success, message, ts FROM decodes"
                       " WHERE base=?", (base,))
    if row is None:
        return None
    return {'success': bool(row['success']), 'message': row['message'],
            'ts': row['ts']}


def _write_decode_marker(wav_path, success, message):
    base, _ = os.path.splitext(os.path.basename(wav_path))
    try:
        with db.write() as cur:
            cur.execute("INSERT OR REPLACE INTO decodes VALUES (?,?,?,?)",
                        (base, 1 if success else 0, str(message),
                         time.time()))
    except Exception as e:
        state.log_console(f"Decode marker write failed: {e}", "warn")


def forget_decode_marker(wav_path):
    """Drop the attempt marker - the recording was deleted (/delete)."""
    base, _ = os.path.splitext(os.path.basename(wav_path))
    try:
        with db.write() as cur:
            cur.execute("DELETE FROM decodes WHERE base=?", (base,))
    except Exception:
        pass


def drop_stale_spawn_markers():
    """At startup: delete decode markers of background decodes that were
    spawned but never finished.

    _decode_iq writes a 'success' marker when it SPAWNS the detached
    SatDump run; only the run itself rewrites the marker with the real
    outcome. A station restart mid-decode kills the daemon thread
    before that rewrite, leaving the recording marked 'success' with no
    products - and the marker short-circuits every later auto-decode,
    so the pass is never retried (observed live 2026-10-17: four whole
    LRPT passes, strong signals included, sat undecodable this way).
    Deleting those rows lets the normal decode sweeps pick the
    recordings up again."""
    try:
        with db.write() as cur:
            cur.execute("DELETE FROM decodes WHERE message LIKE ?",
                        ('%decode started in the background%',))
            dropped = cur.rowcount if cur.rowcount > 0 else 0
    except Exception as e:
        state.log_console(f"Stale spawn-marker cleanup failed: {e}", "warn")
        return
    if dropped:
        state.log_console(f"🧹 Dropped {dropped} unfinished background-decode "
                          f"marker(s) - those recordings will be re-decoded")

def _serial_from_filename(wav_path):
    """The dongle serial suffix of a recording name, or None (primary)."""
    m = _RECORDING_RE.match(os.path.basename(wav_path))
    return m.group('serial') if m else None


def _centered_tmp_path(iq_path):
    """Local path for the centered-baseband temp of a raw IQ file.

    ALWAYS on the LOCAL capture disk — never next to the IQ, which can
    sit on the archive share: the cf32 temp is 4x the IQ size (a full
    Meteor pass writes ~29 GB), and writing that over NFSv3 killed
    every big decode with ENOSPC while the share was tight, leaving
    orphaned temps the archiver's leftover sweep then shipped to the
    share (71 files, one 10.5 GB, measured 2026-10-17). The dot prefix
    keeps the archiver from ever claiming it (it skips dotfiles), and
    the deterministic name lets a new attempt remove a stale leftover
    of a killed run first.
    """
    base, _ = os.path.splitext(os.path.basename(iq_path))
    return os.path.join(RECORD_LOCAL_DIR, '.' + base + '.centered.c32')


def _centered_cf32(iq_path, shift_hz, rate=SDR_RATE):
    """Rewrite the raw u8 IQ as cf32 with the satellite rotated to 0 Hz.

    The .iq.u8 files hold the RAW rtl_tcp stream: the dongle is tuned
    SDR_OFFSET_HZ + correction above the target, so the satellite sits at
    -(SDR_OFFSET_HZ + correction) Hz in the file (plus Doppler). SatDump
    baseband pipelines demodulate around 0 Hz — decoding the raw file
    yields no lock at all (verified live on the 04:29 80.8-degree pass:
    clear +8 dB / 72 kHz LRPT plateau in the FFT, zero products from
    the raw decode). The rotation is chunked (phase-continuous via the
    DSP state) so memory stays bounded; residual Doppler (a few kHz,
    slowly varying) is left to the demod's frequency tracking.

    Returns the temp path (see _centered_tmp_path); the caller removes it.
    rate is the RECORDING's capture rate (see _iq_rate), not today's."""
    import numpy as np
    tmp = _centered_tmp_path(iq_path)
    try:
        os.remove(tmp)     # leftover of a killed earlier run, if any
    except OSError:
        pass
    st = new_state()
    # 100 ms of IQ per chunk: the phase-continuity state makes any chunk
    # size correct, and this keeps every per-chunk array small. The old
    # 10 s chunk at 2.4 Msps built a 7.7 GB rolled phasor LUT inside
    # frequency_shift and OOM-killed the 7.2 GB notebook twice
    chunk = rate // 10
    with open(iq_path, 'rb') as fin, open(tmp, 'wb') as fout:
        while True:
            raw = fin.read(chunk * 2)
            if not raw or len(raw) < 4096:
                break
            c = iq_to_complex(raw)
            c = frequency_shift(c, shift_hz, rate, st)
            fout.write(c.astype(np.complex64).view(np.float32).tobytes())
    return tmp


# Historical capture rates the station has run (240 kHz -> 960 kHz ->
# 2.4 Msps). A recording's own rate is derived from its bytes: the WAV
# duration is authoritative and IQ bytes / 2 / duration is the rate
# (u8 IQ = 2 bytes per sample).
_HISTORICAL_RATES = (2_400_000, 960_000, 240_000)


def _iq_rate(wav_path, iq_path):
    """The capture rate of one raw IQ recording (Hz), or SDR_RATE when it
    cannot be derived.

    The decode chain must follow the rate the RECORDING was captured at,
    not today's: the measurement's FFT bin scaling, the centering
    rotation and SatDump's --samplerate all depend on it. The station
    ran 240 kHz / 960 kHz / 2.4 Msps eras, and decoding an old recording
    with the current rate squashes the spectrum by the rate ratio and
    demodulates garbage (the 2026-10-17 backlog re-decode: 960 kHz-era
    IQs are undecodable without this)."""
    try:
        import wave
        with wave.open(wav_path, 'rb') as w:
            dur = w.getnframes() / float(w.getframerate())
        rate = os.path.getsize(iq_path) / 2.0 / dur
        for r in _HISTORICAL_RATES:
            if abs(rate - r) <= 0.02 * r:
                return r
    except Exception:
        pass
    return SDR_RATE


def _measure_signal_offset(iq_path, signal_bw_hz=120_000, rate=SDR_RATE):
    """Where the satellite actually sits in a raw IQ capture (Hz from
    baseband center), measured from the recording itself.

    Static models are not trustworthy here: the dongle's tuning error is
    only approximated by the calibration ppm, transmitters can be off
    their nominal frequency, and Doppler adds a few kHz. So the decode
    centering measures the signal's spectral position instead of
    trusting any of that.

    The search is a signal-width-matched sliding window (mean power per
    window, satellite side of the tuner only, so the +SDR_OFFSET_HZ DC
    spike and the R820T spurs cannot win a naive peak search).

    The slices are combined per-slice-floor-normalized and with a MAX,
    not an average. A LEO downlink is only above the noise for the part
    of the recording near culmination: the rise/set slices are near
    horizon and carry nothing, and an average drags the mid-pass plateau
    down to noise (measured live 2026-10-17 on the 87.9-degree Meteor
    M2-4 pass: the best slice held the LRPT plateau at 8.7x its own
    floor while the 6-slice average showed 2.8x — under the 4x
    confidence gate, so EVERY real pass measured None and the decode
    fell back to the nominal tuner offset, 17 kHz off for the then
    uncalibrated dongle). Normalizing first also removes the tuner
    AGC's +/-30% floor wander between slices; taking the max per bin
    keeps the strongest view of the sky. A stationary interferer is
    present in ALL slices, so it survives the max unchanged and stays
    subject to the drift gate below.

    But a plain window search is fooled by LOCAL transmitters: this
    site has a persistent ~11 kHz carrier around 137.905 MHz (Region-1
    land mobile, ~7.5 kHz above the Meteor LRPT downlink) measuring
    20-40x the noise floor, which captured the measurement live
    (2026-10-10: -47.6 kHz "measured" from that carrier while a real
    LRPT signal would sit near -60 kHz; the demodulator then centered
    empty spectrum). A LEO downlink differs from local junk in exactly
    two measurable ways: it DRIFTS with Doppler (several hundred Hz
    between the first and last thirds of a recording, +/-3.4 kHz over
    a pass at 137.9 MHz) and the wide digital modes fill tens of kHz.
    The winning window's excess-power region is therefore gated:

      - width >= 45% of signal_bw_hz -> a broadband satellite signal
        (low passes drift little, so width must be able to carry them)
      - else centroid drift between the recording's first and last
        thirds >= 300 Hz -> a moving carrier: the satellite
      - otherwise the feature is stationary and narrow: a local
        transmitter -> None, and the caller falls back to the
        tuner-offset rotation (correct whenever the correction is)

    Returns None when nothing passes the gates.
    """
    import numpy as np
    # ~234 Hz measurement bins at ANY capture rate (4096 at 960 kHz): the
    # width gate below compares a bin-quantized width against absolute Hz,
    # so coarse bins would pass a narrow stationary carrier as "wide" (its
    # five-bin core measures 2.9 kHz at 2.4 MHz - over the 2.7 kHz DSB gate)
    n_fft = 4096 * rate // 960000
    bin_hz = rate / n_fft
    # Hann-window the segments: a strong carrier under a boxcar FFT
    # splatters a sinc skirt above the noise test across tens of kHz,
    # which pins the centroid/drift tests onto a stationary spike
    hann = np.hanning(n_fft).astype(np.float32)
    # window centers to try (satellite side only)
    center_lo, center_hi = -105_000, -25_000
    slices = []
    with open(iq_path, 'rb') as f:
        size = os.path.getsize(iq_path)
        # Slice starts in BYTES spread across the WHOLE file (the six
        # 4-s slices must cover it: the drift test compares the first
        # and last thirds of the recording). The margin keeps the last
        # slice's read inside the file. An earlier version halved the
        # span by mixing sample and byte units - its slices covered
        # only the first quarter of the recording, so the drift test
        # saw almost no Doppler at all.
        span = max(0, size - rate * 8 * 2)
        for t in range(6):
            f.seek(int(span * t / 5) if span else 0)
            raw = f.read(rate * 4 * 2)
            if len(raw) < rate * 2 * 2:
                break
            c = iq_to_complex(raw)
            segs = c[:len(c) // n_fft * n_fft].reshape(-1, n_fft)
            psd = np.zeros(n_fft)
            for s_ in segs:
                psd += np.abs(np.fft.fftshift(np.fft.fft(s_ * hann))) ** 2
            psd /= len(segs)
            slices.append(np.convolve(psd, np.ones(5) / 5, 'same'))
    if not slices:
        return None
    freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / rate))
    # Per-slice floor normalization: the tuner's AGC pumps the noise
    # floor between slices (+/-30% measured on a satellite-less
    # recording), and that wander otherwise fakes signal strength.
    # The normalized slices also feed the drift test below.
    norm = [s_ / (float(np.median(s_[n_fft // 2:])) or 1.0) for s_ in slices]
    # MAX over the normalized slices, not the mean: only the slices
    # near culmination carry the downlink (see docstring).
    avg = np.max(norm, axis=0)
    
    # SITE-SPECIFIC FIX: this site has a persistent land-mobile carrier
    # (config.LOCAL_CARRIER_NOTCH_HZ, ~137.905 MHz, 20-40x the noise floor)
    # ~5-9 kHz above the LRPT downlink, INSIDE the ~72 kHz wide LRPT band.
    # Its baseband position comes from the tune (config.LOCAL_CARRIER_BASEBAND_HZ
    # = carrier - (target + SDR_OFFSET_HZ), -55 kHz for the 137.9000 tune), so
    # a retune cannot silently misplace the notch. The carrier inflates the
    # width-gate threshold (peak/64) and skews the centroid; zero a very
    # narrow +/-200 Hz notch around it to dent it without damaging the
    # LRPT plateau.
    local_carrier_hz = LOCAL_CARRIER_BASEBAND_HZ
    notch_bins = max(1, int(200 / bin_hz))  # +/-200 Hz notch
    carrier_bin = n_fft // 2 + int(local_carrier_hz / bin_hz)
    lo_notch = max(0, carrier_bin - notch_bins)
    hi_notch = min(len(avg), carrier_bin + notch_bins + 1)
    avg[lo_notch:hi_notch] = 0.0
    # Noise reference: the POSITIVE-frequency half. The satellite is
    # always on the negative side (offset tuning), so the positive half
    # is receiver noise (the +SDR_OFFSET_HZ DC spike is a few bins and
    # cannot move a median). The full-spectrum median would sit ON a
    # plateau that fills half the capture - exactly the LRPT geometry
    # (a 120 kHz signal in a 2.4 MHz recording) - and the score gate
    # would reject the satellite it just found.
    med = float(np.median(avg[n_fft // 2:]))
    half = max(1, int(signal_bw_hz / 2 / bin_hz))
    acc = np.concatenate(([0.0], np.cumsum(avg)))
    best_c, best_p = None, -1.0
    lo_bin = n_fft // 2 + int(center_lo / bin_hz)
    hi_bin = n_fft // 2 + int(center_hi / bin_hz)
    for center_bin in range(lo_bin, hi_bin):
        a, b = center_bin - half, center_bin + half
        if a < 0 or b >= len(avg):
            continue
        mean_p = (acc[b + 1] - acc[a]) / (b - a)
        if mean_p > best_p:
            best_p, best_c = mean_p, center_bin
    if best_c is None or best_p < 4.0 * (med or 1.0):
        # Nothing CONFIDENTLY above the floor. The tuner-offset fallback
        # rotation is the safer center on a calibrated dongle, so only a
        # clear detection may override it: a satellite-less recording's
        # AGC/tuner-shape hump measured 3.0x here and would have won at
        # the old 2.0x threshold
        return None
    # Excess-power region inside the winning window (above 3x the
    # spectrum median) and its drift across the recording
    a, b = best_c - half, best_c + half
    region = np.arange(a, b)[avg[a:b] > 3.0 * med]
    if not len(region):
        return None

    def _centroid(psd):
        # Excess power capped at 16x the floor (~12 dB): a strong narrow
        # carrier out-powers a whole broadband plateau bin-for-bin and
        # pins the centroid (and the drift test) onto itself; the cap
        # keeps satellite plateaus and local spikes on one scale
        excess = np.minimum(np.maximum(psd[region] - med, 0.0), 16.0 * med)
        total = float(excess.sum())
        if total <= 0.0:
            return None
        return float((freqs[region] * excess).sum() / total)

    # Wide-signal test: the -18 dB width of the window's peak. A strong
    # narrow carrier drags its sinc skirt above the noise test across
    # the whole window (a stationary land-mobile carrier measured
    # 6 kHz 'wide' at the DSB window size), so the width gate cuts at
    # max(3x median, peak/64) as well: a flat digital plateau keeps
    # nearly all its bins, a carrier collapses to its few-bin core.
    win = avg[a:b]
    width_hz = int((win > max(3.0 * med, float(win.max()) / 64.0)).sum()) * bin_hz
    drift_hz = 0.0
    third = max(1, len(slices) // 3)
    if len(slices) >= 2 * third:
        # The normalized slices (norm, see above) remove the AGC floor
        # wander that would otherwise fake a Doppler drift on low-SNR
        # humps; the first and last thirds are averaged back to the
        # combined floor scale for the region/centroid weighting.
        c_first = _centroid(np.mean(norm[:third], axis=0) * med)
        c_last = _centroid(np.mean(norm[-third:], axis=0) * med)
        if c_first is not None and c_last is not None:
            drift_hz = abs(c_last - c_first)
    if width_hz >= 0.45 * signal_bw_hz or drift_hz >= 300.0:
        center = _centroid(avg)
        return float(center if center is not None else freqs[best_c])
    return None


def _pick_product_png(out_dir):
    """The composite image from a SatDump product directory, or None.

    The dashboard and history serve decoded recordings as ONE flat PNG
    next to the WAV and never list product subdirectories, so a finished
    decode reduces to its largest PNG: the full composite (RGB/IR)
    outputs are the biggest files, single channels and metadata lose.

    SatDump v1.2.0 nests the instrument images one level deep
    (<out>/MSU-MR/*.png — verified live 2026-10-17: a full LRPT decode
    with 1800 lines/channel left NO png at the top level), so the
    search covers the top level and immediate subdirectories."""
    if not os.path.isdir(out_dir):
        return None   # decoder died before creating any product
    # The map-overlay composites (SatDump's *_map.png products) are the most
    # useful single image for the dashboard; without any, the largest PNG
    # (the full composite) wins over channels and metadata.
    pngs = []
    for name in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, name)
        if os.path.isfile(p):
            if name.lower().endswith('.png'):
                pngs.append(name)
        elif os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp) and sub.lower().endswith('.png'):
                    pngs.append(os.path.join(name, sub))
    overlay = [n for n in pngs if n.lower().endswith('_map.png')]
    best, best_size = None, 0
    for name in (overlay or pngs):
        p = os.path.join(out_dir, name)
        size = os.path.getsize(p)
        if size > best_size:
            best, best_size = p, size
    return best


def _seed_satdump_tles():
    """Keep SatDump's TLE file fed from the receiver's own TLE cache
    (station database, tle_cache table) - SatDump cannot fetch
    Celestrak from this network, and with no TLEs it cannot
    geo-reference Meteor images."""
    try:
        rows = db.query("SELECT name, l1, l2 FROM tle_cache")
        lines = []
        for r in rows:
            lines.extend([r['name'].strip(), r['l1'].strip(), r['l2'].strip()])
        if not lines:
            return
        path = os.path.expanduser('~/.config/satdump/satdump_tles.txt')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass   # best effort - SatDump runs without TLEs, just without maps


_UNSHARE_OK = None

def _unshare_ok():
    """unshare -rn (network namespace for the SatDump run, so its Celestrak
    TLE retries cannot stall the decode - see _satdump_decode) is not
    available everywhere: Ubuntu 26.04 restricts unprivileged user
    namespaces ("write failed /proc/self/uid_map", observed on
    prawntenna2). Probed once; a failing probe runs SatDump without
    isolation (the TLEs are seeded by _seed_satdump_tles either way)."""
    global _UNSHARE_OK
    if _UNSHARE_OK is None:
        exe = shutil.which('unshare')
        if not exe:
            _UNSHARE_OK = False
        else:
            try:
                _UNSHARE_OK = subprocess.run([exe, '-rn', 'true'],
                                             capture_output=True,
                                             timeout=10).returncode == 0
            except Exception:
                _UNSHARE_OK = False
    return _UNSHARE_OK


def _satdump_decode(pipeline, iq_path, out_dir, label, shift_hz=None,
                     rate=SDR_RATE):
    """Decode a raw IQ recording with SatDump (runs minutes on the Pi — the
    caller spawns this detached so the scheduler never blocks on it). The
    out_dir doubles as the run marker: existing = already decoded/decoding.

    The IQ is first rotated so the satellite lands at 0 Hz (SatDump's
    baseband pipelines expect a centered signal — see _centered_cf32).
    Note: SatDump also retries fetching TLEs from Celestrak at startup;
    while that host is unreachable each retry costs ~134 s before the
    actual demodulation even starts."""
    exe = shutil.which('satdump')
    if not exe:
        state.log_console(f"SatDump is not installed - cannot decode {label}", "warn")
        return
    os.makedirs(out_dir, exist_ok=True)
    _seed_satdump_tles()
    baseband, fmt = iq_path, 'cu8'
    tmp = None
    try:
        if shift_hz:
            state.log_console(f"Centering baseband for {label}: rotating {shift_hz} Hz")
            tmp = _centered_cf32(iq_path, shift_hz, rate)
            baseband, fmt = tmp, 'cf32'
        # Run in an isolated network namespace when available: SatDump
        # retries its Celestrak TLE fetch, and while that host is
        # unreachable each retry blocks for the full 134 s TCP timeout
        # before the demodulation even starts (observed live). The TLEs
        # come from _seed_satdump_tles instead.
        argv = [exe, pipeline, 'baseband', baseband, out_dir,
                '--samplerate', str(rate), '--baseband_format', fmt]
        if _unshare_ok():
            argv = ['unshare', '-rn'] + argv
        cmd = argv
        state.log_console(f"🛰 SatDump decode started ({label}): {pipeline}")
        # satdump's console output goes to a log file, NOT capture_output:
        # a 12-min 2.4 Msps decode prints gigabytes of progress text and
        # subprocess.run(capture_output=True) buffers ALL of it in this
        # process's RAM (the 6 GB OOM kill on prawntenna2, 2026-10-11)
        #
        # Its TLE self-update is another trap: satdump curls
        # celestrak.org on EVERY run, that host is unreachable from the
        # station network, and libcurl waits out its full ~135 s timeout
        # PER RETRY - observed looping there for 13+ minutes without
        # ever starting the demodulation (2026-10-11). unshare -rn used
        # to isolate that, but unprivileged user namespaces are denied
        # on Ubuntu 26.04. Fix for all hosts: libcurl honors the proxy
        # env vars, and a dead LOCAL port fails the connect in ~0 s -
        # satdump gives up on the update instantly and proceeds with
        # the TLEs _seed_satdump_tles wrote for it.
        env = dict(os.environ)
        env['http_proxy'] = env['https_proxy'] = 'http://127.0.0.1:1'
        with open(os.path.join(out_dir, 'satdump.log'), 'wb') as slog:
            subprocess.run(cmd, stdout=slog, stderr=slog, timeout=3600,
                           env=env)
    except subprocess.TimeoutExpired:
        state.log_console(f"SatDump decode timed out ({label})", "warn")
        return
    except Exception as e:
        state.log_console(f"SatDump decode error ({label}): {e}", "error")
        return
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    products = [f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))]
    # Rewrite the decode marker with the real outcome: decode_recording
    # marked the recording 'success' when it spawned this background
    # job, but the truth is known only here (products or not). A
    # 'no products' marker keeps later auto-decodes from re-running a
    # permanently dark transmitter; Retry still can, with force.
    wav_path = iq_path[:-len('.iq.u8')] + '.wav'
    image = _pick_product_png(out_dir)
    if image is not None:
        # Copy the composite into the flat <recording>.png slot: the
        # dashboard/history list decoded images as one PNG next to the
        # WAV and never look inside the product directory.
        shutil.copyfile(image, iq_path[:-len('.iq.u8')] + '.png')
        state.log_console(f"🛰 SatDump decode done ({label}): {len(products)} product file(s), composite {os.path.basename(image)}")
        _write_decode_marker(wav_path, True, f"SatDump: {len(products)} products, composite {os.path.basename(image)}")
        # The raw IQ did its job: the decode succeeded, and the u8 baseband
        # is 4x the WAV (a full 2.4 Msps pass is ~3.5 GB) with no further
        # use — the composite, the product images and the WAV carry the
        # outcome (operator request 2026-10-17). Deleted on SUCCESS only:
        # a recording whose transmitter stayed dark keeps its IQ so the
        # decode can be retried by hand.
        try:
            iq_mb = os.path.getsize(iq_path) / (1024 * 1024)
            os.remove(iq_path)
            state.log_console(f"🧹 Raw IQ deleted after successful decode: "
                              f"{os.path.basename(iq_path)} ({iq_mb:.0f} MB) — images kept")
        except OSError:
            pass
    elif products:
        state.log_console(f"🛰 SatDump decode done ({label}): {len(products)} product file(s) in {os.path.basename(out_dir)}/ (no image)")
        _write_decode_marker(wav_path, True, f"SatDump: {len(products)} product file(s) (no image)")
    else:
        state.log_console(f"SatDump produced no products ({label}) - signal too weak or transmitter off", "warn")
        _write_decode_marker(wav_path, False, "SatDump produced no products - signal too weak or transmitter off")

def _decode_iq(wav_path, pipeline, suffix, force=False):
    """SatDump decode of the raw IQ sibling of a recording. Returns
    (started, message); spawns the actual work detached."""
    base, _ = os.path.splitext(wav_path)
    iq_path, out_dir = base + '.iq.u8', base + suffix
    label = os.path.basename(wav_path)
    if not os.path.exists(iq_path):
        return False, f'No raw IQ capture for {label} - cannot decode'
    if os.path.exists(out_dir):
        if not force:
            return True, 'already decoded'
        # Retry after a failed run: start the product directory fresh
        shutil.rmtree(out_dir, ignore_errors=True)
    # Where the satellite sits in the raw baseband: measured from the IQ
    # itself (the dongle error is modeled, transmitters can be off their
    # nominal frequency, Doppler shifts a few kHz — see
    # _measure_signal_offset). The tuner offset is the fallback.
    # LRPT (Meteor M2-X: 80 kbaud OQPSK) fills roughly +/-60 kHz - the
    # window width rejects narrowband spurs, and the measurement's
    # drift/width gates reject local carriers (see _measure_signal_offset)
    # The whole chain runs at the RECORDING's own capture rate: the
    # station ran 240 kHz / 960 kHz / 2.4 Msps eras, and the bin scaling,
    # rotation and SatDump samplerate are all rate-dependent (see _iq_rate).
    rate = _iq_rate(wav_path, iq_path)
    measured = _measure_signal_offset(iq_path, 120_000, rate)
    # _measure_signal_offset returns the signal's POSITION in the baseband
    # (negative Hz, e.g. -71425 for a satellite 71.4 kHz below center);
    # frequency_shift rotates BY its argument (signal at f -> f + shift),
    # so centering the signal at 0 needs the NEGATIVE of the position.
    # The SDR_OFFSET_HZ fallback is already a rotation amount (+60 kHz
    # centers a signal at -60 kHz). Passing the raw position rotated the
    # signal to 2x its offset — every decode whose measurement succeeded
    # (i.e. every strong pass) demodulated empty spectrum: the whole
    # 0-byte-CADU LRPT streak and the empty DSB products.
    shift_hz = -measured if measured is not None else SDR_OFFSET_HZ
    rate_note = "" if rate == SDR_RATE else f" @ {rate // 1000} kHz capture rate"
    state.log_console(f"Baseband centering for {os.path.basename(wav_path)}: "
                      f"signal {'measured at' if measured is not None else 'NOT FOUND, assuming'} "
                      f"{(measured if measured is not None else -SDR_OFFSET_HZ) / 1000:.1f} kHz, "
                      f"rotating {shift_hz / 1000:.1f} kHz{rate_note}")
    def _run_satdump(pipeline=pipeline, iq_path=iq_path, out_dir=out_dir,
                     label=label, shift_hz=shift_hz, rate=rate):
        # Registered for the archiver for the whole background run: the
        # decode marker written at spawn time already says 'success', so
        # the family must stay on the local disk until the products are
        # really there (see archive.py)
        base = os.path.splitext(os.path.basename(iq_path))[0]
        state.decodes_active.add(base)
        try:
            _satdump_decode(pipeline, iq_path, out_dir, label, shift_hz, rate)
        finally:
            state.decodes_active.discard(base)
    threading.Thread(target=_run_satdump,
                     daemon=True, name='satdump').start()
    return True, 'decode started in the background (SatDump)'


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def decode_recording(wav_path, force=False):
    """Decode a pass recording to a PNG next to the WAV.

    The decoder is chosen by satellite, detected from the recording
    filename. The outcome of every attempt is written to a decode marker
    (<base>.decode.json) and short-circuits later attempts — see the
    marker block above. force=True re-runs and overwrites the marker.

    Returns (success, png_path, error_message).
    """
    if not force:
        marker = read_decode_marker(wav_path)
        if marker is not None:
            if marker.get('success'):
                png = _png_path(wav_path)
                return True, (png if os.path.exists(png) else None), marker.get('message')
            return False, None, f"decode already attempted: {marker.get('message')}"
    # Registered for the archiver: a decode in progress (this function or
    # the background SatDump run it may spawn) keeps the recording family
    # on the local disk until it finished (see archive.py).
    _base = os.path.splitext(os.path.basename(wav_path))[0]
    state.decodes_active.add(_base)
    try:
        output_png = _png_path(wav_path)
        sat = satellite_from_filename(wav_path)
        if sat is None:
            state.log_console(f"Decode: no tracked satellite in the name of {os.path.basename(wav_path)}", "warn")
            result = (False, None, 'no tracked satellite in the recording name - cannot decode')
        elif sat.startswith('ISS'):
            result = _decode_sstv(wav_path, output_png)
        elif sat.startswith('Meteor'):
            # LRPT is a ~72 kHz wide OQPSK digital mode - the FM-demod audio
            # cannot carry it, but the raw IQ capture can (meteor_m2-x_lrpt)
            started, msg = _decode_iq(wav_path, 'meteor_m2-x_lrpt', '_lrpt', force)
            result = (started, None, f'LRPT recording - {msg}')
        else:
            result = (False, None, f'{sat} has no decoder in this station')
        _write_decode_marker(wav_path, result[0], result[2] or 'decoded')
        return result
    finally:
        # NOTE: a background SatDump run keeps its OWN entry (registered
        # inside _satdump_decode) until it wrote its products - the
        # marker this function wrote at spawn time says 'success', but
        # the analysis is only done when that entry clears.
        state.decodes_active.discard(_base)


def _decode_sstv(wav_path, output_png):
    """Decode ISS Slow-Scan TV (Robot 36 during ARISS events).

    The sstv library auto-detects the mode from the VIS header and returns
    every image in the recording; the ISS repeats images during a pass, so
    extras are saved as <base>_2.png, <base>_3.png, ...
    """
    try:
        import sstv
    except ImportError:
        return False, None, 'sstv decoder not installed (pip install sstv)'
    try:
        images = sstv.decode_from_wav(wav_path)
    except Exception as e:
        return False, None, f'SSTV decode error: {e}'
    if not images:
        return False, None, 'No SSTV transmission found in recording'
    base, _ = os.path.splitext(output_png)
    # Drop leftovers of an earlier decode so the set on disk is this one's
    _remove_quietly(output_png)
    for old in range(2, 100):
        if not os.path.exists(f'{base}_{old}.png'):
            break
        _remove_quietly(f'{base}_{old}.png')
    try:
        images[0].save(output_png)
        for i, image in enumerate(images[1:], start=2):
            image.save(f'{base}_{i}.png')
    except Exception as e:
        return False, None, f'Cannot write SSTV image: {e}'
    return True, output_png, None
