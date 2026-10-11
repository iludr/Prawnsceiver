"""Recording decoding: SatDump (LRPT), sstv (ISS Robot 36)."""
import os
import re
import shutil
import subprocess
import threading
import time

from .. import db
from .. import db
from .. import state

from ..config import SDR_OFFSET_HZ, SDR_RATE, TRACKED_SATS
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

def _serial_from_filename(wav_path):
    """The dongle serial suffix of a recording name, or None (primary)."""
    m = _RECORDING_RE.match(os.path.basename(wav_path))
    return m.group('serial') if m else None


def _centered_cf32(iq_path, shift_hz):
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

    Returns the temp path; the caller removes it."""
    import numpy as np
    tmp = iq_path + '.centered.c32'
    st = new_state()
    chunk = SDR_RATE * 10
    with open(iq_path, 'rb') as fin, open(tmp, 'wb') as fout:
        while True:
            raw = fin.read(chunk * 2)
            if not raw or len(raw) < 4096:
                break
            c = iq_to_complex(raw)
            c = frequency_shift(c, shift_hz, SDR_RATE, st)
            fout.write(c.astype(np.complex64).view(np.float32).tobytes())
    return tmp


def _measure_signal_offset(iq_path, signal_bw_hz=120_000):
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

    But a plain window search is fooled by LOCAL transmitters: this
    site has a persistent ~11 kHz carrier around 137.905 MHz (Region-1
    land mobile, ~7.5 kHz below the Meteor LRPT downlink) measuring
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
    n_fft = 4096 * SDR_RATE // 960000
    bin_hz = SDR_RATE / n_fft
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
        span = max(0, size - SDR_RATE * 8 * 2)
        for t in range(6):
            f.seek(int(span * t / 5) if span else 0)
            raw = f.read(SDR_RATE * 4 * 2)
            if len(raw) < SDR_RATE * 2 * 2:
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
    freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / SDR_RATE))
    avg = np.mean(slices, axis=0)
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
        # Excess power capped at 24 dB over the floor: a strong narrow
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
        # Normalize each slice to the global floor first: the tuner's
        # AGC pumps the noise floor between slices (+/-30% measured on
        # a satellite-less recording), and that wander otherwise fakes
        # a Doppler drift on low-SNR humps
        norm = [s_ / (float(np.median(s_[n_fft // 2:])) or 1.0) for s_ in slices]
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
    outputs are the biggest files, single channels and metadata lose."""
    if not os.path.isdir(out_dir):
        return None   # decoder died before creating any product
    # The map-overlay composites (SatDump's *_map.png products) are the most
    # useful single image for the dashboard; without any, the largest PNG
    # (the full composite) wins over channels and metadata.
    pngs = [name for name in os.listdir(out_dir)
            if name.lower().endswith('.png') and os.path.isfile(os.path.join(out_dir, name))]
    overlay = [n for n in pngs if n.lower().endswith('_map.png')]
    best, best_size = None, 0
    for name in (overlay or pngs):
        p = os.path.join(out_dir, name)
        if name.lower().endswith('.png') and os.path.isfile(p):
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


def _satdump_decode(pipeline, iq_path, out_dir, label, shift_hz=None):
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
            tmp = _centered_cf32(iq_path, shift_hz)
            baseband, fmt = tmp, 'cf32'
        # Run in an isolated network namespace when available: SatDump
        # retries its Celestrak TLE fetch, and while that host is
        # unreachable each retry blocks for the full 134 s TCP timeout
        # before the demodulation even starts (observed live). The TLEs
        # come from _seed_satdump_tles instead.
        argv = [exe, pipeline, 'baseband', baseband, out_dir,
                '--samplerate', str(SDR_RATE), '--baseband_format', fmt]
        if shutil.which('unshare'):
            argv = ['unshare', '-rn'] + argv
        cmd = argv
        state.log_console(f"🛰 SatDump decode started ({label}): {pipeline}")
        subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
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
    measured = _measure_signal_offset(iq_path, 120_000)
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
    state.log_console(f"Baseband centering for {os.path.basename(wav_path)}: "
                      f"signal {'measured at' if measured is not None else 'NOT FOUND, assuming'} "
                      f"{(measured if measured is not None else -SDR_OFFSET_HZ) / 1000:.1f} kHz, "
                      f"rotating {shift_hz / 1000:.1f} kHz")
    threading.Thread(target=_satdump_decode,
                     args=(pipeline, iq_path, out_dir, label, shift_hz),
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
