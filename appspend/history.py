"""Local run history in SQLite, so repeat audits can say what changed since last time."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / ".appspend" / "history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  store TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  apps TEXT NOT NULL,          -- JSON list of app names
  monthly_spend REAL,
  savings_monthly REAL,
  audit TEXT NOT NULL          -- full audit JSON
);
CREATE INDEX IF NOT EXISTS runs_store ON runs(store, generated_at);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, audit_dict: dict) -> dict | None:
    """Store a run and return the diff against the previous run for the same store, if any."""
    store = audit_dict.get("store") or "unknown"
    apps = sorted(i["name"] for i in audit_dict["inventory"])
    prev = conn.execute(
        "SELECT generated_at, apps, savings_monthly FROM runs WHERE store=? ORDER BY generated_at DESC LIMIT 1", (store,)
    ).fetchone()
    s = audit_dict["summary"]
    conn.execute(
        "INSERT INTO runs(store, generated_at, apps, monthly_spend, savings_monthly, audit) VALUES (?,?,?,?,?,?)",
        (store, audit_dict["generated_at"], json.dumps(apps), s.get("monthly_app_spend"),
         s.get("savings_monthly_confirmed"), json.dumps(audit_dict)),
    )
    conn.commit()
    if not prev:
        return None
    before = set(json.loads(prev[1]))
    return {
        "since": prev[0],
        "added": sorted(set(apps) - before),
        "removed": sorted(before - set(apps)),
        "savings_before": prev[2],
        "savings_now": s.get("savings_monthly_confirmed"),
    }


def runs(conn: sqlite3.Connection, store: str) -> list[tuple]:
    return conn.execute(
        "SELECT generated_at, json_array_length(apps), monthly_spend, savings_monthly FROM runs WHERE store=? ORDER BY generated_at",
        (store,),
    ).fetchall()
