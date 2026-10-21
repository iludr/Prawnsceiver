"""Per-dongle calibration data.

Everything dongle-specific lives in this module — the primary dongle
selection, the FM-band range used by the correction rules, and the measured
per-dongle tuning corrections — separate from the general system config, so
recalibrating a dongle (or plugging in a new one) never touches config.py.
"""

# Dongles are either remote rtl_tcp servers ("host:port") or USB dongles
# plugged into the receiver host itself ("usb:<rtl_sdr device index>",
# read directly with rtl_sdr - see radio.py). The physical dongle behind
# an id is pinned by the serving host / the USB enumeration, so these
# entries identify a dongle by its id.
#
# Primary dongle: the id whose dongle feeds live audio and WAV recording.
# If it is not registered at startup, the first added non-AIS dongle
# becomes primary.
# Dongle host since 2026-10-17: 192.168.3.245 (the station's persisted
# dongles table wins at runtime either way - these are the static
# fallbacks, re-keyed per this module's own NOTE whenever the host IP
# changes). :1234 = satellite R820T, :1235 = R820T in the ADS-B role
# (physically the old Pi-era generic R820T, serial 77771111153705700 -
# its -79.6 ppm crystal error re-measured at -83.4 ppm on 2026-10-17),
# :1236 = AIS FC0012 (not currently published by the manager).
PRIMARY_DONGLE = "192.168.3.245:1234"

# AIS dongle: id of a dongle dedicated to ship-traffic reception
# (161.975/162.025 MHz, Danube vessels -- see noaa_receiver/ais.py). AIS
# needs a continuously listening receiver, so a dongle pinned here is
# excluded from satellite tracking and never becomes the primary.
# The FC0012's expected rtl_tcp id on the current host (192.168.3.245:1236
# per the gain table below); persisted role flags win over this pin.
# None = off.
AIS_DONGLE = "192.168.3.245:1236"

# Dongles registered at startup when no persisted list exists yet (the
# dashboard's "Add dongle" persists runtime additions in station.db's
# dongles table; that table then wins over this default). One (host,
# port) per rtl_tcp server published by the prawntenna manager.
DEFAULT_DONGLES = [
    ("192.168.3.245", 1234),   # satellite R820T (primary)
    ("192.168.3.245", 1235),   # R820T (ADS-B)
]


# FM broadcast band (VHF2) range definition
FM_BAND = (87_500_000, 108_000_000)
# Airband voice (AM): the auto demod mode switches to AM inside this
# range. Stops below the 137-138 MHz weather-satellite segment.
AIRBAND = (117_975_000, 136_975_000)

# Tuning corrections per dongle (keyed by its id - rtl_tcp address or
# "usb:<index>"; the physical dongle behind it is pinned). Every dongle mistsunes with its own
# crystal/PLL error, so the correction that centers one dongle displaces
# another (the R820T's -8.2 kHz FM-band correction left the FC0013's
# carrier +11.7 kHz off). Values are measured from the live waterfall: the
# distance of a known carrier from the FFT center is this correction
# (positive = dongle tunes low = add to the tune command).
#   'freqs': exact-frequency corrections in Hz
#   'fm_band': FM broadcast band (87.5-108 MHz) correction
#   'ppm': fallback for frequencies not listed (scaled); None = no fallback
# Dongles not listed here fall back to the primary dongle's values.
#
# MEASUREMENT METHOD (used 2026-10-17, home-grown, no host access
# needed): park the dongle on a known-exact carrier via /tune_dongle,
# grab 15-20 s of raw IQ with the manual Record button, fit the carrier
# peak (36.6 Hz FFT bins). References: the station's own identified
# DMR Tier-III control channel at 153.1125 MHz (German 4m channel
# raster) and FM broadcast 99.6 MHz. Correction = peak position + 60
# kHz (the offset tune puts a 0-error dongle's carrier at -60 kHz).
SDR_DONGLE_CORRECTIONS = {
    # Satellite R820T on 192.168.3.245:1234, measured 2026-10-17 with
    # the method above:
    #   153.1125 MHz DMR carrier: -59655.8 Hz twice (8 min apart,
    #     identical) -> +344 Hz = +2.25 ppm
    #   99.6 MHz FM carrier: -59948.7 Hz -> +51 Hz = +0.51 ppm
    # Band-to-band wander of +/-2 ppm (plain crystal, not the old v5
    # TCXO); every value below is two orders under the narrowest demod
    # here (DSB, 6 kHz) - they mostly mark the dongle as MEASURED so
    # the dashboard stops saying "unmeasured dongle" and the ppm
    # fallback is honest.
    # IMPORTANT: the true Meteor LRPT downlink is ~137.896-137.900 MHz
    # (SatNOGS 137.9000 alternate channel), NOT 137.9125. The ~17 kHz offset
    # in pass recordings (satellite at ~-77 kHz instead of -60) is the
    # transmitter frequency error. The decode's own measurement handles
    # any transmitter offset; this correction only covers the (tiny) dongle
    # error. The station now tracks at 137.900 MHz, so the LRPT band entry
    # reflects this.
    "192.168.3.245:1234": {
        "freqs": {
            137900000: 300,    # LRPT band at actual downlink freq (2.2 ppm scaled from the 153.1125 fit)
            437550000: 980,    # ISS (Zarya) (SSTV FM: kHz-scale tolerance)
        },
        "fm_band": 51,          # measured at 99.6 MHz
        "ppm": 2,
    },
    # R820T on 192.168.3.245:1235 (ADS-B role) - physically the old
    # Pi-era generic R820T (serial 77771111153705700), whose -79.6 ppm
    # crystal error re-measured 2026-10-17 as:
    #   153.1125 MHz DMR carrier: -72766 Hz -> -12766 Hz = -83.4 ppm
    # (consistent with the old -10977 Hz @ 137.9 MHz / -34827 Hz @
    # 437.55 MHz, both -79.6 ppm - same dongle, aging/temperature).
    # The Pi-era DISABLED block is superseded by this fresh measurement;
    # at 1090 MHz the error is -90.9 kHz, which the ADS-B demod's
    # full-capture-band preamble scan tolerated at correction 0 - with
    # the correction the squitters land at baseband center after the
    # offset rotation, like every other role.
    "192.168.3.245:1235": {
        "freqs": {
            1090000000: -90900,  # ADS-B center (-83.4 ppm x 1090 MHz)
        },
        "ppm": -83,
    },
    # FC0012 (AIS) expected on 192.168.3.245:1236 - NOT measured yet
    # (the manager is not currently publishing it). Measure with the
    # method above (park on 153.1125, record, fit) before trusting AIS
    # channel centering on this dongle; until then it stays
    # "unmeasured" and gets a 0 correction.
    # NESDR SMArt v5 (R820T2 + 0.5 ppm TCXO), measured 2026-10-10 during
    # high-rate mode (960 kHz capture, 1875 Hz FFT bins):
    #   - NOAA 19 DSB pass 19:09-19:11: the waterfall carrier (post-offset
    #     rotation AND post-Doppler NCO, so its position IS the raw error)
    #     held +0..+234 Hz, mean ~+40 Hz = +0.3 ppm at 137.77 MHz, while
    #     the applied Doppler swept -349..-1514 Hz over the same 2 min.
    #     That value includes the satellite's own transmitter error - the
    #     dongle alone is likely even closer to nominal.
    #   - FM broadcast 89.7-105.0: sideband-argmax, spectrum-symmetry and
    #     stereo-pilot-midpoint methods all bottom out at ~+/-2 ppm with
    #     1875 Hz bins (pilot lines share bins with program sidebands);
    #     every result was consistent with 0 (89.7 +0.95, 93.0 +2.81 ppm).
    # Corrections stay 0 on purpose: an error <= 0.5 ppm (<= 70 Hz at
    # 137 MHz) is two orders of magnitude below the narrowest demodulator
    # here (DSB, 6 kHz). The 0 entries mark the dongle as measured so the
    # dashboard stops showing 'unmeasured dongle'.
    "192.168.3.65:1234": {
        "freqs": {
            137900000: 0,   # Meteor-M 2-3 / 2-4 LRPT (actual downlink ~137.896-137.900 MHz)
            162000000: 0,   # AIS center (161.975 / 162.025 channels)
            437550000: 0,   # ISS (Zarya)
        },
        "fm_band": 0,
        "ppm": 0,
    },
}

# Per-dongle fixed tuner gain in dB. Absent address -> SDR_GAIN from config
# (0 = the tuner's AGC). Fixed gain gives a stable noise floor: AGC pumps the
# gain down as a satellite rises, which shifts the whole waterfall during a
# pass and makes pass-to-pass comparisons unreliable.
# 2026-10-08 EXPERIMENT VERDICT: 29.7 dB manual on the R820T left the ADC
# ~17-22 dB under-driven vs AGC (IQ rms 1.29 vs 9.4-15.5 u8-units = ~1% of
# ADC range, quantization eats ~3 of the 8 bits) — audio was indistinguish-
# able, IQ/digital decodes destroyed (see EVENTLOG 11:40). AGC wins until a
# fixed value is MEASURED for a specific dongle (sweep gain on a strong FM
# station / NOAA pass, pick the knee before noise floor rise); do not guess
# from a guide's number for a different dongle.
# 2026-10-10 AIS overload experiment: the R820T with the resonant 46 cm
# whip is dominated by a strong local BOS carrier at ~162.075 MHz (measured:
# invisible to the v5's 137 MHz V-dipole, gone when retuned to 137 MHz - real
# RF, not a spur). AGC pumps on it and the AIS demod decodes nothing (0 frames
# vs the v5's 493 on the same band/time, ~68 false burst triggers/s). Fixed
# 40.2 dB (~10 dB under the AGC-equivalent) tests whether the failure is AGC
# pumping or genuine front-end overload. Verdict on the whip: overload gone
# (floor 3.5 -> 2.7, false-burst storm stopped), frames still 0 - river
# went quiet, inconclusive on its own.
# 2026-10-10 antenna-swap A/B (V-dipole -> R820T, whip -> v5, both AIS
# 14:39-14:48 UTC): v5+whip caught every burst of the WA YSUR ATON beacon
# (~1/3 min, 3/3); R820T+V-dipole 0 frames and the bursts barely visible
# in its waterfall - while the SAME V-dipole had fed 493 frames to the v5
# in the morning. 40.2 dB was picked for the whip's delivered power; on
# the V-dipole it under-drives the ADC. Entry removed -> AGC (SDR_GAIN=0),
# the same gain path the v5 uses, so the next A/B isolates the dongle
# itself from the gain setting.
#
# 2026-10-17 MEASURED SWEEP (the method this file prescribes): each
# dongle paused, its rtl_tcp taken over directly, tuner gain stepped
# through the table at the ROLE frequency (satellite 137.9725 / ADS-B
# 1090.06 / AIS 162.06 MHz), 6 s IQ grab per step: IQ rms (ADC drive,
# u8 units), time-domain max (clipping) and the strongest 512-bin FFT
# peak in dBFS. Findings, all three dongles on 192.168.3.245:
#   - daemon-default AGC on BOTH R820Ts clips (time max = 127.5): the
#     satellite dongle pumps to rms 28 u8 against a recurring strong
#     carrier in the 136.8-139.2 MHz span (peak bin -7.7 dBFS at 49.4 dB
#     manual), and the ADS-B dongle pumps up between 1090 MHz pulses
#     and clips on real squitters - the moving noise floor the ADS-B
#     demod's threshold then chased (see the vectorized preamble scan).
#   - No gain knee anywhere: the floor rises only 18.5 dB (satellite) /
#     4.5 dB (ADS-B) over the full 44 dB range - tuner-noise limited;
#     more gain never buys SNR here, it only fixes ADC quantization.
#   - 49.4 dB on the satellite dongle clips on the strong carrier (17
#     samples > 110 of 14.4M); 43.9 dB holds 5.3 dB of headroom
#     (max 69.5) at rms 5.9 u8 - 4.6x the drive of the old 29.7 dB
#     experiment, without intermod during passes.
#   - ADS-B at 49.4 dB: rms 2.3, time max 54.5 with live traffic - no
#     clipping anywhere near; maximum gain it is (the R820T is nearly
#     deaf at 1090 MHz, every dB counts).
#   - The FC0012 (AIS) tops out at 21.6 dB and even then drives the ADC
#     to only rms 1.5-1.6 u8 (its daemon default sat at 0.71 = ~1 bit
#     of noise). Deaf tuner, but max gain + a stable floor is all it
#     has; AIS frames are strong (river ships are close).
# NOTE: keys are the CURRENT rtl_tcp addresses on 192.168.3.245 - when
# the dongle host's IP changes (WiFi/wired), the persisted dongles
# table AND these entries must be re-keyed.
SDR_DONGLE_GAIN = {
    "192.168.3.245:1234": 43.9,   # satellite (R820T): knee-free, clip-free
    "192.168.3.245:1235": 49.4,   # ADS-B (R820T): max drive, no clipping
    "192.168.3.245:1236": 21.6,   # AIS (FC0012): tuner maximum
}

def correction_info(freq_hz, dongle=None):
    """(correction_hz, source) describing the tuning correction applied to a
    dongle at a frequency (see SDR_DONGLE_CORRECTIONS). The source labels
    where the value comes from: an exact measured frequency, the FM-band
    rule, a scaled ppm fallback, or nothing.

    An UNKNOWN address gets a zero correction marked 'unmeasured dongle':
    inheriting the primary's table would mis-tune a modern TCXO dongle
    (e.g. a NESDR SMArt v5 at 0.5 ppm) by ~11 kHz on first use. The dongle
    cards' tuning infobox shows the marker until the dongle gets measured
    and its own entry."""
    d = SDR_DONGLE_CORRECTIONS.get(dongle)
    if d is None:
        return 0, "unmeasured dongle"
    freqs = d.get("freqs", {})
    if freq_hz in freqs:
        return freqs[freq_hz], "measured"
    if FM_BAND[0] <= freq_hz <= FM_BAND[1]:
        return d.get("fm_band", 0), "FM-band"
    if d.get("ppm"):
        return int(round(freq_hz * d["ppm"] / 1e6)), "%d ppm" % d["ppm"]
    return 0, "none"

def tuning_correction(freq_hz, dongle=None):
    """Static tuning correction for one dongle's crystal/PLL error at a
    frequency (see SDR_DONGLE_CORRECTIONS)."""
    return correction_info(freq_hz, dongle)[0]
