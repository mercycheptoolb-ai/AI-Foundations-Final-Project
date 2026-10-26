"""Prompt caching on vs off, token estimates vs real counts, and tokenomics at scale (HW6 Problems 2 & 4 style).

Runs the same set of ordinary visit notes through the full pipeline (all defenses) twice:
  on  - fixed prompt cache key per agent (the app's setting)
  off - no cache key and a random id at the start of the instructions, so nothing can be read from cache
Then checks whether caching changed quality (same referrals, tools used, language, length, retries).

Usage:  .venv/bin/python evals/cache_test.py [--repeats 2]
Writes: evals/results/cache_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics as st

import tiktoken

from common import SETUPS, pipeline, save
from backend.agents import looks_swahili, prompt_text
from backend.config import MODEL_NAME, PRICES

VISITS = [
    "ID KK-0001. Amani, msichana, miezi 19. Uzito 9.4 kg, urefu 78 cm amelala. MUAC 12.2. Hana uvimbe miguuni.",
    "Wanjiru, girl, 10 months. 8.9 kg, length 72 cm lying. MUAC 14.1 cm, no oedema. Breastfeeding well.",
    "Juma, boy, 2 years 3 months. Weight 8.9 kg, height 84 cm standing. MUAC 11.1 cm. Very weak and sleepy.",
    "Baraka boy 36 months, 12.1 kg, height 89 cm standing, MUAC 13.0. Had a cough last week.",
    "Faith, girl, 7 months, 6.1 kg, 63.5 cm lying, MUAC 12.4, eats porridge twice a day.",
    "Ochieng mvulana miaka 4, kilo 15.2, urefu 101 cm amesimama, MUAC 15.1. Anakula vizuri.",
    "Mercy girl 22 months 13.9 kg 82 cm lying MUAC 16.0, drinks soda most days.",
    "Kipchoge, boy, 5 months, 6.8 kg, 64 cm lying. Exclusively breastfed. No swelling.",
]
VISITS_PER_USER_MONTH = 40  # assumption: one CHP does ~40 under-5 growth visits a month (state this in the report)


def estimate_static_tokens() -> dict:
    enc = tiktoken.get_encoding("o200k_base")
    return {name: len(enc.encode(prompt_text(name, True))) for name in ("intake", "triage")}


def quality(run: dict, note: str) -> dict:
    n = run.get("note") or {}
    swahili_note = bool(re.search(r"\b(msichana|mvulana|miezi|miaka|uzito|urefu)\b", note, re.I))
    msg = n.get("caregiver_message", "")
    return {
        "referral_ok": run.get("final_referral") == run.get("rule_referral"),
        "model_referral_matched_rules": n.get("referral") == run.get("rule_referral"),
        "validator_retries": sum("Sent back" in g for g in run.get("guard_events", [])),
        "used_both_tools": {t["tool"] for t in run.get("tool_calls", [])} >= {"get_child_history", "get_kenya_benchmark"},
        "language_ok": looks_swahili(msg) == swahili_note,
        "caregiver_words": len(msg.split()),
        "caregiver_message": msg,
        "fallback": run.get("used_fallback", False),
    }


async def run_mode(mode: str, repeats: int) -> list[dict]:
    rows = []
    for r in range(repeats):
        for i, note in enumerate(VISITS):  # sequential, like real use: one visit after another
            run = await pipeline(note=note, defenses=SETUPS["all"], cache=mode)
            u = {k: sum(x[k] for x in run["usage"]) for k in ("input_tokens", "cached_input_tokens", "output_tokens",
                                                               "reasoning_tokens", "cost_usd", "seconds")}
            rows.append({"mode": mode, "repeat": r, "visit": i, "stopped": run.get("stopped"),
                         "per_agent": run["usage"], **u, **({} if run.get("stopped") else quality(run, note))})
            print(f"{mode:3} r{r} v{i}: in {u['input_tokens']:>5} (cached {u['cached_input_tokens']:>5}) out {u['output_tokens']:>4}"
                  f" ${u['cost_usd']:.5f} {u['seconds']:.1f}s {run.get('stopped') or ''}")
    return rows


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if not r["stopped"]]
    m = lambda k: round(st.mean(r[k] for r in done), 6)  # noqa: E731
    by_agent = {}
    for agent in ("intake", "triage"):
        us = [u for r in done for u in r["per_agent"] if u["agent"] == agent]
        by_agent[agent] = {k: round(st.mean(u[k] for u in us), 1) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")}
        by_agent[agent]["cost_usd"] = round(st.mean(u["cost_usd"] for u in us), 6)
    return {
        "visits": len(done), "mean_input_tokens": m("input_tokens"), "mean_cached_input_tokens": m("cached_input_tokens"),
        "cache_hit_share": round(sum(r["cached_input_tokens"] for r in done) / max(1, sum(r["input_tokens"] for r in done)), 3),
        "mean_output_tokens": m("output_tokens"), "mean_reasoning_tokens": m("reasoning_tokens"),
        "mean_cost_usd": m("cost_usd"), "mean_seconds": round(st.mean(r["seconds"] for r in done), 2),
        "by_agent": by_agent,
        "quality": {
            "referral_ok": sum(r["referral_ok"] for r in done), "model_referral_matched_rules": sum(r["model_referral_matched_rules"] for r in done),
            "validator_retries": sum(r["validator_retries"] for r in done), "used_both_tools": sum(r["used_both_tools"] for r in done),
            "language_ok": sum(r["language_ok"] for r in done), "fallbacks": sum(r["fallback"] for r in done),
            "mean_caregiver_words": round(st.mean(r["caregiver_words"] for r in done), 1),
        },
    }


def scale(cost_per_visit: float) -> dict:
    per_user = cost_per_visit * VISITS_PER_USER_MONTH
    return {
        "assumption_visits_per_user_month": VISITS_PER_USER_MONTH,
        "cost_per_visit_usd": round(cost_per_visit, 6),
        "per_user_month_usd": round(per_user, 4),
        "users": {str(n): {"month_usd": round(per_user * n, 2), "year_usd": round(per_user * n * 12, 2)}
                  for n in (1, 100, 10_000, 100_000)},
    }


async def main(repeats: int) -> None:
    est = estimate_static_tokens()
    on = await run_mode("on", repeats)
    off = await run_mode("off", repeats)
    s_on, s_off = summarize(on), summarize(off)
    saving = 1 - s_on["mean_cost_usd"] / s_off["mean_cost_usd"]
    path = save("cache_results.json", {
        "model": MODEL_NAME, "prices_per_1m": PRICES, "repeats": repeats, "visits": VISITS,
        "static_prompt_token_estimate_tiktoken": est,
        "on": s_on, "off": s_off, "cost_saving_share": round(saving, 3),
        "tokenomics_cache_on": scale(s_on["mean_cost_usd"]), "tokenomics_cache_off": scale(s_off["mean_cost_usd"]),
        "rows": on + off,
    })
    print(f"\nstatic prompt estimate (tiktoken o200k): {est}")
    for name, s in (("cache on", s_on), ("cache off", s_off)):
        print(f"{name:9}: in {s['mean_input_tokens']:.0f} (cached {s['mean_cached_input_tokens']:.0f}, {s['cache_hit_share']:.0%}) "
              f"out {s['mean_output_tokens']:.0f}  ${s['mean_cost_usd']:.5f}/visit  {s['mean_seconds']}s  quality {s['quality']}")
    print(f"caching saves {saving:.0%} per visit · per user/month ${scale(s_on['mean_cost_usd'])['per_user_month_usd']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repeats", type=int, default=2)
    asyncio.run(main(p.parse_args().repeats))
