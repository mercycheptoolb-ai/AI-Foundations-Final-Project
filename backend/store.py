"""SQLite storage for visits, plus the token usage log and the daily budget check."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .config import DATA, OUTPUT

DB = DATA / "kukua.db"
USAGE_LOG = OUTPUT / "usage_log.jsonl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT 'demo',
    child_key TEXT NOT NULL,
    child_name TEXT NOT NULL,
    sex TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    age_days INTEGER NOT NULL,
    weight_kg REAL NOT NULL,
    length_cm REAL,
    muac_cm REAL,
    status TEXT NOT NULL,
    referral TEXT NOT NULL,
    assessment_json TEXT,
    note_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS visits_child ON visits(child_id);
CREATE INDEX IF NOT EXISTS visits_name ON visits(child_key);
"""

# Fictional demo children so history and growth-faltering checks have something to show.
# Two different girls are both called Amani: history must follow the ID, never the name.
SEED = [
    ("KK-0001", "Amani", "female", "2026-08-20", 487, 9.6, 77.0, 13.6, "normal", "routine_growth_monitoring"),
    ("KK-0001", "Amani", "female", "2026-09-22", 520, 9.6, 77.8, 13.2, "normal", "routine_growth_monitoring"),
    ("KK-0002", "Baraka", "male", "2026-09-15", 880, 11.4, 85.5, 13.4, "at_risk", "counsel_and_follow_up"),
    ("KK-0003", "Amani", "female", "2026-09-30", 1310, 13.2, 95.0, 15.0, "normal", "routine_growth_monitoring"),
]
ID_PREFIX = "KK-"


def child_key(name: str) -> str:
    return " ".join(name.lower().split())


def connect(db: Path = DB) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "INSERT INTO visits (child_id, child_key, child_name, sex, visit_date, age_days, weight_kg, length_cm, "
            "muac_cm, status, referral, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r[0], child_key(r[1]), *r[1:], now) for r in SEED])
        con.commit()
    return con


def normalize_id(child_id: str) -> str:
    return child_id.strip().upper()


def history(child_id: str | None, limit: int = 6) -> list[dict]:
    """Earlier visits for exactly this child ID (never matched by name)."""
    if not child_id:
        return []
    with connect() as con:
        rows = con.execute(
            "SELECT visit_date, age_days, weight_kg, length_cm, muac_cm, status, referral FROM visits "
            "WHERE child_id = ? ORDER BY age_days DESC LIMIT ?", (normalize_id(child_id), limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def child(child_id: str) -> dict | None:
    """Latest known name, sex and age for an ID, or None if the ID has never been seen."""
    with connect() as con:
        row = con.execute(
            "SELECT child_id, child_name, sex, visit_date, age_days FROM visits WHERE child_id = ? "
            "ORDER BY id DESC LIMIT 1", (normalize_id(child_id),)).fetchone()
    return dict(row) if row else None


def find_children(name: str, limit: int = 8) -> list[dict]:
    """Children whose name contains the search text, one row per ID, so the worker can pick the right one."""
    if not name.strip():
        return []
    with connect() as con:
        rows = con.execute(
            "SELECT child_id, child_name, sex, MAX(visit_date) AS last_visit, MAX(age_days) AS last_age_days, "
            "COUNT(*) AS visits FROM visits WHERE child_key LIKE ? GROUP BY child_id "
            "ORDER BY last_visit DESC LIMIT ?", (f"%{child_key(name)}%", limit)).fetchall()
    return [dict(r) for r in rows]


def new_child_id() -> str:
    with connect() as con:
        ids = [r[0] for r in con.execute("SELECT DISTINCT child_id FROM visits WHERE child_id LIKE ?", (ID_PREFIX + "%",))]
    nums = [int(i[len(ID_PREFIX):]) for i in ids if i[len(ID_PREFIX):].isdigit()]
    return f"{ID_PREFIX}{max(nums, default=0) + 1:04d}"


def save_visit(worker: str, child_id: str, name: str, sex: str, age_days: int, weight_kg: float, length_cm: float | None,
               muac_cm: float | None, status: str, referral: str, assessment: dict, note: dict | None) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO visits (worker, child_id, child_key, child_name, sex, visit_date, age_days, weight_kg, length_cm, "
            "muac_cm, status, referral, assessment_json, note_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (worker, normalize_id(child_id), child_key(name), name.strip(), sex, date.today().isoformat(), age_days, weight_kg,
             length_cm, muac_cm, status, referral, json.dumps(assessment), json.dumps(note) if note else None,
             datetime.now(timezone.utc).isoformat()))
        return int(cur.lastrowid)


def recent_visits(worker: str, limit: int = 50) -> list[dict]:
    """Visits recorded by this health worker, newest first."""
    with connect() as con:
        rows = con.execute(
            "SELECT id, child_id, child_name, sex, visit_date, age_days, weight_kg, length_cm, muac_cm, status, referral "
            "FROM visits WHERE worker = ? ORDER BY id DESC LIMIT ?", (worker, limit)).fetchall()
    return [dict(r) for r in rows]


def log_usage(entry: dict) -> None:
    with USAGE_LOG.open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")


def spent_today() -> float:
    if not USAGE_LOG.exists():
        return 0.0
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0.0
    for line in USAGE_LOG.read_text().splitlines():
        e = json.loads(line)
        if e["at"].startswith(today):
            total += e.get("cost_usd", 0.0)
    return total
