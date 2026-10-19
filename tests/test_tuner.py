"""Prawntuner length math, hill-climb decisions and role restoration."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault("PRAWN_DB_FILE",
                      os.path.join(tempfile.mkdtemp(), "station.db"))

from noaa_receiver.tuner import (apply_measurement, antenna_length_cm,
                                 hill_climb, initial_lengths,
                                 role_restore_plan)


for frequency_mhz in (1090.0, 137.9, 162.0, 466.0):
    frequency_hz = frequency_mhz * 1e6
    expected = 7125.0 / frequency_mhz
    assert abs(antenna_length_cm(frequency_hz, "whip") - expected) < 1e-9
    assert abs(antenna_length_cm(frequency_hz, "vdipole") - expected) < 1e-9
    lengths = initial_lengths(frequency_hz, "whip")
    assert lengths["start_cm"] > lengths["nominal_cm"]
    assert abs(lengths["start_cm"] / lengths["nominal_cm"] - 1.05) < 0.001
print("1. quarter-wave whip and per-leg V-dipole lengths: ok")


def point(length, ratio_db, decodes=0, valid=True, reason=None):
    return {"length_cm": length, "ratio_db": ratio_db,
            "peak_dbfs": ratio_db - 40, "decodes": decodes,
            "valid": valid, "invalid_reason": reason}


first = point(50.0, 8.0)
decision = hill_climb([first])
assert decision["status"] == "awaiting-user"
assert decision["direction"] == 1 and decision["step_cm"] == 2.0
assert "LENGTHEN by 2" in decision["instruction"]
improved = point(52.0, 9.0, 1)
decision = hill_climb([first, improved])
assert decision["direction"] == 1 and decision["step_cm"] == 2.0
print("2. improving measurements continue the coarse step: ok")

noisy = hill_climb([point(50.0, 8.0), point(50.0, 8.1)])
assert noisy["status"] == "awaiting-user"
assert "Hold" in noisy["instruction"]
print("3. same-length noise asks for a repeat measurement: ok")

converged = hill_climb([
    point(50.0, 12.0, 4),
    point(49.5, 10.0, 2),
    point(50.5, 11.0, 3),
])
assert converged["status"] == "converged"
assert converged["converged_length_cm"] == 50.0
assert "Tuned length" in converged["instruction"]
print("4. worse half-centimeter readings on both sides converge: ok")

session = {"status": "measuring", "retry_only": False, "step_cm": 2.0,
           "direction": 1, "points": [], "converged_length_cm": None}
apply_measurement(session, point(50.0, 8.0, valid=False,
                                 reason="traffic disappeared"))
assert session["status"] == "awaiting-user" and session["retry_only"]
assert "INVALID" in session["instruction"]
session["status"] = "measuring"
apply_measurement(session, point(50.0, 8.0))
assert session["status"] == "awaiting-user" and not session["retry_only"]
assert "LENGTHEN" in session["instruction"]
print("5. invalid and valid measurement state transitions: ok")

assert role_restore_plan("manual", "adsb") == [("adsb", False)]
assert role_restore_plan("ais", None) == [("ais", True)]
assert role_restore_plan("sat", "aprs") == [("aprs", False)]
assert role_restore_plan("adsb", "adsb") == []
print("6. temporary role restore sequence: ok")

print("ALL PRAWNTUNER TESTS PASSED", flush=True)
os._exit(0)