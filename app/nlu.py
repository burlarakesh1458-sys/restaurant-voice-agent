"""Deterministic slot parsing. No LLM is needed (or trusted) for any of this.

The LLM extracts *strings* ("7:30", "tonight", "four") into tool arguments; this module turns
them into canonical values (19:30, 2026-11-13, 4). The same functions power the rule-based
baseline agent used in CI and in the "does every part need an LLM?" comparison.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "a couple": 2, "couple": 2, "pair": 2, "a dozen": 12,
    "dozen": 12, "single": 1, "just me": 1, "myself": 1,
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
_MINUTE_WORDS = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30, "forty five": 45,
                 "forty-five": 45, "ten": 10, "twenty": 20, "fifty": 50, "o five": 5}


def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


def parse_party_size(value) -> int | None:
    """4, "4", "four", "table for four", "a couple", "party of 6" -> int."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    s = str(value).strip().lower()
    if s.lstrip("-").isdigit():
        return int(s)
    n = _num(s)
    if n is not None:
        return n
    return find_party_size(s)


_PARTY_PATTERNS = [
    r"(?:table|reservation|booking|book|reserve)\s+(?:a table\s+)?for\s+(\w+(?:\s\w+)?)",
    r"party of\s+(\w+)", r"(\w+)\s+(?:people|persons|guests|adults|of us|pax)",
    r"for\s+(\w+)\s+(?:at|on|tonight|tomorrow|today|this|next)\b",
    r"\bfor\s+(\w+)\b(?!\s*(?:a\.?m|p\.?m|o'clock|:|thirty|fifteen))",
]


def find_party_size(text: str) -> int | None:
    t = text.lower()
    for p in _PARTY_PATTERNS:
        for m in re.finditer(p, t):
            cand = m.group(1).strip()
            for c in (cand, cand.split()[0]):
                n = _num(c)
                if n is not None and 0 <= n <= 200:
                    return n
    if re.search(r"\b(a couple|two of us|me and my (wife|husband|partner|friend))\b", t):
        return 2
    return None


def _to_24h(h: int, m: int, ampm: str | None) -> tuple[int, int] | None:
    if not (0 <= m < 60):
        return None
    if ampm:
        ampm = ampm.replace(".", "").lower()
        if not (1 <= h <= 12):
            return None
        if ampm.startswith("p") and h != 12:
            h += 12
        if ampm.startswith("a") and h == 12:
            h = 0
    elif 1 <= h <= 10:
        h += 12  # a restaurant hears "seven thirty" as the evening
    if not (0 <= h <= 23):
        return None
    return h, m


_TIME_RE = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?=\s|$|[,.?!])", re.IGNORECASE)


def parse_time(value) -> str | None:
    """"19:30", "7:30 pm", "7.30", "half past seven", "seven thirty", "noon" -> "HH:MM"."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        h, m = int(s.split(":")[0]), int(s.split(":")[1])
        if 0 <= h <= 23 and 0 <= m < 60:
            if 1 <= h <= 10:  # "7:30" from a speaker means evening
                h += 12
            return f"{h:02d}:{m:02d}"
        return None
    return find_time(s)


def find_time(text: str) -> str | None:
    t = text.lower().replace("-", " ")
    if re.search(r"\b(noon|midday)\b", t):
        return "12:00"
    if re.search(r"\bmidnight\b", t):
        return "00:00"
    ampm_m = re.search(r"\b(?:in the )?(evening|night|tonight)\b", t)
    m = re.search(r"\b(half|quarter)\s+(past|to)\s+(\w+)", t)
    if m:
        h = _num(m.group(3))
        if h is not None:
            mins = 30 if m.group(1) == "half" else 15
            if m.group(2) == "to":
                h, mins = h - 1, 60 - mins
            r = _to_24h(h, mins, None)
            return f"{r[0]:02d}:{r[1]:02d}" if r else None
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(a\.?m\.?|p\.?m\.?)?", t)
    if m:
        r = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
        return f"{r[0]:02d}:{r[1]:02d}" if r else None
    m = re.search(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?=\W|$)", t)
    if m:
        r = _to_24h(int(m.group(1)), 0, m.group(2))
        return f"{r[0]:02d}:{r[1]:02d}" if r else None
    words = "|".join(k for k in NUM_WORDS if " " not in k and NUM_WORDS[k] <= 12 and NUM_WORDS[k] >= 1)
    m = None
    for prep in ("at|around|by|about", "for"):
        m = re.search(rf"\b(?:{prep})\s+({words}|\d{{1,2}})"
                      rf"(?:\s+(thirty|fifteen|forty five|forty-five|o'clock|oclock))?"
                      rf"(?:\s*(a\.?m\.?|p\.?m\.?))?(?!\s*(?:people|persons|guests|of us|pax))\b", t)
        if m and prep == "for" and not (m.group(2) or m.group(3)):
            m = None  # "for two" is a party size unless it carries a time marker ("for 8 pm")
        if m:
            break
    if m:
        h = _num(m.group(1))
        mins = _MINUTE_WORDS.get(m.group(2) or "o'clock", 0)
        if h is not None:
            ampm = m.group(3)
            r = _to_24h(h, mins, ampm)
            if r:
                return f"{r[0]:02d}:{r[1]:02d}"
    m = re.search(rf"\b({words})\s+(thirty|fifteen|forty five|o'clock|oclock)\b", t)
    if m:
        r = _to_24h(_num(m.group(1)), _MINUTE_WORDS[m.group(2)], None)
        return f"{r[0]:02d}:{r[1]:02d}" if r else None
    _ = ampm_m
    return None


def parse_date(value, now: datetime) -> str | None:
    """"2026-11-13", "today", "tonight", "tomorrow", "saturday", "next friday", "14 november" -> ISO."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return None
    return find_date(s, now)


def find_date(text: str, now: datetime) -> str | None:
    t = text.lower()
    today = now.date()
    if re.search(r"\bday after tomorrow\b", t):
        return (today + timedelta(days=2)).isoformat()
    if re.search(r"\b(tomorrow|tmrw|tomorow)\b", t):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\b(today|tonight|this evening|this afternoon|now)\b", t):
        return today.isoformat()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None
    mon = "|".join(MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+({mon})\b", t) or \
        re.search(rf"\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.groups()
        day, month = (int(a), b) if a.isdigit() else (int(b), a)
        y = today.year
        try:
            d = date(y, MONTHS.index(month) + 1, day)
        except ValueError:
            return None
        if d < today - timedelta(days=60):
            d = date(y + 1, d.month, d.day)
        return d.isoformat()
    m = re.search(r"\b(next|this|coming)?\s*(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(2))
        delta = (wd - today.weekday()) % 7
        if m.group(1) == "next" and delta == 0:
            delta = 7
        return (today + timedelta(days=delta)).isoformat()
    if re.search(r"\b(yesterday)\b", t):
        return (today - timedelta(days=1)).isoformat()
    return None


_CODE_RE = re.compile(r"\bT\s*-?\s*V\s*-?\s*([A-Z0-9](?:[\s-]*[A-Z0-9]){3})\b", re.IGNORECASE)
_SPOKEN_CHARS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def normalize_code(value) -> str | None:
    """"tv-d4f8", "T V D 4 F 8", "TV D four F eight" -> "TV-D4F8"."""
    if not value:
        return None
    s = str(value).upper()
    for w, d in _SPOKEN_CHARS.items():
        s = re.sub(rf"\b{w.upper()}\b", d, s)
    m = _CODE_RE.search(s)
    if not m:
        compact = re.sub(r"[^A-Z0-9]", "", s)
        if re.fullmatch(r"TV[A-Z0-9]{4}", compact):
            return f"TV-{compact[2:]}"
        return None
    return "TV-" + re.sub(r"[\s-]", "", m.group(1)).upper()


def normalize_phone(value) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None


_NAME_RE = re.compile(
    r"\b(?i:my name is|name is|this is|it's|it is|under(?: the name)?|name's|i am|i'm)\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
_NOT_NAMES = {"Looking", "Calling", "Allergic", "Sorry", "Here", "Just", "Not", "Going", "Trying", "Wondering",
              "Hoping", "Interested", "Booking", "So", "Very", "Really", "The"}


def find_name(text: str) -> str | None:
    m = _NAME_RE.search(text)
    if m:
        name = m.group(1).strip()
        if name.split()[0] not in _NOT_NAMES:
            return name
    m = re.search(r"\bname(?: is|:)?\s+([a-z]+(?:\s+[a-z]+)?)\b", text, re.IGNORECASE)
    if m and m.group(1).split()[0].capitalize() not in _NOT_NAMES:
        return m.group(1).title()
    return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z']+", s.lower())
