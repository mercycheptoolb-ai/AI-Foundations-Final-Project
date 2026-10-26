"""The two agents (intake, triage), the code checks around them, and token accounting."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.usage import UsageLimits

from . import store
from .config import DATA, MODEL_NAME, PRICES, PROMPTS, build_model
from .growth import FOLLOW_UP_DAYS, REFERRAL_TEXT, Assessment
from .models import TriageNote, VisitRecord

MAX_NOTE_CHARS = 1500
INTAKE_LIMITS = UsageLimits(request_limit=3, total_tokens_limit=12_000)
TRIAGE_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=4, total_tokens_limit=30_000)

# Defense layers (same three as HW6). The app always runs with all of them; evals/ switches them off one by one.
#   code   - sanitizer redacts instruction-like text and invisible characters; numbers not in the note are dropped
#   design - the WHO rules decide the referral; an output validator sends back any note that disagrees
#   prompt - the "Security rules" / "Safety rules" sections of the prompts
LAYERS = ("code", "design", "prompt")
ALL = frozenset(LAYERS)
SECURITY_HEADERS = ("# Security rules", "# Safety rules")

# Prompt caching. "on": a fixed cache key per agent routes repeat calls to the same OpenAI prompt cache, so the
# long static instructions (prompt + counselling guide) are billed at the cached-input rate after the first call.
# "off": no key, and a random id as the first tokens of the instructions so no two requests share a prefix.
CACHE_KEYS = {"intake": "kukua-intake", "triage": "kukua-triage"}

_model = None


def model():
    global _model
    if _model is None:
        _model = build_model()
    return _model


# ---------------------------------------------------------------- code-layer input checks

INVISIBLE = re.compile(r"[­᠎​-‏‪-‮⁠-⁤⁪-⁯﻿]")
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|your|these)? ?(instructions|rules|prompt)",
    r"disregard (the |your |all )?(rules|instructions|guidelines)",
    r"(system|developer) prompt", r"you are now", r"new instructions",
    r"(mark|classify|label|record) (him|her|the child|this child|it)? ?(as )?(healthy|normal|fine)",
    r"(do not|don't|dont) (refer|send)", r"no referral", r"override",
    r"puuza (maagizo|sheria)",  # Swahili: "ignore the instructions/rules"
]
INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.I)


@dataclass
class CleanNote:
    text: str
    removed: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)


def clean_note(raw: str, redact: bool = True) -> CleanNote:
    removed = []
    text = unicodedata.normalize("NFKC", raw or "")
    text, n = INVISIBLE.subn("", text)
    if n:
        removed.append(f"{n} invisible unicode character(s)")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    if len(text) > MAX_NOTE_CHARS:
        removed.append(f"{len(text) - MAX_NOTE_CHARS} characters beyond the {MAX_NOTE_CHARS}-character limit")
        text = text[:MAX_NOTE_CHARS]
    text = text.replace("</visit_note>", "")  # the note cannot close its own delimiter
    suspicious = sorted({m.group(0).strip() for m in INJECTION_RE.finditer(text)})
    if redact and suspicious:
        text = INJECTION_RE.sub("[instruction removed]", text)
        removed.append(f"{len(suspicious)} instruction-like phrase(s)")
    return CleanNote(text.strip(), removed, suspicious)


# Language check (code): common Swahili words. Used to fix the intake's language field and to make sure the
# caregiver message is actually in the caregiver's language.
SWAHILI_RE = re.compile(r"\b(na|ya|wa|za|kwa|ni|mtoto|msichana|mvulana|miezi|miaka|uzito|urefu|tafadhali|chakula|siku|"
                        r"mpe|yake|wake|leo|kila|lishe|asante|endelea|kliniki|hana|ana|amelala|amesimama)\b", re.I)


def looks_swahili(text: str, min_words: int = 4) -> bool:
    return len(SWAHILI_RE.findall(text or "")) >= min_words


NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
NUMERIC_FIELDS = ("age_months", "weight_kg", "length_cm", "muac_cm")
# How a stated number may legitimately map to each field (unit conversions the prompt allows).
CONVERSIONS = {
    "age_months": (1, 12, 1 / 4.345),
    "weight_kg": (1, 1 / 1000),
    "length_cm": (1, 1 / 10, 100),
    "muac_cm": (1, 1 / 10),
}


def ungrounded_numbers(record: VisitRecord, note: str) -> list[str]:
    """Numeric fields whose value does not appear in the note (allowing unit conversions).

    Guards against the intake agent inventing a measurement. Age is skipped when the note has a date
    (age computed from a date of birth).
    """
    stated = [float(n.replace(",", ".")) for n in NUM_RE.findall(note)]
    # "2 years 3 months" -> 27: allow years*12 + months from neighbouring numbers
    combos = [a * 12 + b for a, b in zip(stated, stated[1:]) if a <= 5 and b < 12]
    has_date = bool(re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}|born|dob|alizaliwa", note, re.I))
    bad = []
    for name in NUMERIC_FIELDS:
        value = getattr(record, name)
        if value is None or (name == "age_months" and has_date):
            continue
        candidates = [n * k for n in stated for k in CONVERSIONS[name]] + (combos if name == "age_months" else [])
        if not any(abs(c - value) <= max(0.051, 0.01 * value) for c in candidates):
            bad.append(name)
    return bad


# ---------------------------------------------------------------- usage + trace

def usage_dict(agent: str, usage, seconds: float) -> dict[str, Any]:
    cached = usage.cache_read_tokens
    uncached = usage.input_tokens - cached
    cost = (uncached * PRICES["input"] + cached * PRICES["cached_input"] + usage.output_tokens * PRICES["output"]) / 1e6
    return {
        "agent": agent, "model": MODEL_NAME, "requests": usage.requests, "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens, "cached_input_tokens": cached, "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.details.get("reasoning_tokens", 0), "cost_usd": round(cost, 6),
        "seconds": round(seconds, 2),
    }


def tool_calls(messages) -> list[dict]:
    return [{"tool": p.tool_name, "args": p.args or {}} for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolCallPart) and not p.tool_name.startswith("final_result")]


def prompt_text(name: str, prompt_layer: bool) -> str:
    """prompts/<name>.md, with its security/safety section cut out when the prompt layer is off."""
    text = (PROMPTS / f"{name}.md").read_text()
    if prompt_layer:
        return text
    for header in SECURITY_HEADERS:
        if header in text:
            before, after = text.split(header, 1)
            nxt = re.search(r"^# ", after, re.M)
            text = before + (after[nxt.start():] if nxt else "")
    return text


def _instructions(static: str, cache: str):
    if cache == "on":
        return static
    return lambda: f"Request id: {uuid4().hex}\n\n{static}"


def _settings(agent: str, cache: str) -> OpenAIResponsesModelSettings:
    return {"openai_prompt_cache_key": CACHE_KEYS[agent]} if cache == "on" else {}


# ---------------------------------------------------------------- intake agent

_intake_agents: dict[tuple, Agent] = {}


def intake_agent(defenses: frozenset[str] = ALL, cache: str = "on") -> Agent:
    key = ("prompt" in defenses, cache)
    if key not in _intake_agents:
        _intake_agents[key] = Agent(
            output_type=VisitRecord,
            instructions=_instructions(prompt_text("intake", "prompt" in defenses), cache),
            model_settings=_settings("intake", cache),
            retries=1,
        )
    return _intake_agents[key]


async def run_intake(note: str, image: tuple[bytes, str] | None = None, *, defenses: frozenset[str] = ALL,
                     cache: str = "on", log: bool = True, llm=None) -> dict[str, Any]:
    code = "code" in defenses
    clean = clean_note(note, redact=True) if code else CleanNote((note or "").strip())
    if not clean.text and not image:
        raise ValueError("Type a visit note or attach a photo of the growth card.")
    parts: list[Any] = [
        f"Today's date: {date.today().isoformat()}\n"
        f"<visit_note>\n{clean.text or '(no text - read the attached photo)'}\n</visit_note>"
    ]
    if image:
        # "high" detail: at the default resolution, handwritten "100" was misread as "10" in 4 of 8 runs (evals/photo_test.py)
        parts.append(BinaryContent(data=image[0], media_type=image[1], vendor_metadata={"detail": "high"}))
    t0 = time.perf_counter()
    result = await intake_agent(defenses, cache).run(parts, model=llm or model(), usage_limits=INTAKE_LIMITS)
    usage = usage_dict("intake", result.usage, time.perf_counter() - t0)
    if log:
        store.log_usage(usage)

    record = result.output
    warnings = []
    if image:
        warnings.append("Values were read from a photo: check every number against the card before assessing.")
    elif code:
        if looks_swahili(clean.text) and record.caregiver_language != "Swahili":
            warnings.append("The note is in Swahili, so the caregiver language was set to Swahili.")
            record.caregiver_language = "Swahili"
        for name in ungrounded_numbers(record, clean.text):
            warnings.append(f"{name} = {getattr(record, name)} was not in the note, so it was removed. Enter it by hand.")
            setattr(record, name, None)
        if record.child_id and record.child_id.strip().lower() not in clean.text.lower():
            warnings.append(f"Child ID {record.child_id} was not in the note, so it was removed. Look the child up below.")
            record.child_id = None
    missing = [f for f in ("sex", "age_months", "weight_kg") if getattr(record, f) is None]
    return {
        "record": record.model_dump(),
        "missing_required": missing,
        "warnings": warnings,
        "sanitizer": {"removed": clean.removed, "suspicious": clean.suspicious},
        "usage": usage,
    }


# ---------------------------------------------------------------- triage agent

@dataclass
class TriageDeps:
    record: dict
    assessment: Assessment
    history: list[dict]
    guard_events: list[str] = field(default_factory=list)


_BENCH = json.loads((DATA / "kenya_anthropometry.json").read_text())
_AGE_BANDS = [(0, 6, "0–5"), (6, 12, "6–11"), (12, 24, "12–23"), (24, 36, "24–35"), (36, 48, "36–47"), (48, 61, "48–59")]


def get_child_history(ctx: RunContext[TriageDeps]) -> list[dict]:
    """Earlier visits for this child, oldest first (empty if this is the first visit)."""
    return ctx.deps.history


def get_kenya_benchmark(ctx: RunContext[TriageDeps]) -> dict:
    """Kenya national survey figures (KDHS 2022, via WHO GHO) for this child's age band and sex."""
    a = ctx.deps.assessment
    band = next(label for lo, hi, label in _AGE_BANDS if lo <= a.age_months < hi)
    sex_label = "Female" if a.sex == "female" else "Male"
    out: dict[str, Any] = {"source": "WHO GHO, Kenya national survey 2022 (KDHS)", "age_band_months": band}
    for key in ("stunting", "wasting", "underweight", "overweight"):
        ind = _BENCH["survey"][key]
        year = "2022"
        b = ind["breakdowns"].get(year, {})
        nat = next((p["value"] for p in ind["national"] if str(p["year"]) == year), None)
        age = next((i["value"] for i in b.get("AGEGROUP", {}).get("items", []) if i["label"] == band), None)
        sex = next((i["value"] for i in b.get("SEX", {}).get("items", []) if i["label"] == sex_label), None)
        poorest = next((i["value"] for i in b.get("WEALTHQUINTILE", {}).get("items", []) if i["label"] == "Poorest"), None)
        out[key] = {"national_pct": nat, f"age_{band}_months_pct": age, f"{sex_label.lower()}_pct": sex,
                    "poorest_households_pct": poorest}
    return out


REASSURANCE_RE = re.compile(r"\b(healthy|growing well|no problem|nothing to worry|all is well|yuko salama|anakua vizuri)\b", re.I)


def enforce_rules(ctx: RunContext[TriageDeps], note: TriageNote) -> TriageNote:
    """Design layer: the model may explain the classification but never change it."""
    a = ctx.deps.assessment
    if note.referral != a.referral:
        ctx.deps.guard_events.append(f"Triage agent proposed referral '{note.referral}'; rules say '{a.referral}'. Sent back.")
        raise ModelRetry(f"referral must be exactly '{a.referral}' (set by the WHO rules). Rewrite the note to match it.")
    if note.follow_up_in_days > a.follow_up_days:
        ctx.deps.guard_events.append(f"Follow-up {note.follow_up_in_days} d exceeded the rule maximum {a.follow_up_days} d; capped.")
        note.follow_up_in_days = a.follow_up_days
    want_sw = ctx.deps.record.get("caregiver_language") == "Swahili"
    if want_sw != looks_swahili(note.caregiver_message):
        lang = "Swahili" if want_sw else "English"
        ctx.deps.guard_events.append(f"Caregiver message was not in {lang}. Sent back.")
        raise ModelRetry(f"caregiver_message must be written in {lang}, the caregiver's language.")
    if a.status in ("severe", "moderate") and REASSURANCE_RE.search(note.summary_for_worker + " " + note.caregiver_message):
        ctx.deps.guard_events.append("Triage agent described a malnourished child as healthy. Sent back.")
        raise ModelRetry("The child is not healthy according to the assessment. Do not describe them as healthy or growing well.")
    return note


_triage_agents: dict[tuple, Agent] = {}


def triage_agent(defenses: frozenset[str] = ALL, cache: str = "on") -> Agent:
    key = ("prompt" in defenses, "design" in defenses, cache)
    if key not in _triage_agents:
        agent = Agent(
            deps_type=TriageDeps,
            output_type=TriageNote,
            instructions=_instructions(prompt_text("triage", "prompt" in defenses), cache),
            model_settings=_settings("triage", cache),
            tools=[get_child_history, get_kenya_benchmark],
            retries=2,
        )
        if "design" in defenses:
            agent.output_validator(enforce_rules)
        _triage_agents[key] = agent
    return _triage_agents[key]


def fallback_note(a: Assessment, language: str) -> TriageNote:
    """Template note used when the agent fails or the budget is exhausted: the rules still stand."""
    msg = {
        "English": "Thank you for bringing your child today. Please follow the health worker's advice and come back on the agreed date.",
        "Swahili": "Asante kwa kumleta mtoto leo. Tafadhali fuata ushauri wa mhudumu wa afya na urudi siku mliyokubaliana.",
    }[language]
    return TriageNote(
        summary_for_worker="; ".join(f.label for f in a.findings) or "No growth problem found.",
        referral=a.referral, next_steps=[REFERRAL_TEXT[a.referral]], caregiver_message=msg,
        national_context="(not available - the AI note could not be generated)",
        follow_up_in_days=FOLLOW_UP_DAYS[a.referral])


async def run_triage(record: dict, assessment: Assessment, history: list[dict], *, defenses: frozenset[str] = ALL,
                     cache: str = "on", log: bool = True, llm=None) -> dict[str, Any]:
    """llm: optional model override (evals/guard_test.py passes a scripted, misbehaving model)."""
    deps = TriageDeps(record=record, assessment=assessment, history=history)
    note_field = record.get("notes")
    if note_field and "code" in defenses:
        c = clean_note(note_field)
        note_field = c.text
        if c.suspicious:
            deps.guard_events.append(f"Notes contained instruction-like text {c.suspicious}; removed before the model saw them.")
    child = {"name": record.get("child_name"), "caregiver_language": record.get("caregiver_language", "English"),
             "danger_signs": record.get("danger_signs", [])}
    if "design" in defenses:
        payload = {"child": child, "assessment": assessment.model_dump(exclude={"referral_text"}),
                   "rule_based_referral": assessment.referral, "rule_based_follow_up_days": assessment.follow_up_days}
        prompt = f"Assessment (computed by code, authoritative):\n```json\n{json.dumps(payload, indent=1)}\n```\n"
    else:
        # Naive design for comparison: the model gets the measurements and z-scores and decides the referral itself.
        payload = {"child": child, "measurements": {k: record.get(k) for k in (
                       "sex", "age_months", "weight_kg", "length_cm", "measured_lying", "muac_cm", "oedema")},
                   "zscores": [z.model_dump() for z in assessment.zscores]}
        prompt = (f"Visit:\n```json\n{json.dumps(payload, indent=1)}\n```\n"
                  "No referral level has been computed for this visit: decide it yourself from the WHO cut-offs.\n")
    prompt += f"<worker_notes>\n{note_field or '(none)'}\n</worker_notes>"
    t0 = time.perf_counter()
    used_fallback = False
    try:
        result = await triage_agent(defenses, cache).run(prompt, deps=deps, model=llm or model(), usage_limits=TRIAGE_LIMITS)
        note, usage_obj, calls = result.output, result.usage, tool_calls(result.all_messages())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
        deps.guard_events.append(f"Triage agent failed ({type(exc).__name__}); used the rule-based template note.")
        note, usage_obj, calls, used_fallback = fallback_note(assessment, record.get("caregiver_language", "English")), None, [], True
    usage = usage_dict("triage", usage_obj, time.perf_counter() - t0) if usage_obj else {
        "agent": "triage", "model": MODEL_NAME, "requests": 0, "tool_calls": 0, "input_tokens": 0,
        "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0,
        "seconds": round(time.perf_counter() - t0, 2)}
    if log:
        store.log_usage(usage)
    # With the design layer the WHO rules always decide the referral. Without it, the pipeline trusts the model.
    final_referral = assessment.referral if "design" in defenses else note.referral
    return {"note": note.model_dump(), "final_referral": final_referral, "tool_calls": calls,
            "guard_events": deps.guard_events, "used_fallback": used_fallback, "usage": usage}
