"""Deterministic WHO growth calculator and nutrition classification.

No language model touches anything in this file. The agents read its output; they never compute
or override a z-score or a referral level.

Methods follow the WHO Child Growth Standards (2006) and the WHO Anthro software:
- z = ((y/M)^L - 1) / (L*S), with the "restricted" tail adjustment beyond ±3 SD for every
  indicator except length/height-for-age.
- Recumbent length under 24 months, standing height from 24 months; a 0.7 cm correction when
  the other position was used.
- Biologically implausible z-scores are flagged and left out of the classification.
Classification cut-offs follow WHO/UNICEF guidance on acute malnutrition (WHZ, MUAC, oedema) and
the WHO definitions of stunting, underweight and overweight.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DATA = Path(__file__).resolve().parents[1] / "data"
_TABLES = json.loads((DATA / "who_growth_standards.json").read_text())["tables"]

DAYS_PER_MONTH = 30.4375
MAX_AGE_DAYS = 1856  # 60 completed months
POSITION_CORRECTION_CM = 0.7

Sex = Literal["male", "female"]
Indicator = Literal["wfa", "lhfa", "wfl", "wfh", "acfa"]
INDICATOR_NAMES = {
    "wfa": "Weight-for-age (WAZ)",
    "lhfa": "Length/height-for-age (HAZ)",
    "wfl": "Weight-for-length (WHZ)",
    "wfh": "Weight-for-height (WHZ)",
    "acfa": "MUAC-for-age (MUACZ)",
}
# WHO flag limits for implausible values: (low, high).
FLAG_LIMITS = {"wfa": (-6, 5), "lhfa": (-6, 6), "wfl": (-5, 5), "wfh": (-5, 5), "acfa": (-5, 5)}
# Plausible raw measurement ranges; anything outside is almost surely a typo or a unit mix-up.
INPUT_LIMITS = {"weight_kg": (0.9, 58.0), "length_cm": (38.0, 130.0), "muac_cm": (7.0, 26.0)}


class MeasurementError(ValueError):
    """A measurement is missing or outside any plausible range."""


class ZScore(BaseModel):
    indicator: Indicator
    name: str
    z: float | None = Field(description="null when not computable (out of table range, or oedema)")
    flagged: bool = Field(default=False, description="True when |z| is outside WHO plausibility limits")
    note: str | None = None


class Finding(BaseModel):
    code: str
    label: str
    severity: Literal["severe", "moderate", "at_risk", "info"]


Referral = Literal["urgent_referral", "refer_nutrition_program", "counsel_and_follow_up", "routine_growth_monitoring"]
Status = Literal["severe", "moderate", "at_risk", "normal", "check_measurement"]

REFERRAL_TEXT: dict[str, str] = {
    "urgent_referral": "Refer TODAY to the health facility (severe acute malnutrition or danger sign).",
    "refer_nutrition_program": "Refer to the nutrition programme (supplementary feeding / clinical review).",
    "counsel_and_follow_up": "Counsel the caregiver and re-measure at a follow-up visit.",
    "routine_growth_monitoring": "Growing well. Continue routine monthly growth monitoring.",
}
FOLLOW_UP_DAYS = {"urgent_referral": 0, "refer_nutrition_program": 14, "counsel_and_follow_up": 30,
                  "routine_growth_monitoring": 30}


class Assessment(BaseModel):
    sex: Sex
    age_days: int
    age_months: float
    weight_kg: float
    length_cm_used: float | None = Field(description="length/height after position correction")
    position_used: Literal["recumbent length", "standing height"] | None
    muac_cm: float | None
    oedema: bool
    zscores: list[ZScore]
    findings: list[Finding]
    status: Status
    referral: Referral
    referral_text: str
    follow_up_days: int


def age_days_from_months(months: float) -> int:
    return int(round(months * DAYS_PER_MONTH))


def _lms(indicator: str, sex: str, x: float) -> tuple[float, float, float] | None:
    """L, M, S at x (age in days, or length/height in cm), linearly interpolated; None if out of range."""
    rows = _TABLES[indicator][sex]
    x0, step = rows[0][0], rows[1][0] - rows[0][0]
    pos = (x - x0) / step
    if pos < -1e-9 or pos > len(rows) - 1 + 1e-9:
        return None
    i = min(int(math.floor(pos + 1e-9)), len(rows) - 2)
    frac = max(0.0, pos - i)
    a, b = rows[i], rows[i + 1]
    return tuple(a[k] + (b[k] - a[k]) * frac for k in (1, 2, 3))  # type: ignore[return-value]


def _sd(L: float, M: float, S: float, k: float) -> float:
    return M * math.exp(S * k) if L == 0 else M * (1 + L * S * k) ** (1 / L)


def zscore(y: float, L: float, M: float, S: float, restricted: bool) -> float:
    z = math.log(y / M) / S if L == 0 else ((y / M) ** L - 1) / (S * L)
    if restricted and z > 3:
        sd3, sd2 = _sd(L, M, S, 3), _sd(L, M, S, 2)
        z = 3 + (y - sd3) / (sd3 - sd2)
    elif restricted and z < -3:
        sd3, sd2 = _sd(L, M, S, -3), _sd(L, M, S, -2)
        z = -3 + (y - sd3) / (sd2 - sd3)
    return round(z, 2)


def _score(indicator: Indicator, sex: str, x: float, y: float) -> ZScore:
    lms = _lms(indicator, sex, x)
    if lms is None:
        return ZScore(indicator=indicator, name=INDICATOR_NAMES[indicator], z=None, note="outside the WHO table range")
    z = zscore(y, *lms, restricted=indicator != "lhfa")
    lo, hi = FLAG_LIMITS[indicator]
    flagged = z < lo or z > hi
    return ZScore(indicator=indicator, name=INDICATOR_NAMES[indicator], z=z, flagged=flagged,
                  note="implausible - re-measure" if flagged else None)


def _check(name: str, value: float | None) -> None:
    if value is None:
        return
    lo, hi = INPUT_LIMITS[name]
    if not lo <= value <= hi:
        raise MeasurementError(f"{name.replace('_', ' ')} = {value} is outside the plausible range {lo}–{hi}. "
                               "Check the units and re-measure.")


def assess(*, sex: Sex, age_days: int, weight_kg: float, length_cm: float | None = None,
           measured_lying: bool | None = None, muac_cm: float | None = None, oedema: bool = False,
           danger_signs: list[str] | None = None, history: list[dict] | None = None) -> Assessment:
    """Compute z-scores and the rule-based classification for one visit."""
    if sex not in ("male", "female"):
        raise MeasurementError("sex must be male or female")
    if not 0 <= age_days <= MAX_AGE_DAYS:
        raise MeasurementError(f"age {age_days} days is outside 0–60 months; the WHO standards cover under-5s only.")
    _check("weight_kg", weight_kg)
    _check("length_cm", length_cm)
    _check("muac_cm", muac_cm)

    under2 = age_days < 731
    length_used, position = None, None
    if length_cm is not None:
        length_used = length_cm
        if under2 and measured_lying is False:
            length_used += POSITION_CORRECTION_CM
        elif not under2 and measured_lying is True:
            length_used -= POSITION_CORRECTION_CM
        length_used = round(length_used, 1)
        position = "recumbent length" if under2 else "standing height"

    scores: list[ZScore] = []
    wfa = _score("wfa", sex, age_days, weight_kg)
    if length_used is not None:
        hfa = _score("lhfa", sex, age_days, length_used)
        whz = _score("wfl" if under2 else "wfh", sex, length_used, weight_kg)
    else:
        hfa = whz = None
    if oedema:  # weight is inflated by fluid, so weight-based z-scores are not interpretable
        wfa = wfa.model_copy(update={"z": None, "note": "not computed: bilateral oedema"})
        if whz:
            whz = whz.model_copy(update={"z": None, "note": "not computed: bilateral oedema"})
    scores.append(wfa)
    if hfa:
        scores += [hfa, whz]  # type: ignore[list-item]
    muacz = _score("acfa", sex, age_days, muac_cm) if muac_cm is not None and age_days >= 91 else None
    if muacz:
        scores.append(muacz)

    def z(s: ZScore | None) -> float | None:
        return s.z if s and s.z is not None and not s.flagged else None

    waz_, haz_, whz_ = z(wfa), z(hfa), z(whz)
    age_months = age_days / DAYS_PER_MONTH
    muac_applies = muac_cm is not None and age_months >= 6

    findings: list[Finding] = []
    add = lambda code, label, sev: findings.append(Finding(code=code, label=label, severity=sev))  # noqa: E731

    for sign in danger_signs or []:
        add("DANGER_SIGN", f"General danger sign: {sign}", "severe")
    if oedema:
        add("SAM_OEDEMA", "Bilateral pitting oedema - severe acute malnutrition", "severe")
    if whz_ is not None and whz_ < -3:
        add("SAM_WHZ", "Severe wasting (WHZ < -3)", "severe")
    elif whz_ is not None and whz_ < -2:
        add("MAM_WHZ", "Moderate wasting (WHZ -3 to -2)", "moderate")
    if muac_applies and muac_cm < 11.5:
        add("SAM_MUAC", "MUAC below 11.5 cm (red) - severe acute malnutrition", "severe")
    elif muac_applies and muac_cm < 12.5:
        add("MAM_MUAC", "MUAC 11.5–12.4 cm (yellow) - moderate acute malnutrition", "moderate")
    if haz_ is not None and haz_ < -3:
        add("STUNTED_SEVERE", "Severely stunted (HAZ < -3)", "at_risk")
    elif haz_ is not None and haz_ < -2:
        add("STUNTED", "Stunted (HAZ -3 to -2)", "at_risk")
    if waz_ is not None and waz_ < -3:
        add("UNDERWEIGHT_SEVERE", "Severely underweight (WAZ < -3)", "moderate")
    elif waz_ is not None and waz_ < -2:
        add("UNDERWEIGHT", "Underweight (WAZ -3 to -2)", "at_risk")
    if whz_ is not None and whz_ > 3:
        add("OBESE", "Obese (WHZ > +3)", "at_risk")
    elif whz_ is not None and whz_ > 2:
        add("OVERWEIGHT", "Overweight (WHZ +2 to +3)", "at_risk")
    elif whz_ is not None and whz_ > 1:
        add("RISK_OVERWEIGHT", "Possible risk of overweight (WHZ > +1)", "info")
    if length_cm is None:
        add("NO_LENGTH", "Length/height not recorded - wasting and stunting could not be assessed", "info")
    if muac_cm is None and age_months >= 6:
        add("NO_MUAC", "MUAC not recorded", "info")
    if history and (trend := growth_trend(history, weight_kg, age_days)):
        findings.append(trend)
    if any(s.flagged for s in scores):
        add("IMPLAUSIBLE", "A z-score is outside WHO plausibility limits - re-measure before acting", "info")

    sev = {f.severity for f in findings}
    if "severe" in sev:
        status, referral = "severe", "urgent_referral"
    elif "moderate" in sev:
        status, referral = "moderate", "refer_nutrition_program"
    elif any(s.flagged for s in scores):
        status, referral = "check_measurement", "counsel_and_follow_up"
    elif "at_risk" in sev:
        status, referral = "at_risk", "counsel_and_follow_up"
    else:
        status, referral = "normal", "routine_growth_monitoring"

    return Assessment(
        sex=sex, age_days=age_days, age_months=round(age_months, 1), weight_kg=weight_kg,
        length_cm_used=length_used, position_used=position, muac_cm=muac_cm, oedema=oedema,
        zscores=scores, findings=findings, status=status, referral=referral,
        referral_text=REFERRAL_TEXT[referral], follow_up_days=FOLLOW_UP_DAYS[referral],
    )


def growth_trend(history: list[dict], current_weight: float, current_age_days: int) -> Finding | None:
    """Growth faltering: no weight gain since the last visit 21+ days ago (deterministic)."""
    earlier = [h for h in history if h.get("age_days") is not None and current_age_days - h["age_days"] >= 21]
    if not earlier:
        return None
    last = max(earlier, key=lambda h: h["age_days"])
    if current_weight <= last["weight_kg"]:
        return Finding(code="GROWTH_FALTERING",
                       label=f"No weight gain since last visit ({last['weight_kg']} kg → {current_weight} kg)",
                       severity="at_risk")
    return None
