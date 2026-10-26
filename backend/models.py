"""Structured outputs of the two agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .growth import Referral

DangerSign = Literal["unable to drink or breastfeed", "vomits everything", "convulsions", "lethargic or unconscious"]


class VisitRecord(BaseModel):
    """Intake agent output: one growth-monitoring visit, exactly as the health worker reported it."""

    child_id: str | None = Field(
        description="The child's ID exactly as written (e.g. KK-0001, a card or booklet number). null if no ID is written.")
    child_name: str | None = Field(description="Child's name as written. null if not given.")
    sex: Literal["male", "female"] | None = Field(description="null if not stated")
    age_months: float | None = Field(
        description="Age in months. Convert years (x12) or weeks (/4.345). If a date of birth is given, compute "
                    "from today's date. null if not stated.")
    weight_kg: float | None = Field(description="Weight in kg. Convert grams (/1000). null if not stated.")
    length_cm: float | None = Field(description="Length or height in cm. Convert mm (/10) or m (x100). null if not stated.")
    measured_lying: bool | None = Field(description="true = lying (recumbent length), false = standing height, null if not stated.")
    muac_cm: float | None = Field(description="Mid-upper arm circumference in cm. Convert mm (/10). null if not stated.")
    oedema: bool | None = Field(description="Bilateral pitting oedema (swelling of both feet). null if not mentioned.")
    danger_signs: list[DangerSign] = Field(default_factory=list, description="Only signs the note explicitly reports.")
    caregiver_language: Literal["English", "Swahili"] = Field(
        default="English", description="Language for the caregiver message: Swahili if the note is in Swahili or asks for it.")
    notes: str | None = Field(description="Other observations (illness, feeding, appetite) in one short sentence. null if none.")


class TriageNote(BaseModel):
    """Triage agent output: explanation, next steps and caregiver message for one assessed visit."""

    summary_for_worker: str = Field(description="2-3 plain sentences for the health worker: what the numbers mean.")
    referral: Referral = Field(description="Must be exactly the referral level given by the WHO rules.")
    next_steps: list[str] = Field(description="3-5 short, concrete actions for the health worker, most urgent first.")
    caregiver_message: str = Field(description="Warm, simple message to read to the caregiver, in caregiver_language, under 120 words.")
    national_context: str = Field(description="One sentence comparing this child's situation with Kenya's national figures, from the benchmark tool.")
    follow_up_in_days: int = Field(description="Days until the next visit. Never later than the rule-based follow-up.")
