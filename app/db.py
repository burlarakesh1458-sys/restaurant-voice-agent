"""SQLite schema and seeding from data/*.csv."""

from __future__ import annotations

import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dishes (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, aliases TEXT, category TEXT, description TEXT,
  price_inr INTEGER, vegetarian INTEGER, vegan INTEGER, ingredients TEXT,
  allergen_info_status TEXT NOT NULL CHECK (allergen_info_status IN ('verified','unverified','missing')),
  allergen_note TEXT
);
CREATE TABLE IF NOT EXISTS dish_allergens (
  dish_id INTEGER REFERENCES dishes(id), allergen TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('contains','may_contain','free')),
  source TEXT, verified_on TEXT, PRIMARY KEY (dish_id, allergen)
);
CREATE TABLE IF NOT EXISTS opening_hours (
  weekday INTEGER NOT NULL, open TEXT, close TEXT, last_seating TEXT
);
CREATE TABLE IF NOT EXISTS tables (id TEXT PRIMARY KEY, seats INTEGER NOT NULL, area TEXT);
CREATE TABLE IF NOT EXISTS reservations (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT, party_size INTEGER NOT NULL,
  date TEXT NOT NULL, time TEXT NOT NULL, table_id TEXT REFERENCES tables(id),
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','cancelled')),
  notes TEXT, idempotency_key TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_res_date ON reservations(date, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_res_idem ON reservations(idempotency_key) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS handoffs (
  ticket TEXT PRIMARY KEY, reason TEXT NOT NULL, summary TEXT, phone TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rows(name: str) -> list[dict]:
    with open(DATA_DIR / name, newline="") as f:
        return list(csv.DictReader(f))


def seed(path: str | Path, base_date: date) -> sqlite3.Connection:
    """Create a fresh database at ``path`` (":memory:" allowed) with bookings relative to ``base_date``."""
    if str(path) != ":memory:":
        Path(path).unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.execute("BEGIN")
    for r in _rows("menu.csv"):
        conn.execute(
            "INSERT INTO dishes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(r["id"]),
                r["name"],
                r["aliases"],
                r["category"],
                r["description"],
                int(r["price_inr"]),
                int(r["vegetarian"]),
                int(r["vegan"]),
                r["ingredients"],
                r["allergen_info_status"],
                r["allergen_note"] or None,
            ),
        )
    for r in _rows("allergens.csv"):
        conn.execute(
            "INSERT INTO dish_allergens VALUES (?,?,?,?,?)",
            (int(r["dish_id"]), r["allergen"], r["status"], r["source"], r["verified_on"]),
        )
    for r in _rows("hours.csv"):
        conn.execute(
            "INSERT INTO opening_hours VALUES (?,?,?,?)",
            (int(r["weekday"]), r["open"] or None, r["close"] or None, r["last_seating"] or None),
        )
    for r in _rows("tables.csv"):
        conn.execute("INSERT INTO tables VALUES (?,?,?)", (r["id"], int(r["seats"]), r["area"]))
    for r in _rows("reservations.csv"):
        d = (base_date + timedelta(days=int(r["day_offset"]))).isoformat()
        conn.execute(
            "INSERT INTO reservations (code,name,phone,party_size,date,time,table_id,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (r["code"], r["name"], r["phone"], int(r["party_size"]), d, r["time"], r["table_id"], f"seed:{r['code']}"),
        )
    conn.execute("COMMIT")
    return conn
