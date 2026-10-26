"""Agent 3: the data guide chatbot. Answers questions about Kenya nutrition data using tools over two datasets:
Kenya DHS 2014/2022 by county and province (data/kenya_dhs_regions.json) and the WHO GHO national series
(data/kenya_anthropometry.json). A code guard rejects any answer containing a figure that no tool returned."""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from . import store
from .agents import clean_note, model, prompt_text, tool_calls, usage_dict
from .config import DATA

MAX_QUESTION_CHARS = 500
CHAT_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8, total_tokens_limit=40_000)

REGIONS = json.loads((DATA / "kenya_dhs_regions.json").read_text())
WHO = json.loads((DATA / "kenya_anthropometry.json").read_text())
IND = REGIONS["indicators"]
IndicatorKey = Literal["stunting", "severe_stunting", "wasting", "severe_wasting", "underweight", "child_overweight",
                       "women_thin", "women_overweight_or_obese", "women_obese", "women_mean_bmi", "women_short"]
WHO_KEY = {"stunting": "stunting", "wasting": "wasting", "severe_wasting": "severe_wasting", "underweight": "underweight",
           "child_overweight": "overweight"}
SOURCE_DHS = "Kenya DHS {year} (DHS Program)"
SOURCE_WHO = "WHO Global Health Observatory (Kenya national surveys)"


class ChatAnswer(BaseModel):
    answer: str = Field(description="The answer, 2-5 short sentences or a short list. Every figure must come from a tool result.")
    sources: list[str] = Field(description="Datasets used, e.g. 'Kenya DHS 2022 (DHS Program)'.")


@dataclass
class GuideDeps:
    seen_numbers: list[float] = field(default_factory=list)  # every number any tool returned this turn
    guard_events: list[str] = field(default_factory=list)


def _remember(ctx: RunContext[GuideDeps], result: Any) -> Any:
    ctx.deps.seen_numbers += [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", json.dumps(result))]
    return result


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


PLACES = sorted({(r["level"], r["name"]) for r in REGIONS["rows"]})


def _match_place(place: str) -> tuple[str, str] | None:
    want = _norm(place)
    if want in ("kenya", "national", "country", "nationally", ""):
        return ("national", "Kenya")
    by_norm = {_norm(n): (lvl, n) for lvl, n in PLACES}
    if want in by_norm:
        return by_norm[want]
    close = difflib.get_close_matches(want, by_norm, n=1, cutoff=0.75)
    return by_norm[close[0]] if close else None


def _value(ind: str, year: int, level: str, name: str) -> float | None:
    return next((r["value"] for r in REGIONS["rows"] if r["indicator"] == ind and r["year"] == year
                 and r["level"] == level and r["name"] == name), None)


def _unit(ind: str) -> str:
    return IND[ind]["unit"]


def list_data(ctx: RunContext[GuideDeps]) -> dict:
    """What indicators, places and years exist."""
    return _remember(ctx, {
        "indicators": {k: v["label"] for k, v in IND.items()},
        "regional_years": [2014, 2022],
        "levels": {"province": sorted({n for lvl, n in PLACES if lvl == "province"}),
                   "county": sorted({n for lvl, n in PLACES if lvl == "county"})},
        "national_series_from_WHO": {k: [p["year"] for p in v["national"]] for k, v in WHO["survey"].items()},
        "national_breakdowns_2022": ["sex", "residence", "wealth", "education", "age"],
    })


def find_value(ctx: RunContext[GuideDeps], indicator: IndicatorKey, place: str, year: Literal[2014, 2022] = 2022) -> dict:
    """One indicator for one county, former province, or 'Kenya', with the national figure and county rank."""
    match = _match_place(place)
    if not match:
        return {"error": f"No county or province called '{place}' in the data.", "hint": "Use list_data to see the place names."}
    level, name = match
    value = _value(indicator, year, level, name)
    out: dict[str, Any] = {"indicator": IND[indicator]["label"], "place": name, "level": level, "year": year,
                           "value": value, "unit": _unit(indicator), "source": SOURCE_DHS.format(year=year),
                           "national_value": _value(indicator, year, "national", "Kenya")}
    if level == "county" and value is not None:
        vals = sorted((r["value"] for r in REGIONS["rows"] if r["indicator"] == indicator and r["year"] == year
                       and r["level"] == "county"), reverse=True)
        out["rank_among_counties_highest_first"] = vals.index(value) + 1
        out["number_of_counties"] = len(vals)
    return _remember(ctx, out)


def rank_places(ctx: RunContext[GuideDeps], indicator: IndicatorKey, year: Literal[2014, 2022] = 2022,
                level: Literal["county", "province"] = "county", order: Literal["highest", "lowest"] = "highest",
                n: int = 5) -> dict:
    """The counties or former provinces with the highest or lowest value."""
    rows = [r for r in REGIONS["rows"] if r["indicator"] == indicator and r["year"] == year and r["level"] == level]
    rows.sort(key=lambda r: r["value"], reverse=order == "highest")
    return _remember(ctx, {"indicator": IND[indicator]["label"], "year": year, "level": level, "order": order,
                           "unit": _unit(indicator), "source": SOURCE_DHS.format(year=year),
                           "national_value": _value(indicator, year, "national", "Kenya"),
                           "places": [{"name": r["name"], "value": r["value"]} for r in rows[:max(1, min(n, 15))]]})


def compare_years(ctx: RunContext[GuideDeps], indicator: IndicatorKey, place: str = "Kenya") -> dict:
    """Change between the 2014 and 2022 Kenya DHS for a county, province or Kenya."""
    match = _match_place(place)
    if not match:
        return {"error": f"No county or province called '{place}' in the data."}
    level, name = match
    v14, v22 = _value(indicator, 2014, level, name), _value(indicator, 2022, level, name)
    change = round(v22 - v14, 1) if v14 is not None and v22 is not None else None
    return _remember(ctx, {"indicator": IND[indicator]["label"], "place": name, "unit": _unit(indicator),
                           "2014": v14, "2022": v22, "change_points": change,
                           "source": "Kenya DHS 2014 and 2022 (DHS Program)"})


def national_breakdown(ctx: RunContext[GuideDeps], indicator: Literal["stunting", "wasting", "severe_wasting", "underweight", "child_overweight"],
                       group: Literal["sex", "residence", "wealth", "education", "age"]) -> dict:
    """Kenya 2022 national figures for children by sex, urban/rural, wealth quintile, mother's education or age."""
    dim = {"sex": "SEX", "residence": "RESIDENCEAREATYPE", "wealth": "WEALTHQUINTILE", "education": "EDUCATIONLEVEL",
           "age": "AGEGROUP"}[group]
    b = WHO["survey"][WHO_KEY[indicator]]["breakdowns"].get("2022", {}).get(dim)
    if not b:
        return {"error": "No 2022 breakdown for that group."}
    return _remember(ctx, {"indicator": WHO["survey"][WHO_KEY[indicator]]["name"] + " among children under 5", "year": 2022,
                           "group": b["label"], "unit": "%", "source": SOURCE_WHO,
                           "values": [{"group": i["label"], "value": i["value"]} for i in b["items"]]})


def national_trend(ctx: RunContext[GuideDeps], indicator: Literal["stunting", "wasting", "severe_wasting", "underweight", "child_overweight"]) -> dict:
    """Kenya's national survey values for an indicator from 1987 to 2022."""
    s = WHO["survey"][WHO_KEY[indicator]]
    return _remember(ctx, {"indicator": s["name"] + " among children under 5", "unit": "%", "source": SOURCE_WHO,
                           "series": [{"year": p["year"], "value": p["value"]} for p in s["national"]]})


NUM_IN_TEXT = re.compile(r"(?<![\w-])\d+(?:\.\d+)?(?=\s*%|\s*per\s*cent|\s*percent|\s*kg/m)|(?<![\w-])\d+\.\d+")


def enforce_grounding(ctx: RunContext[GuideDeps], out: ChatAnswer) -> ChatAnswer:
    """Every percentage or decimal figure in the answer must be a number some tool returned this turn."""
    figures = [float(x) for x in NUM_IN_TEXT.findall(out.answer)]
    seen = set(ctx.deps.seen_numbers)
    # also allow simple arithmetic on returned numbers: differences ("4.1 points higher") and ratios ("1.9 times")
    derived = {round(abs(a - b), 1) for a in seen for b in seen} | {round(a / b, 1) for a in seen for b in seen if b > 0}
    unknown = [f for f in figures if not any(abs(f - s) <= 0.051 for s in seen | derived)]
    if unknown:
        ctx.deps.guard_events.append(f"Answer had figures no tool returned {unknown}. Sent back.")
        raise ModelRetry(f"These figures did not come from a tool result: {unknown}. Call a tool and use only its numbers.")
    return out


guide_agent = Agent(
    deps_type=GuideDeps,
    output_type=ChatAnswer,
    instructions=prompt_text("data_guide", True),
    model_settings={"openai_prompt_cache_key": "kukua-data-guide"},
    tools=[list_data, find_value, rank_places, compare_years, national_breakdown, national_trend],
    retries=2,
)
guide_agent.output_validator(enforce_grounding)


async def ask(question: str, history: list[dict], *, log: bool = True, llm=None) -> dict[str, Any]:
    clean = clean_note(question[:MAX_QUESTION_CHARS])
    if not clean.text:
        raise ValueError("Type a question about Kenya's nutrition data.")
    past = "\n".join(f"{h['role']}: {clean_note(str(h.get('text', ''))[:600]).text}" for h in history[-6:]
                     if h.get("role") in ("user", "assistant"))
    prompt = (f"<conversation_so_far>\n{past or '(none)'}\n</conversation_so_far>\n"
              f"<question>\n{clean.text}\n</question>")
    deps = GuideDeps()
    if clean.suspicious:
        deps.guard_events.append(f"Instruction-like text removed from the question: {clean.suspicious}")
    t0 = time.perf_counter()
    try:
        result = await guide_agent.run(prompt, deps=deps, model=llm or model(), usage_limits=CHAT_LIMITS)
        answer, usage_obj, calls = result.output, result.usage, tool_calls(result.all_messages())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
        deps.guard_events.append(f"Data guide stopped ({type(exc).__name__}).")
        answer = ChatAnswer(answer="Sorry, I couldn't answer that reliably from the data. Try asking about one indicator "
                                   "(for example stunting) for one county or for Kenya.", sources=[])
        usage_obj, calls = None, []
    usage = usage_dict("data_guide", usage_obj, time.perf_counter() - t0) if usage_obj else None
    if usage and log:
        store.log_usage(usage)
    return {**answer.model_dump(), "tool_calls": calls, "guard_events": deps.guard_events, "usage": usage}
