"""Download the WHO Child Growth Standards (LMS tables) and save them as data/who_growth_standards.json.

Run once (the JSON is committed, so the app does not need this at runtime):
    python scripts/build_who_tables.py

Source: https://www.who.int/tools/child-growth-standards/standards (expanded z-score tables).
"""

import io
import json
import ssl
import urllib.request
from pathlib import Path

import certifi
import openpyxl

BASE = "https://cdn.who.int/media/docs/default-source/child-growth/child-growth-standards/indicators"
FILES = {
    # indicator: (path template, x column) - x is age in days, or length/height in cm
    "wfa": ("weight-for-age/expanded-tables/wfa-{sex}-zscore-expanded-tables.xlsx", "Day"),
    "lhfa": ("length-height-for-age/expandable-tables/lhfa-{sex}-zscore-expanded-tables.xlsx", "Day"),
    "wfl": ("weight-for-length-height/expanded-tables/wfl-{sex}-zscore-expanded-table.xlsx", "Length"),
    "wfh": ("weight-for-length-height/expanded-tables/wfh-{sex}-zscore-expanded-tables.xlsx", "Height"),
    "acfa": ("arm-circumference-for-age/expanded-tables/acfa-{sex}-zscore-expanded-tables.xlsx", "Day"),
}
OUT = Path(__file__).resolve().parents[1] / "data" / "who_growth_standards.json"


def main() -> None:
    ctx = ssl.create_default_context(cafile=certifi.where())
    tables: dict[str, dict[str, list[list[float]]]] = {}
    for key, (template, xcol) in FILES.items():
        for sex in ("boys", "girls"):
            url = f"{BASE}/{template.format(sex=sex)}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # the WHO CDN rejects Python's default agent
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                ws = openpyxl.load_workbook(io.BytesIO(resp.read()), read_only=True).active
            rows = list(ws.iter_rows(values_only=True))
            head = list(rows[0])
            ix, il, im, is_ = head.index(xcol), head.index("L"), head.index("M"), head.index("S")
            tables.setdefault(key, {})["male" if sex == "boys" else "female"] = [
                [r[ix], r[il], r[im], r[is_]] for r in rows[1:] if r[ix] is not None
            ]
            print(key, sex, len(tables[key]["male" if sex == "boys" else "female"]), "rows")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"source": "WHO Child Growth Standards (2006), expanded LMS tables", "tables": tables},
                              separators=(",", ":")))
    print("wrote", OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
