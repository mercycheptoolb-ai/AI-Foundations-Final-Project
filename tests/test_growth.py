"""Check the calculator against values printed in the WHO expanded z-score tables.

Run: .venv/bin/python -m pytest tests -q   (or: .venv/bin/python tests/test_growth.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.growth import MeasurementError, _lms, assess, zscore  # noqa: E402


def z_at(indicator, sex, x, y, restricted=True):
    return zscore(y, *_lms(indicator, sex, x), restricted=restricted)


def test_wfa_girl_548_days_matches_who_sd_columns():
    # WHO wfa-girls, day 548: SD0 10.232, SD2neg 8.061, SD3neg 7.194, SD4neg 6.326, SD3 15.091
    assert z_at("wfa", "female", 548, 10.232) == 0.0
    assert z_at("wfa", "female", 548, 8.061) == -2.0
    assert z_at("wfa", "female", 548, 7.194) == -3.0
    assert z_at("wfa", "female", 548, 6.326) == -4.0  # restricted tail
    assert z_at("wfa", "female", 548, 15.091) == 3.0


def test_lhfa_boy_913_days_unrestricted():
    # WHO lhfa-boys, day 913: SD2neg 85.12, SD3neg 81.714, SD4neg 78.309
    assert z_at("lhfa", "male", 913, 85.12, restricted=False) == -2.0
    assert z_at("lhfa", "male", 913, 78.309, restricted=False) == -4.0


def test_wfl_and_wfh_tables():
    # wfl-girls 74.0 cm: SD3neg 6.927 ; wfh-boys 95.3 cm: SD2neg 12.08, SD2 16.686
    assert z_at("wfl", "female", 74.0, 6.927) == -3.0
    assert z_at("wfh", "male", 95.3, 12.08) == -2.0
    assert z_at("wfh", "male", 95.3, 16.686) == 2.0


def test_muac_for_age():
    # acfa-girls day 730: SD2neg 12.675
    assert z_at("acfa", "female", 730, 12.675) == -2.0


def test_interpolates_between_length_rows():
    lo, hi = _lms("wfh", "male", 95.3), _lms("wfh", "male", 95.4)
    mid = _lms("wfh", "male", 95.35)
    assert lo[1] < mid[1] < hi[1]


def test_severe_acute_malnutrition_by_muac():
    a = assess(sex="female", age_days=548, weight_kg=8.0, length_cm=78.0, muac_cm=11.2)
    assert a.status == "severe" and a.referral == "urgent_referral"
    assert any(f.code == "SAM_MUAC" for f in a.findings)


def test_oedema_is_always_severe_and_blanks_weight_scores():
    a = assess(sex="male", age_days=700, weight_kg=10.5, length_cm=80, muac_cm=13.5, oedema=True)
    assert a.referral == "urgent_referral"
    assert next(s for s in a.zscores if s.indicator == "wfa").z is None


def test_moderate_wasting_and_stunting():
    # boy 30 months, 84 cm standing (HAZ -2.33), 9.4 kg (WHZ -2.47)
    a = assess(sex="male", age_days=913, weight_kg=9.4, length_cm=84.0, measured_lying=False, muac_cm=12.8)
    codes = {f.code for f in a.findings}
    assert "MAM_WHZ" in codes and "STUNTED" in codes
    assert a.referral == "refer_nutrition_program"


def test_healthy_child_routine():
    a = assess(sex="female", age_days=548, weight_kg=10.2, length_cm=80.7, muac_cm=14.5)
    assert a.status == "normal" and a.referral == "routine_growth_monitoring"


def test_position_correction():
    lying_older = assess(sex="male", age_days=913, weight_kg=13, length_cm=92.0, measured_lying=True)
    standing_younger = assess(sex="male", age_days=400, weight_kg=9, length_cm=74.0, measured_lying=False)
    assert lying_older.length_cm_used == 91.3
    assert standing_younger.length_cm_used == 74.7


def test_rejects_implausible_inputs():
    for kwargs in ({"weight_kg": 85}, {"weight_kg": 9, "length_cm": 300}, {"weight_kg": 9, "muac_cm": 2}):
        try:
            assess(sex="male", age_days=400, **kwargs)
        except MeasurementError:
            continue
        raise AssertionError(f"accepted {kwargs}")
    try:
        assess(sex="male", age_days=2500, weight_kg=15)
    except MeasurementError:
        pass
    else:
        raise AssertionError("accepted a 6-year-old")


def test_flags_biologically_implausible_z():
    # 74 cm and 20 kg is a typo-level combination: flagged, not classified as obese
    a = assess(sex="female", age_days=548, weight_kg=20.0, length_cm=74.0)
    assert a.status in ("check_measurement", "moderate", "severe") or any(s.flagged for s in a.zscores)
    assert any(s.flagged for s in a.zscores)


def test_growth_faltering_from_history():
    hist = [{"age_days": 500, "weight_kg": 9.6}]
    a = assess(sex="female", age_days=548, weight_kg=9.5, length_cm=79.0, muac_cm=14.0, history=hist)
    assert any(f.code == "GROWTH_FALTERING" for f in a.findings)
    assert a.referral == "counsel_and_follow_up"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
