"""Download Kenya DHS nutrition indicators by county and province (2014, 2022) from the DHS Program API.

Run once (the JSON is committed):  .venv/bin/python scripts/build_region_data.py
Writes data/kenya_dhs_regions.json, used by the data-guide chatbot.
Source: The DHS Program STATcompiler API, https://api.dhsprogram.com (Kenya DHS 2014 and 2022).
"""

import json
import ssl
import urllib.request
from pathlib import Path

import certifi

API = "https://api.dhsprogram.com/rest/dhs/data"
SURVEYS = {"KE2022DHS": 2022, "KE2014DHS": 2014}
INDICATORS = {
    "CN_NUTS_C_HA2": ("stunting", "Children under 5 stunted (height-for-age < -2 SD)", "children"),
    "CN_NUTS_C_HA3": ("severe_stunting", "Children under 5 severely stunted (< -3 SD)", "children"),
    "CN_NUTS_C_WH2": ("wasting", "Children under 5 wasted (weight-for-height < -2 SD)", "children"),
    "CN_NUTS_C_WH3": ("severe_wasting", "Children under 5 severely wasted (< -3 SD)", "children"),
    "CN_NUTS_C_WA2": ("underweight", "Children under 5 underweight (weight-for-age < -2 SD)", "children"),
    "CN_NUTS_C_WHP": ("child_overweight", "Children under 5 overweight (weight-for-height > +2 SD)", "children"),
    "AN_NUTS_W_THN": ("women_thin", "Women 15-49 thin (BMI < 18.5)", "women"),
    "AN_NUTS_W_OWT": ("women_overweight_or_obese", "Women 15-49 overweight or obese (BMI >= 25)", "women"),
    "AN_NUTS_W_OBS": ("women_obese", "Women 15-49 obese (BMI >= 30)", "women"),
    "AN_NUTS_W_BMI": ("women_mean_bmi", "Women 15-49 mean BMI (kg/m2)", "women"),
    "AN_NUTS_W_SHT": ("women_short", "Women 15-49 shorter than 145 cm", "women"),
}
OUT = Path(__file__).resolve().parents[1] / "data" / "kenya_dhs_regions.json"


def fetch(breakdown: str) -> list[dict]:
    url = (f"{API}?countryIds=KE&surveyIds={','.join(SURVEYS)}&indicatorIds={','.join(INDICATORS)}"
           f"&breakdown={breakdown}&f=json&perpage=5000")
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
        return json.load(resp)["Data"]


def main() -> None:
    rows = []
    for breakdown in ("national", "subnational"):
        for r in fetch(breakdown):
            if r["IndicatorId"] not in INDICATORS or r["Value"] is None:
                continue
            label = r["CharacteristicLabel"]
            if breakdown == "national":
                level, name = "national", "Kenya"
            elif label.startswith(".."):
                level, name = "county", label.lstrip(".").strip()
            else:
                level, name = "province", label.strip()
            rows.append({"indicator": INDICATORS[r["IndicatorId"]][0], "year": SURVEYS[r["SurveyId"]], "level": level,
                         "name": name, "value": r["Value"], "sample": r.get("DenominatorUnweighted") or None})
    # A county/province appears once per indicator and year; keep the first (preferred) value.
    seen, clean = set(), []
    for r in rows:
        key = (r["indicator"], r["year"], r["level"], r["name"])
        if key not in seen:
            seen.add(key)
            clean.append(r)
    data = {
        "source": "Kenya Demographic and Health Survey 2014 and 2022, via The DHS Program API (api.dhsprogram.com)",
        "indicators": {k: {"label": lab, "population": pop, "unit": "kg/m²" if k.endswith("bmi") else "%"}
                       for k, lab, pop in INDICATORS.values()},
        "rows": clean,
    }
    OUT.write_text(json.dumps(data, indent=0, ensure_ascii=False))
    counties = {r["name"] for r in clean if r["level"] == "county"}
    print(f"wrote {OUT.name}: {len(clean)} values, {len(counties)} counties, "
          f"{len({r['name'] for r in clean if r['level'] == 'province'})} provinces")


if __name__ == "__main__":
    main()
