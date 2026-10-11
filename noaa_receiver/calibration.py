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
# 2026-10-08: the NESDR SMArt v5 (R820T2, 0.5 ppm TCXO) is the primary; the
# R820T listens for AIS. The station moved BACK to the VM on 2026-10-11
# (the flaky-rtl_tcp premise was disproved: that day's audio hangs were
# GIL saturation + decode memory bugs, both fixed and host-independent);
# the notebook serves both dongles again via the prawntenna manager,
# pinned by serial: v5 48263793 -> rtl_tcp :1234 (satellites), generic
# R820T 77771111153705700 -> :1235 (AIS). Wired Ethernet since
# 2026-10-11 evening (192.168.3.35): WiFi could not carry 2x2.4 Msps
# (76.8 Mbit/s over 2.4 GHz = link saturation, 50-375 ms RTT to the VM
# and starved capture streams - THAT was the original "flaky
# connection"; wired: 0.7 ms).
PRIMARY_DONGLE = "192.168.3.65:1234"

# AIS dongle: id of a dongle dedicated to ship-traffic reception
# (161.975/162.025 MHz, Danube vessels -- see noaa_receiver/ais.py). AIS
# needs a continuously listening receiver, so a dongle pinned here is
# excluded from satellite tracking and never becomes the primary. The
# generic R820T (serial 77771111153705700) is rtl_tcp 192.168.3.65:1235.
# None = off.
AIS_DONGLE = "192.168.3.65:1235"

# Dongles registered at startup when no persisted list exists yet (the
# dashboard's "Add dongle" persists runtime additions in station.db's
# dongles table; that table then wins over this default). One (host,
# port) per rtl_tcp server published by the prawntenna manager.
DEFAULT_DONGLES = [
    ("192.168.3.65", 1234),   # NESDR SMArt v5 (serial 48263793, primary)
    ("192.168.3.65", 1235),   # generic R820T (serial 77771111153705700, AIS)
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
SDR_DONGLE_CORRECTIONS = {
    # Old generic R820T (now the AIS dongle, USB device 1 / "usb:1"):
    # never confirms PLL lock, but the crystal error is a
    # smooth +72..82 ppm HIGH across all bands (LO tunes high -> correction
    # negative). Measured 2026-10-06, four independent methods agree:
    #   - rtl_test -p: cumulative PPM 82 / 72 (sample clock = PLL reference)
    #   - 103.0 MHz: FM stereo pilot 18998.05 Hz with -8200 applied -> +8198 Hz
    #   - 144.8 MHz: local carriers line up with the 144.6929/144.754/144.8761
    #     survey (that survey reported true RF minus the LO error)
    #   - 437.55 MHz: 15:08 ISS ridges -22.5..-28.7 kHz raw fit +34.8 kHz
    #     with approach Doppler +6..+12 kHz
    # The earlier +21.6 kHz VHF3 value came from an R820T spur at bin 302
    # misread as the satellite: with +21600 applied, neither dongle showed
    # any APT ridge anywhere in the +/-120 kHz window during the 18:00 and
    # 19:38 NOAA 15 passes (58 deg max elevation).
    "192.168.3.65:1235": {
        "freqs": {
            137912500: -10977,  # Meteor LRPT band (79.6 ppm x 137.91 MHz)
            437550000: -34827,  # ISS (Zarya) (79.6 ppm x 437.55 MHz)
        },
        "fm_band": -8200,  # VHF2: tunes ~8 kHz HIGH (carriers 6-11 kHz below nominal, measured on 89.7/93.0/95.0/99.6/103.0/105.0)
        # ppm fallback, correctly signed since 2026-10-11: the old +80
        # entry contradicted this dongle's OWN measured values
        # (-10977 Hz @ 137.9 MHz / -34827 Hz @ 437.55 MHz, both
        # -79.6 ppm) and mistuned the 162 MHz AIS band by +26 kHz.
        # -80 ppm follows the measurements' trend (-12960 Hz at the
        # AIS band); a direct measurement on prawntenna2 would still
        # be better than the fallback.
        "ppm": -80,
    },
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
            137912500: 0,   # Meteor-M 2-3 / 2-4 LRPT
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
SDR_DONGLE_GAIN = {
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
