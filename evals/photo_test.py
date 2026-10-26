"""Photo intake reliability: read the same real handwritten note several times and compare every field
with what is written on it. Usage: .venv/bin/python evals/photo_test.py [--runs 8]
Writes evals/results/photo_results.json"""

from __future__ import annotations

import argparse
import asyncio

from common import ROOT, save
from backend.agents import run_intake
from backend.growth import MeasurementError, age_days_from_months, assess

PHOTO = ROOT / "docs" / "samples" / "handwritten_note.jpg"
# What the note says: "Juma, 4 years old, 100 cm height laying down, weight 16kg, MUAC - 15.4" (sex not written)
TRUTH = {"child_name": "Juma", "age_months": 48, "length_cm": 100, "measured_lying": True, "weight_kg": 16, "muac_cm": 15.4, "sex": None}


async def main(runs: int) -> None:
    rows = []
    for i in range(runs):
        r = await run_intake("", (PHOTO.read_bytes(), "image/jpeg"), log=False)
        rec = r["record"]
        wrong = {k: rec.get(k) for k, v in TRUTH.items() if rec.get(k) != v and not (isinstance(v, (int, float)) and isinstance(rec.get(k), (int, float)) and abs(rec[k] - v) < 0.01)}
        try:  # would the code have accepted it, once the worker picks the sex?
            assess(sex="male", age_days=age_days_from_months(rec["age_months"] or 0), weight_kg=rec["weight_kg"] or 0,
                   length_cm=rec["length_cm"], measured_lying=rec["measured_lying"], muac_cm=rec["muac_cm"])
            blocked = None
        except MeasurementError as exc:
            blocked = str(exc)
        rows.append({"run": i + 1, "all_correct": not wrong, "wrong_fields": wrong, "blocked_by_range_check": blocked,
                     "cost_usd": r["usage"]["cost_usd"], "seconds": r["usage"]["seconds"]})
        print(i + 1, "✓" if not wrong else f"✗ {wrong}", "| range check:", "blocked" if blocked else "passed")
    n_ok = sum(x["all_correct"] for x in rows)
    wrong_and_passed = sum(1 for x in rows if not x["all_correct"] and not x["blocked_by_range_check"])
    summary = {"runs": runs, "all_fields_correct": n_ok, "misreads": runs - n_ok,
               "misreads_caught_by_range_check": runs - n_ok - wrong_and_passed, "misreads_left_for_worker": wrong_and_passed,
               "sex_guessed": sum(1 for x in rows if "sex" in x["wrong_fields"]),
               "mean_cost_usd": round(sum(x["cost_usd"] for x in rows) / runs, 6)}
    print(summary)
    save("photo_results.json", {"truth": TRUTH, "summary": summary, "rows": rows})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=8)
    asyncio.run(main(p.parse_args().runs))
