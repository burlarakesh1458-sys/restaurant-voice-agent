"""The agent's tools: deterministic, validated, idempotent functions over SQLite.

The LLM only decides *which* tool to call and extracts raw argument strings. Everything that
must be correct (dates, availability, table allocation, allergen facts, confirmation codes)
is plain code here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import nlu
from .clock import Clock

SEATING_MINUTES = 90
MAX_ONLINE_PARTY = 8
BOOKING_HORIZON_DAYS = 60
MUTATING = {"create_reservation", "modify_reservation", "cancel_reservation"}

ALLERGEN_WORDS = {
    # spoken word -> canonical allergen(s)
    "nut": ["tree_nuts", "peanuts"],
    "nuts": ["tree_nuts", "peanuts"],
    "tree nut": ["tree_nuts"],
    "tree nuts": ["tree_nuts"],
    "tree_nuts": ["tree_nuts"],
    "pine nut": ["tree_nuts"],
    "pine nuts": ["tree_nuts"],
    "walnut": ["tree_nuts"],
    "walnuts": ["tree_nuts"],
    "almond": ["tree_nuts"],
    "almonds": ["tree_nuts"],
    "pistachio": ["tree_nuts"],
    "pistachios": ["tree_nuts"],
    "cashew": ["tree_nuts"],
    "cashews": ["tree_nuts"],
    "hazelnut": ["tree_nuts"],
    "hazelnuts": ["tree_nuts"],
    "peanut": ["peanuts"],
    "peanuts": ["peanuts"],
    "groundnut": ["peanuts"],
    "gluten": ["gluten"],
    "wheat": ["gluten"],
    "dairy": ["milk"],
    "milk": ["milk"],
    "lactose": ["milk"],
    "cheese": ["milk"],
    "egg": ["eggs"],
    "eggs": ["eggs"],
    "fish": ["fish"],
    "shellfish": ["crustaceans", "molluscs"],
    "crustacean": ["crustaceans"],
    "crustaceans": ["crustaceans"],
    "prawn": ["crustaceans"],
    "prawns": ["crustaceans"],
    "shrimp": ["crustaceans"],
    "molluscs": ["molluscs"],
    "mollusc": ["molluscs"],
    "soy": ["soy"],
    "soya": ["soy"],
    "sesame": ["sesame"],
    "celery": ["celery"],
    "mustard": ["mustard"],
    "sulphites": ["sulphites"],
    "sulfites": ["sulphites"],
    "sulphite": ["sulphites"],
    "sulfite": ["sulphites"],
    "lupin": ["lupin"],
}
ALL_ALLERGENS = [
    "gluten",
    "crustaceans",
    "eggs",
    "fish",
    "peanuts",
    "tree_nuts",
    "soy",
    "milk",
    "celery",
    "mustard",
    "sesame",
    "sulphites",
    "lupin",
    "molluscs",
]
ALLERGEN_LABEL = {
    "tree_nuts": "tree nuts",
    "peanuts": "peanuts",
    "crustaceans": "crustaceans",
    "molluscs": "molluscs",
    "sulphites": "sulphites",
}


def label(a: str) -> str:
    return ALLERGEN_LABEL.get(a, a)


def canonical_allergens(word: str | None) -> list[str]:
    if not word:
        return []
    w = word.strip().lower().replace("-", " ")
    w = w.removesuffix(" allergy").removesuffix(" free")
    return ALLERGEN_WORDS.get(w, ALLERGEN_WORDS.get(w.rstrip("s"), []))


def _hm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _fmt_time(t: str) -> str:
    h, m = divmod(_hm(t), 60)
    suffix = "PM" if h >= 12 else "AM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"


def _fmt_date(d: str) -> str:
    dt = date.fromisoformat(d)
    return dt.strftime("%A %d %B").replace(" 0", " ")


def spell_code(code: str) -> str:
    """TV-D4F8 -> "T V D 4 F 8" (easier to hear on a phone line)."""
    return " ".join(c for c in code if c != "-")


class ToolError(Exception):
    """Transient infrastructure failure (DB locked, timeout). Retried by the executor."""


@dataclass
class CallContext:
    caller_phone: str | None = None
    low_confidence: bool = False  # ASR confidence below threshold this turn -> confirm before writing
    session_id: str = "local"


@dataclass
class FaultPlan:
    """Fault injection for tests and the eval suite: {tool: (mode, n_failures)}; mode = timeout|error."""

    faults: dict[str, tuple[str, int]] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)

    def check(self, tool: str) -> str | None:
        if tool not in self.faults:
            return None
        mode, n = self.faults[tool]
        k = self.seen.get(tool, 0)
        self.seen[tool] = k + 1
        return mode if k < n else None


# ----------------------------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format). Descriptions are kept short to save tokens.
# ----------------------------------------------------------------------------------------------
CATEGORY_WORDS = {
    "dessert": "dolci",
    "desserts": "dolci",
    "sweet": "dolci",
    "sweets": "dolci",
    "starter": "antipasti",
    "starters": "antipasti",
    "appetizer": "antipasti",
    "appetizers": "antipasti",
    "main": "secondi",
    "mains": "secondi",
    "soup": "zuppe",
    "soups": "zuppe",
    "salad": "insalate",
    "salads": "insalate",
    "bread": "pane",
}
DATE_DESC = "as the caller said it, e.g. 'tonight', 'saturday'"


def _fn(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_D = {"type": "string", "description": DATE_DESC}
TOOL_SCHEMAS = [
    _fn(
        "check_availability",
        "Is a table free? Returns alternatives if not.",
        {"date": _D, "time": _S, "party_size": _I},
        ["date", "time", "party_size"],
    ),
    _fn(
        "search_menu",
        "Find dishes by keyword/diet. free_of returns only VERIFIED allergen-free dishes.",
        {"query": _S, "vegetarian": _B, "vegan": _B, "free_of": _S, "max_price_inr": _I},
    ),
    _fn(
        "get_allergens",
        "Verified allergen record of a dish. Sole source for allergen answers.",
        {"dish": _S, "allergen": _S},
        ["dish"],
    ),
    _fn("get_opening_hours", "Opening hours for a date, or the week.", {"date": _D}),
    _fn(
        "create_reservation",
        "Book a table (idempotent).",
        {"name": _S, "party_size": _I, "date": _D, "time": _S, "notes": _S},
        ["name", "party_size", "date", "time"],
    ),
    _fn(
        "modify_reservation",
        "Change an existing booking.",
        {"code": _S, "name": _S, "new_date": _D, "new_time": _S, "new_party_size": _I},
    ),
    _fn("cancel_reservation", "Cancel a booking by code, or by name from the booking phone.", {"code": _S, "name": _S}),
    _fn("handoff_to_human", "Transfer the caller to staff.", {"reason": _S, "summary": _S}, ["reason"]),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


class RestaurantTools:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _services(self, d: str) -> list[sqlite3.Row]:
        wd = date.fromisoformat(d).weekday()
        return [
            r
            for r in self.conn.execute(
                "SELECT * FROM opening_hours WHERE weekday=? AND open IS NOT NULL ORDER BY open", (wd,)
            )
        ]

    def _norm_date(self, raw) -> str | None:
        return nlu.parse_date(raw, self.clock.now())

    def _validate_slot(self, raw_date, raw_time, raw_party) -> tuple[dict | None, tuple[str, str, int] | None]:
        d = self._norm_date(raw_date)
        if not d:
            return {"error": "invalid_date", "message": f"Could not understand the date '{raw_date}'."}, None
        t = nlu.parse_time(raw_time)
        if not t:
            return {"error": "invalid_time", "message": f"Could not understand the time '{raw_time}'."}, None
        p = nlu.parse_party_size(raw_party)
        if p is None or p < 1:
            return {"error": "invalid_party_size", "message": "Party size must be at least 1."}, None
        if p > MAX_ONLINE_PARTY:
            return {
                "error": "large_party",
                "party_size": p,
                "message": f"Parties over {MAX_ONLINE_PARTY} are handled by our events team.",
                "suggest": "handoff_to_human",
            }, None
        now = self.clock.now()
        slot_dt = datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=now.tzinfo)
        if slot_dt < now + timedelta(minutes=15):
            return {
                "error": "time_in_past",
                "date": d,
                "time": t,
                "message": "That time has already passed or is too soon.",
            }, None
        if slot_dt > now + timedelta(days=BOOKING_HORIZON_DAYS):
            return {
                "error": "too_far_ahead",
                "message": f"We take bookings up to {BOOKING_HORIZON_DAYS} days ahead.",
            }, None
        return None, (d, t, p)

    def _in_service(self, d: str, t: str) -> bool:
        m = _hm(t)
        return any(_hm(s["open"]) <= m <= _hm(s["last_seating"]) for s in self._services(d))

    def _free_table(self, d: str, t: str, p: int, exclude_code: str | None = None) -> str | None:
        m = _hm(t)
        busy = {
            r["table_id"]
            for r in self.conn.execute(
                "SELECT table_id, time FROM reservations WHERE date=? AND status='confirmed' AND code IS NOT ?",
                (d, exclude_code),
            )
            if abs(_hm(r["time"]) - m) < SEATING_MINUTES
        }
        for r in self.conn.execute("SELECT id, seats FROM tables WHERE seats>=? ORDER BY seats, id", (p,)):
            if r["id"] not in busy:
                return r["id"]
        return None

    def _alternatives(self, d: str, t: str, p: int, exclude_code: str | None = None, k: int = 3) -> list[str]:
        now = self.clock.now()
        cands = []
        for s in self._services(d):
            m = _hm(s["open"])
            while m <= _hm(s["last_seating"]):
                hhmm = f"{m // 60:02d}:{m % 60:02d}"
                slot_dt = datetime.fromisoformat(f"{d}T{hhmm}").replace(tzinfo=now.tzinfo)
                if hhmm != t and slot_dt >= now + timedelta(minutes=15) and self._free_table(d, hhmm, p, exclude_code):
                    cands.append(hhmm)
                m += 15
        cands.sort(key=lambda x: (abs(_hm(x) - _hm(t)), _hm(x)))
        # prefer spread-out suggestions (not 19:15, 19:00, 18:45)
        picked: list[str] = []
        for c in cands:
            if all(abs(_hm(c) - _hm(x)) >= 30 for x in picked):
                picked.append(c)
            if len(picked) == k:
                break
        return sorted(picked, key=_hm)

    def _closed_reason(self, d: str, t: str) -> dict:
        services = self._services(d)
        if not services:
            return {
                "available": False,
                "reason": "closed",
                "date": d,
                "message": f"We're closed on {date.fromisoformat(d).strftime('%A')}s.",
            }
        hours = ", ".join(f"{_fmt_time(s['open'])} to {_fmt_time(s['close'])}" for s in services)
        return {
            "available": False,
            "reason": "outside_hours",
            "date": d,
            "time": t,
            "message": f"On {_fmt_date(d)} we're open {hours}; last seating is an hour before closing.",
        }

    # ------------------------------------------------------------------ tools
    def check_availability(self, ctx: CallContext, date=None, time=None, party_size=None) -> dict:
        err, slot = self._validate_slot(date, time, party_size)
        if err:
            return err
        d, t, p = slot
        if not self._in_service(d, t):
            out = self._closed_reason(d, t)
            out["alternatives"] = self._alternatives(d, t, p) if out["reason"] == "outside_hours" else []
            return out
        if self._free_table(d, t, p):
            return {
                "available": True,
                "date": d,
                "time": t,
                "party_size": p,
                "spoken": f"{_fmt_date(d)} at {_fmt_time(t)} for {p}",
            }
        alts = self._alternatives(d, t, p)
        return {
            "available": False,
            "reason": "fully_booked",
            "date": d,
            "time": t,
            "party_size": p,
            "alternatives": alts,
            "alternatives_spoken": [_fmt_time(a) for a in alts],
        }

    def search_menu(
        self, ctx: CallContext, query=None, vegetarian=None, vegan=None, free_of=None, max_price_inr=None, category=None
    ) -> dict:
        rows = list(self.conn.execute("SELECT * FROM dishes ORDER BY id"))
        q = (query or category or "").strip().lower()
        stop = {
            "menu",
            "dishes",
            "dish",
            "food",
            "options",
            "something",
            "anything",
            "what",
            "the",
            "a",
            "do",
            "you",
            "have",
            "any",
            "your",
            "on",
            "items",
            "item",
        }
        terms = [CATEGORY_WORDS.get(w, w) for w in nlu.tokens(q) if w not in stop and len(w) > 2]
        out = []
        for r in rows:
            hay = " ".join([r["name"], r["aliases"] or "", r["category"], r["description"], r["ingredients"]]).lower()
            if terms and not any(w.rstrip("s") in hay for w in terms):
                continue
            if vegetarian and not r["vegetarian"]:
                continue
            if vegan and not r["vegan"]:
                continue
            if max_price_inr and r["price_inr"] > int(max_price_inr):
                continue
            if free_of:
                wanted = canonical_allergens(free_of)
                if not wanted:
                    return {"error": "unknown_allergen", "message": f"Unknown allergen '{free_of}'."}
                if r["allergen_info_status"] != "verified":
                    continue
                st = {
                    a["allergen"]: a["status"]
                    for a in self.conn.execute(
                        "SELECT allergen, status FROM dish_allergens WHERE dish_id=?", (r["id"],)
                    )
                }
                if any(st.get(a) != "free" for a in wanted):
                    continue
            out.append(
                {
                    "name": r["name"],
                    "category": r["category"],
                    "description": r["description"],
                    "price_inr": r["price_inr"],
                    "vegetarian": bool(r["vegetarian"]),
                    "vegan": bool(r["vegan"]),
                }
            )
        res = {"count": len(out), "dishes": out[:6]}
        if free_of:
            res["note"] = (
                "Only dishes with a VERIFIED 'free' record are listed; dishes with missing "
                "allergen data are excluded, not assumed safe."
            )
        return res

    def _resolve_dish(self, raw: str) -> dict:
        q = (raw or "").strip().lower()
        q = q.removeprefix("the ").strip()
        rows = list(self.conn.execute("SELECT * FROM dishes"))
        exact, partial = [], []
        for r in rows:
            names = [r["name"].lower()] + [a for a in (r["aliases"] or "").split("|") if a]
            if q in names:
                exact.append(r)
            elif q and any(q in n or n in q for n in names) or (len(q) > 3 and q in r["name"].lower()):
                partial.append(r)
        if exact:
            return {"dish": exact[0], "related": [r for r in partial if r["id"] != exact[0]["id"]]}
        if len(partial) == 1:
            return {"dish": partial[0], "related": []}
        if len(partial) > 1:
            return {"ambiguous": [r["name"] for r in partial]}
        best, score = None, 0.0
        for r in rows:
            for n in [r["name"]] + (r["aliases"] or "").split("|"):
                s = nlu.similarity(q, n)
                if s > score:
                    best, score = r, s
        if best is not None and score >= 0.7:
            return {"did_you_mean": best["name"], "score": round(score, 2)}
        return {"not_found": True}

    def allergen_record(self, dish_row: sqlite3.Row) -> dict:
        rows = {
            r["allergen"]: r
            for r in self.conn.execute("SELECT * FROM dish_allergens WHERE dish_id=?", (dish_row["id"],))
        }
        return {
            "status": dish_row["allergen_info_status"],
            "rows": {a: r["status"] for a, r in rows.items()},
            "verified_on": next(iter(rows.values()))["verified_on"] if rows else None,
        }

    def allergen_status(self, dish_row: sqlite3.Row, allergen: str) -> str:
        """contains | may_contain | free | unknown  -- unknown unless a verified row exists."""
        rec = self.allergen_record(dish_row)
        if rec["status"] != "verified":
            return "unknown"
        return rec["rows"].get(allergen, "unknown")

    def get_allergens(self, ctx: CallContext, dish=None, allergen=None) -> dict:
        if not dish:
            return {"error": "missing_dish", "message": "Which dish?"}
        res = self._resolve_dish(str(dish))
        if "ambiguous" in res:
            return {
                "ambiguous": True,
                "candidates": res["ambiguous"],
                "message": "Several dishes match; ask which one.",
            }
        if "did_you_mean" in res:
            return {
                "did_you_mean": res["did_you_mean"],
                "message": f"No dish called '{dish}'. Confirm the caller meant {res['did_you_mean']} "
                "before giving any allergen information.",
            }
        if "not_found" in res:
            return {"not_found": True, "message": f"'{dish}' is not on our menu."}
        row = res["dish"]
        out = self._allergen_answer(row, allergen)
        if res["related"]:
            out["other_matching_dishes"] = [self._allergen_answer(r, allergen) for r in res["related"]]
        return out

    def _allergen_answer(self, row: sqlite3.Row, allergen: str | None) -> dict:
        rec = self.allergen_record(row)
        name = row["name"]
        out: dict = {"dish": name, "allergen_info_status": rec["status"]}
        if rec["status"] != "verified":
            out["verified"] = False
            out["answer"] = (
                f"I can't confirm the allergens in the {name}: we don't have a verified allergen "
                f"record for it. I can connect you with a staff member to check with the kitchen."
            )
            out["policy"] = "DO NOT GUESS. Say you can't confirm and offer a staff handoff."
            return out
        out["verified"] = True
        out["verified_on"] = rec["verified_on"]
        out["contains"] = sorted(label(a) for a, s in rec["rows"].items() if s == "contains")
        out["may_contain"] = sorted(label(a) for a, s in rec["rows"].items() if s == "may_contain")
        out["not_assessed"] = [label(a) for a in ALL_ALLERGENS if a not in rec["rows"]]
        if allergen:
            wanted = canonical_allergens(allergen)
            if not wanted:
                out["answer"] = f"I don't have a verified record for '{allergen}' in the {name}."
                out["policy"] = "DO NOT GUESS."
                return out
            parts, unknown = [], False
            for a in wanted:
                st = rec["rows"].get(a, "unknown")
                if st == "contains":
                    parts.append(f"contains {label(a)}")
                elif st == "may_contain":
                    parts.append(f"may contain traces of {label(a)}")
                elif st == "free":
                    parts.append(f"is verified free of {label(a)}")
                else:
                    unknown = True
                    parts.append(f"has no verified record for {label(a)}")
            out["query"] = {label(a): rec["rows"].get(a, "unknown") for a in wanted}
            note = f" ({row['allergen_note']})" if row["allergen_note"] else ""
            out["answer"] = f"Our verified allergen record says the {name} " + " and ".join(parts) + f".{note}"
            if unknown:
                out["answer"] += " I can't confirm that part; a staff member can check with the kitchen."
                out["policy"] = "Unknown part: say you can't confirm and offer a staff handoff."
        else:
            out["answer"] = (
                f"The {name} contains {', '.join(out['contains']) or 'none of the 14 major allergens'}"
                + (f", and may contain {', '.join(out['may_contain'])}" if out["may_contain"] else "")
                + "."
            )
        return out

    def get_opening_hours(self, ctx: CallContext, date=None) -> dict:
        if date:
            d = self._norm_date(date)
            if not d:
                return {"error": "invalid_date", "message": f"Could not understand '{date}'."}
            s = self._services(d)
            if not s:
                return {"date": d, "weekday": _fmt_date(d), "closed": True}
            return {
                "date": d,
                "weekday": _fmt_date(d),
                "closed": False,
                "services": [
                    {
                        "open": _fmt_time(r["open"]),
                        "close": _fmt_time(r["close"]),
                        "last_seating": _fmt_time(r["last_seating"]),
                    }
                    for r in s
                ],
            }
        week = {}
        for wd, name in enumerate(nlu.WEEKDAYS):
            rows = list(self.conn.execute("SELECT * FROM opening_hours WHERE weekday=? AND open IS NOT NULL", (wd,)))
            week[name] = (
                "closed" if not rows else ", ".join(f"{_fmt_time(r['open'])}-{_fmt_time(r['close'])}" for r in rows)
            )
        return {"week": week}

    def _readback(self, r: dict | sqlite3.Row) -> str:
        return (
            f"a table for {r['party_size']} under {r['name']} on {_fmt_date(r['date'])} at {_fmt_time(r['time'])}"
            f", confirmation code {spell_code(r['code'])}"
        )

    def create_reservation(
        self, ctx: CallContext, name=None, party_size=None, date=None, time=None, notes=None, phone=None
    ) -> dict:
        name = (name or "").strip()
        if not name or name.lower() in {"unknown", "guest", "customer", "caller", "n/a", "none", "user"}:
            return {"error": "missing_name", "message": "Ask the caller for a name for the booking."}
        err, slot = self._validate_slot(date, time, party_size)
        if err:
            return err
        d, t, p = slot
        phone = nlu.normalize_phone(phone) or ctx.caller_phone
        key = f"{name.lower()}|{d}|{t}|{p}"
        existing = self.conn.execute(
            "SELECT * FROM reservations WHERE idempotency_key=? AND status='confirmed'", (key,)
        ).fetchone()
        if existing:
            return {
                "status": "confirmed",
                "duplicate": True,
                "code": existing["code"],
                "readback": self._readback(existing),
                "message": "This booking already exists; no second booking was made.",
            }
        if not self._in_service(d, t):
            out = self._closed_reason(d, t)
            out["status"] = "not_booked"
            out["alternatives"] = self._alternatives(d, t, p) if out["reason"] == "outside_hours" else []
            return out
        table = self._free_table(d, t, p)
        if not table:
            alts = self._alternatives(d, t, p)
            return {
                "status": "not_booked",
                "reason": "fully_booked",
                "alternatives": alts,
                "alternatives_spoken": [_fmt_time(a) for a in alts],
            }
        if ctx.low_confidence:
            return {
                "status": "needs_confirmation",
                "readback": f"a table for {p} under {name} on {_fmt_date(d)} at {_fmt_time(t)}",
                "message": "Speech recognition was uncertain. Read this back and ask the caller to confirm "
                "before booking.",
            }
        code = self._new_code(key)
        self.conn.execute(
            "INSERT INTO reservations (code,name,phone,party_size,date,time,table_id,notes,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (code, name, phone, p, d, t, table, notes, key),
        )
        row = {"code": code, "name": name, "party_size": p, "date": d, "time": t}
        return {
            "status": "confirmed",
            "code": code,
            "date": d,
            "time": t,
            "party_size": p,
            "name": name,
            "readback": self._readback(row),
        }

    def _new_code(self, key: str) -> str:
        salt = 0
        while True:
            h = hashlib.sha1(f"{key}#{salt}".encode()).digest()
            code = "TV-" + base64.b32encode(h).decode()[:4].replace("O", "Q").replace("I", "J")
            if not self.conn.execute("SELECT 1 FROM reservations WHERE code=?", (code,)).fetchone():
                return code
            salt += 1

    def _lookup(self, ctx: CallContext, code=None, name=None) -> tuple[dict | None, sqlite3.Row | None]:
        c = nlu.normalize_code(code) if code else None
        today = self.clock.now().date().isoformat()
        if c:
            r = self.conn.execute("SELECT * FROM reservations WHERE code=?", (c,)).fetchone()
            if not r:
                return {"error": "not_found", "message": f"No booking with code {spell_code(c)}."}, None
            if r["status"] == "cancelled":
                return {"error": "already_cancelled", "message": "That booking is already cancelled."}, None
            return None, r
        if name:
            rows = [
                r
                for r in self.conn.execute(
                    "SELECT * FROM reservations WHERE status='confirmed' AND date>=? ORDER BY date, time", (today,)
                )
                if nlu.similarity(r["name"], name) >= 0.85
            ]
            if not rows:
                return {"error": "not_found", "message": f"No upcoming booking under {name}."}, None
            mine = [r for r in rows if ctx.caller_phone and r["phone"] == ctx.caller_phone]
            if not mine:
                return {
                    "error": "verification_required",
                    "message": "This phone number doesn't match the booking. Ask for the confirmation code.",
                }, None
            if len(mine) > 1:
                return {
                    "error": "multiple_bookings",
                    "message": "Several bookings match; ask for the code.",
                    "bookings": [self._readback(r) for r in mine],
                }, None
            return None, mine[0]
        return {"error": "missing_identifier", "message": "Ask for the confirmation code."}, None

    def cancel_reservation(self, ctx: CallContext, code=None, name=None) -> dict:
        err, r = self._lookup(ctx, code, name)
        if err:
            return err
        if ctx.low_confidence:
            return {
                "status": "needs_confirmation",
                "readback": self._readback(r),
                "message": "Speech recognition was uncertain. Confirm before cancelling.",
            }
        self.conn.execute(
            "UPDATE reservations SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE code=?", (r["code"],)
        )
        return {"status": "cancelled", "code": r["code"], "readback": self._readback(r)}

    def modify_reservation(
        self, ctx: CallContext, code=None, name=None, new_date=None, new_time=None, new_party_size=None
    ) -> dict:
        err, r = self._lookup(ctx, code, name)
        if err:
            return err
        if not (new_date or new_time or new_party_size):
            return {"error": "nothing_to_change", "message": "Ask what should change."}
        err, slot = self._validate_slot(
            new_date or r["date"],
            new_time or r["time"],
            new_party_size if new_party_size is not None else r["party_size"],
        )
        if err:
            return err
        d, t, p = slot
        if not self._in_service(d, t):
            out = self._closed_reason(d, t)
            out["status"] = "not_changed"
            return out
        table = self._free_table(d, t, p, exclude_code=r["code"])
        if not table:
            alts = self._alternatives(d, t, p, exclude_code=r["code"])
            return {
                "status": "not_changed",
                "reason": "fully_booked",
                "alternatives": alts,
                "alternatives_spoken": [_fmt_time(a) for a in alts],
            }
        if ctx.low_confidence:
            return {
                "status": "needs_confirmation",
                "readback": f"move to {p} people on {_fmt_date(d)} at {_fmt_time(t)}",
                "message": "Speech recognition was uncertain. Confirm before changing.",
            }
        key = f"{r['name'].lower()}|{d}|{t}|{p}"
        self.conn.execute(
            "UPDATE reservations SET date=?, time=?, party_size=?, table_id=?, idempotency_key=?,"
            " updated_at=CURRENT_TIMESTAMP WHERE code=?",
            (d, t, p, table, key, r["code"]),
        )
        new = dict(r) | {"date": d, "time": t, "party_size": p}
        return {"status": "modified", "code": r["code"], "readback": self._readback(new)}

    def handoff_to_human(self, ctx: CallContext, reason=None, summary=None) -> dict:
        reason = (reason or "unspecified").strip()
        ticket = "H-" + hashlib.sha1(f"{ctx.session_id}|{reason}|{time.time_ns()}".encode()).hexdigest()[:5].upper()
        self.conn.execute(
            "INSERT INTO handoffs (ticket, reason, summary, phone) VALUES (?,?,?,?)",
            (ticket, reason, summary, ctx.caller_phone),
        )
        now = self.clock.now()
        open_now = self._in_service(now.date().isoformat(), now.strftime("%H:%M")) or any(
            _hm(s["open"]) - 60 <= now.hour * 60 + now.minute <= _hm(s["close"])
            for s in self._services(now.date().isoformat())
        )
        msg = (
            "Transferring you to a member of staff now."
            if open_now
            else "A member of staff will call you back as soon as we open."
        )
        return {"status": "handoff_created", "ticket": ticket, "message": msg}


# ----------------------------------------------------------------------------------------------
# Resilient executor: timeout + retry + fault injection + auto-handoff for failed writes.
# ----------------------------------------------------------------------------------------------
@dataclass
class ToolCallRecord:
    name: str
    args: dict
    result: dict
    ms: float
    attempts: int


class ToolExecutor:
    def __init__(
        self,
        tools: RestaurantTools,
        timeout_s: float = 2.0,
        retries: int = 2,
        backoff_s: float = 0.2,
        faults: FaultPlan | None = None,
    ):
        self.tools = tools
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.faults = faults or FaultPlan()

    def _invoke(self, name: str, args: dict, ctx: CallContext) -> dict:
        mode = self.faults.check(name)
        if mode == "timeout":
            time.sleep(self.timeout_s + 0.3)  # simulate a hung DB/API call
        elif mode == "error":
            raise ToolError(f"{name}: backend unavailable (injected)")
        fn = getattr(self.tools, name)
        return fn(ctx, **args)

    async def execute(self, name: str, args: dict, ctx: CallContext) -> ToolCallRecord:
        t0 = time.perf_counter()
        if name not in TOOL_NAMES:
            return ToolCallRecord(name, args, {"error": "unknown_tool", "message": f"No tool named {name}."}, 0, 0)
        clean = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        attempts = 0
        last_err = ""
        for attempt in range(self.retries + 1):
            attempts += 1
            try:
                result = await asyncio.wait_for(asyncio.to_thread(self._invoke, name, clean, ctx), self.timeout_s)
                return ToolCallRecord(name, clean, result, (time.perf_counter() - t0) * 1000, attempts)
            except TypeError as e:  # bad argument names from the LLM: not retryable
                return ToolCallRecord(
                    name,
                    clean,
                    {"error": "bad_arguments", "message": str(e)[:200]},
                    (time.perf_counter() - t0) * 1000,
                    attempts,
                )
            except (TimeoutError, ToolError, sqlite3.OperationalError) as e:
                last_err = type(e).__name__ if isinstance(e, asyncio.TimeoutError) else str(e)
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff_s * (2**attempt))
        result = {
            "error": "service_unavailable",
            "retryable": False,
            "message": f"The {name.replace('_', ' ')} system is not responding ({last_err[:80]}).",
        }
        if name in MUTATING:
            # Never lose a write silently: open a callback ticket with everything staff need.
            ho = self.tools.handoff_to_human(ctx, reason=f"{name} failed after retries", summary=f"{name} {clean}")
            result.update(
                {
                    "handoff_created": True,
                    "ticket": ho["ticket"],
                    "message": result["message"] + " A callback ticket was opened for staff to complete it.",
                }
            )
        else:
            result["suggest"] = "apologise; offer handoff_to_human"
        return ToolCallRecord(name, clean, result, (time.perf_counter() - t0) * 1000, attempts)
