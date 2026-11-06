"""LLM providers with tool calling.

* ``GroqLLM``      OpenAI-compatible chat completions (Groq by default) with retry/Retry-After,
                   an on-disk response cache (so a rate-limited eval run can resume) and usage stats.
* ``FallbackLLM``  tries providers in order.
* ``RuleBasedLLM`` a deterministic intent + slot policy that speaks the same tool-calling protocol.
                   It is the fake LLM for offline CI *and* the "no LLM" baseline in the eval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import nlu
from .tools import ALLERGEN_WORDS


class LLMError(Exception):
    def __init__(self, msg: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(msg)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                                 for tc in self.tool_calls]
        return msg


_TRY_AGAIN = re.compile(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", re.I)


def _retry_after(headers, body: str) -> float | None:
    v = headers.get("retry-after") if headers else None
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    m = _TRY_AGAIN.search(body or "")
    if m:
        mins, val, unit = m.groups()
        return float(val) / (1000 if unit == "ms" else 1) + 60 * int(mins or 0)
    return None


class ResponseCache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")

    def get(self, k: str) -> dict | None:
        r = self.conn.execute("SELECT v FROM cache WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, k: str, v: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (k, json.dumps(v)))
        self.conn.commit()


class GroqLLM:
    def __init__(self, model: str = "openai/gpt-oss-20b", api_key: str | None = None,
                 base_url: str = "https://api.groq.com/openai/v1", cache: ResponseCache | None = None,
                 timeout_s: float = 30.0, max_retries: int = 6, max_wait_s: float = 90.0,
                 temperature: float = 0.0, max_tokens: int = 700):
        self.model = model
        self.name = f"groq:{model}"
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.max_wait_s = max_wait_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.usage = {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0, "retries": 0}
        self._client = httpx.AsyncClient(timeout=timeout_s)

    def _payload(self, messages: list[dict], tools: list[dict] | None, tool_choice: str) -> dict:
        p = {"model": self.model, "messages": messages, "temperature": self.temperature,
             "max_completion_tokens": self.max_tokens}
        if tools:
            p["tools"] = tools
            p["tool_choice"] = tool_choice
            p["parallel_tool_calls"] = True
        if "gpt-oss" in self.model:
            p["reasoning_effort"] = "low"
            p["include_reasoning"] = False
        elif "qwen3" in self.model:
            p["reasoning_format"] = "hidden"
            p["reasoning_effort"] = "none"
        return p

    async def complete(self, messages: list[dict], tools: list[dict] | None = None,
                       tool_choice: str = "auto") -> LLMResponse:
        payload = self._payload(messages, tools, tool_choice)
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if self.cache and (hit := self.cache.get(key)):
            self.usage["cached"] += 1
            return self._parse(hit, 0.0, cached=True)
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                r = await self._client.post(f"{self.base_url}/chat/completions", json=payload,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                last = LLMError(f"transport: {type(e).__name__}")
                await asyncio.sleep(min(2 ** attempt, 8))
                self.usage["retries"] += 1
                continue
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code == 200:
                data = r.json()
                self.usage["calls"] += 1
                u = data.get("usage") or {}
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                self.usage["completion_tokens"] += u.get("completion_tokens", 0)
                self.usage["cached_prompt_tokens"] = self.usage.get("cached_prompt_tokens", 0) + \
                    ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                if self.cache:
                    self.cache.put(key, data)
                return self._parse(data, ms)
            body = r.text
            if r.status_code == 429 or r.status_code >= 500:
                wait = _retry_after(r.headers, body) or min(2 ** attempt, 20)
                if "tokens per day" in body or "requests per day" in body or wait > self.max_wait_s:
                    raise LLMError(f"{self.name} daily quota exhausted; Please try again in {wait:.1f}s",
                                   retryable=False, retry_after=wait)
                last = LLMError(f"HTTP {r.status_code}", retry_after=wait)
                self.usage["retries"] += 1
                await asyncio.sleep(wait + 0.25)
                continue
            if r.status_code == 400 and "tool_use_failed" in body and attempt < 2:
                # model emitted a malformed tool call; one retry with a little temperature usually fixes it
                payload = dict(payload, temperature=0.3)
                self.usage["retries"] += 1
                last = LLMError("tool_use_failed")
                continue
            raise LLMError(f"{self.name} HTTP {r.status_code}: {body[:300]}", retryable=False)
        raise last or LLMError("unknown")

    def _parse(self, data: dict, ms: float, cached: bool = False) -> LLMResponse:
        msg = data["choices"][0]["message"]
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc["function"].get("arguments")}
            calls.append(ToolCall(tc.get("id") or f"call_{i}", tc["function"]["name"], args))
        u = data.get("usage") or {}
        return LLMResponse(msg.get("content") or "", calls, self.model, ms, u.get("prompt_tokens", 0),
                           u.get("completion_tokens", 0), cached)


class FallbackLLM:
    """Try providers in order. A provider that hits its quota cools down for its Retry-After, then rejoins."""

    def __init__(self, providers: list):
        self.providers = providers
        self.name = "->".join(p.name for p in providers)
        self.cooldown_until: dict[str, float] = {}

    async def complete(self, messages, tools=None, tool_choice="auto") -> LLMResponse:
        err: LLMError | None = None
        now = time.monotonic()
        for p in self.providers:
            if self.cooldown_until.get(p.name, 0) > now:
                continue
            try:
                return await p.complete(messages, tools, tool_choice)
            except LLMError as e:
                err = e
                if "quota" in str(e):
                    self.cooldown_until[p.name] = time.monotonic() + (e.retry_after or 300)
        if err is None:  # everyone is cooling down
            wait = min(self.cooldown_until.values()) - now
            err = LLMError(f"all providers quota exhausted; Please try again in {max(1.0, wait):.1f}s",
                           retryable=False, retry_after=wait)
        raise err


class FaultyLLM:
    """Wraps an LLM and fails the first ``n`` calls (timeout or error) for resilience tests."""

    def __init__(self, inner, n: int = 1, mode: str = "error"):
        self.inner, self.n, self.mode = inner, n, mode
        self.name = f"faulty({inner.name})"

    async def complete(self, messages, tools=None, tool_choice="auto"):
        if self.n > 0:
            self.n -= 1
            if self.mode == "timeout":
                await asyncio.sleep(0.05)
            raise LLMError(f"injected {self.mode}", retryable=False)
        return await self.inner.complete(messages, tools, tool_choice)


# ==============================================================================================
# Rule-based policy (no LLM). Same protocol as GroqLLM.
# ==============================================================================================
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|correct|that's right|that is right|right|sure|please do|go ahead|"
                  r"confirm|perfect|ok|okay|sounds good|book it)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|not quite|wrong|that's wrong)\b", re.I)
_HUMAN = re.compile(r"\b(human|real person|a person|manager|staff member|someone|speak to|talk to|transfer)\b", re.I)
_COMPLAINT = re.compile(
    r"\b(complain|complaint|terrible|awful|food poisoning|sick after|rude|refund|disgusting)\b", re.I)
_CANCEL = re.compile(r"\bcancel\b", re.I)
_MODIFY = re.compile(r"\b(change|move|modify|push|reschedule|instead|make it)\b", re.I)
_BOOK = re.compile(r"\b(book|reserve|reservation|table for|a table|booking)\b", re.I)
_MENU = re.compile(r"\b(menu|vegetarian|vegan|dessert|desserts|pasta|pizza|what do you (have|serve)|recommend|"
                   r"dishes|options|starters?)\b", re.I)
_HOURS = re.compile(r"\b(open|close|closing|hours|opening)\b", re.I)


class RuleBasedLLM:
    name = "rules:v1"

    def __init__(self, dish_aliases: dict[str, str], now_fn):
        self.dish_aliases = sorted(dish_aliases.items(), key=lambda x: len(x[0]), reverse=True)
        self.now_fn = now_fn
        self.usage = {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0, "retries": 0}
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"rb_{self._n}"

    def _find_dish(self, text: str) -> str | None:
        t = text.lower()
        for alias, _name in self.dish_aliases:
            if re.search(r"\b" + re.escape(alias) + r"\b", t):
                return alias
        m = re.search(r"\b(?:the|does|is|in)\s+([a-z]+)\s+(?:contain|have|has|safe|got)", t)
        return m.group(1) if m else None

    @staticmethod
    def _find_allergen(text: str) -> str | None:
        t = text.lower()
        for w in sorted(ALLERGEN_WORDS, key=len, reverse=True):
            if re.search(r"\b" + re.escape(w) + r"\b", t):
                return w
        if re.search(r"\b(allerg|allergen)", t):
            return "any"
        return None

    def _state(self, messages: list[dict]) -> dict:
        now = self.now_fn()
        st = {"party": None, "date": None, "time": None, "name": None, "code": None, "booking": False,
              "booked": False, "pending_confirm": None}
        for m in messages:
            if m["role"] == "user":
                text = re.sub(r"^\[[^\]]*\]\s*", "", m["content"] or "")
                st["party"] = nlu.find_party_size(text) or st["party"]
                st["date"] = nlu.find_date(text, now) or st["date"]
                st["time"] = nlu.find_time(text) or st["time"]
                st["name"] = nlu.find_name(text) or st["name"]
                st["code"] = nlu.normalize_code(text) or st["code"]
                if _BOOK.search(text) and not _CANCEL.search(text):
                    st["booking"] = True
                if re.fullmatch(r"\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\.?\s*", text) and st["booking"]:
                    st["name"] = text.strip().rstrip(".")
            elif m["role"] == "tool":
                try:
                    r = m.get("_result") or json.loads(m["content"])
                except Exception:
                    continue
                if r.get("status") in ("confirmed",):
                    st["booked"] = True
                    st["booking"] = False
                if r.get("status") == "needs_confirmation":
                    st["pending_confirm"] = True
                elif r.get("status") in ("confirmed", "cancelled", "modified"):
                    st["pending_confirm"] = None
        return st

    async def complete(self, messages, tools=None, tool_choice="auto") -> LLMResponse:
        self.usage["calls"] += 1
        await asyncio.sleep(0)
        last = messages[-1]
        if last["role"] == "tool":
            return self._compose(messages)
        if tool_choice == "none":
            return LLMResponse("Sorry, could you say that again?", model=self.name)
        return self._plan(messages)

    def _plan(self, messages) -> LLMResponse:
        user = re.sub(r"^\[[^\]]*\]\s*", "", messages[-1]["content"] or "")
        st = self._state(messages)
        prev_assistant = next((m.get("content") or "" for m in reversed(messages[:-1])
                               if m["role"] == "assistant" and m.get("content")), "")
        calls: list[ToolCall] = []

        def call(tool_name, **args):
            calls.append(ToolCall(self._id(), tool_name, {k: v for k, v in args.items() if v is not None}))

        if _COMPLAINT.search(user):
            call("handoff_to_human", reason="complaint", summary=user[:200])
        elif _HUMAN.search(user) and re.search(r"\b(speak|talk|transfer|connect|get me|want|need)\b", user, re.I) \
                or (_YES.search(user) and re.search(r"staff member|connect you", prev_assistant, re.I)):
            call("handoff_to_human", reason="caller requested staff", summary=user[:200])
        elif _CANCEL.search(user):
            call("cancel_reservation", code=st["code"], name=None if st["code"] else st["name"])
        elif _MODIFY.search(user) and (st["code"] or re.search(r"\b(my|the) (booking|reservation)\b", user, re.I)):
            call("modify_reservation", code=st["code"], name=None if st["code"] else st["name"],
                 new_time=nlu.find_time(user), new_party_size=nlu.find_party_size(user),
                 new_date=nlu.find_date(user, self.now_fn()))
        else:
            allergen = self._find_allergen(user)
            dish = self._find_dish(user)
            if allergen and re.search(r"\b(contain\w*|have|has|in it|safe|allerg\w*|free|got|eat)\b", user, re.I):
                if dish:
                    call("get_allergens", dish=dish, allergen=None if allergen == "any" else allergen)
                elif not _BOOK.search(user):
                    return LLMResponse("Which dish would you like me to check?", model=self.name)
            elif dish and re.search(r"\b(did you mean|yes|i meant)\b", user, re.I):
                pass
            booking_now = _BOOK.search(user) or (st["booking"] and (
                nlu.find_time(user) or nlu.find_party_size(user) or nlu.find_date(user, self.now_fn())
                or nlu.find_name(user) or _YES.search(user) or
                re.fullmatch(r"\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\.?\s*", user)))
            if booking_now and not st["booked"]:
                date = st["date"] or ("today" if st["time"] else None)
                if st["party"] and date and st["time"]:
                    if st["name"]:
                        call("create_reservation", name=st["name"], party_size=st["party"], date=date,
                             time=st["time"])
                    else:
                        call("check_availability", date=date, time=st["time"], party_size=st["party"])
                elif not calls:
                    missing = [k for k, v in (("how many people", st["party"]), ("what day", date),
                                              ("what time", st["time"])) if not v]
                    return LLMResponse(f"Happy to book that. Could you tell me {' and '.join(missing)}?",
                                       model=self.name)
            if not calls:
                if _HOURS.search(user):
                    call("get_opening_hours", date=nlu.find_date(user, self.now_fn()))
                elif _MENU.search(user):
                    veg = bool(re.search(r"\bvegetarian\b", user, re.I)) or None
                    vegan = bool(re.search(r"\bvegan\b", user, re.I)) or None
                    q = next((w for w in ("dessert", "pasta", "pizza", "antipasti", "salad", "soup")
                              if w in user.lower()), None)
                    call("search_menu", query=q, vegetarian=veg, vegan=vegan)
        if calls:
            return LLMResponse("", calls, model=self.name)
        return LLMResponse("I can help with table bookings, our menu, opening hours and allergen questions. "
                           "What would you like to do?", model=self.name)

    def _compose(self, messages) -> LLMResponse:
        results = []
        for m in reversed(messages):
            if m["role"] != "tool":
                break
            results.append((m.get("name", ""), m.get("_result") or json.loads(m["content"])))
        results.reverse()
        st = self._state(messages)
        parts: list[str] = []
        follow: list[ToolCall] = []
        for name, r in results:
            if r.get("error") == "large_party":
                follow.append(ToolCall(self._id(), "handoff_to_human",
                                       {"reason": f"party of {r.get('party_size')}"}))
                continue
            if r.get("error") == "service_unavailable":
                if r.get("handoff_created"):
                    parts.append("Sorry, our booking system isn't responding. I've opened a callback request so a "
                                 "staff member can finish it and call you back.")
                else:
                    parts.append("Sorry, that system isn't responding right now. I can connect you with a "
                                 "staff member if you like.")
                continue
            if "error" in r:
                parts.append(r.get("message", "Sorry, something went wrong."))
                continue
            if name == "check_availability":
                if r.get("available"):
                    parts.append(f"Good news, we have a table {r['spoken']}.")
                    if st["booking"] and not st["name"]:
                        parts.append("May I have a name for the booking?")
                elif r.get("reason") == "fully_booked":
                    parts.append(f"Sorry, that time is fully booked. I can offer "
                                 f"{', '.join(r.get('alternatives_spoken') or []) or 'another day'}.")
                else:
                    parts.append(r.get("message", "Sorry, we can't seat you then."))
            elif name == "create_reservation":
                if r.get("status") == "confirmed":
                    parts.append(f"You're booked: {r['readback']}.")
                elif r.get("status") == "needs_confirmation":
                    parts.append(f"Just to confirm, {r['readback']}. Is that right?")
                elif r.get("reason") == "fully_booked":
                    parts.append(f"Sorry, that time is fully booked. I can offer "
                                 f"{', '.join(r.get('alternatives_spoken') or []) or 'another day'}.")
                else:
                    parts.append(r.get("message", "Sorry, I couldn't book that."))
            elif name == "get_allergens":
                if r.get("did_you_mean"):
                    parts.append(f"Did you mean the {r['did_you_mean']}?")
                elif r.get("ambiguous"):
                    parts.append(f"Which dish do you mean: {' or '.join(r['candidates'])}?")
                elif r.get("not_found"):
                    parts.append(r["message"])
                else:
                    parts.append(r["answer"])
                    for o in r.get("other_matching_dishes", []):
                        parts.append(o["answer"])
            elif name == "search_menu":
                names = [d["name"] for d in r.get("dishes", [])[:4]]
                parts.append(f"We have {', '.join(names)}." if names else "I couldn't find a dish like that.")
            elif name == "get_opening_hours":
                if r.get("closed"):
                    parts.append(f"We're closed on {r['weekday']}.")
                elif "services" in r:
                    parts.append(f"On {r['weekday']} we're open " + " and ".join(
                        f"{s['open']} to {s['close']}" for s in r["services"]) + ".")
                else:
                    parts.append("We're open Tuesday to Sunday for lunch and dinner, and closed on Mondays.")
            elif name in ("cancel_reservation", "modify_reservation"):
                if r.get("status") == "cancelled":
                    parts.append(f"Done, I've cancelled {r['readback']}.")
                elif r.get("status") == "modified":
                    parts.append(f"Done, it's now {r['readback']}.")
                elif r.get("status") == "needs_confirmation":
                    parts.append(f"Just to confirm, {r['readback']}. Is that right?")
                else:
                    parts.append(r.get("message", "Sorry, I couldn't change that."))
            elif name == "handoff_to_human":
                parts.append(r.get("message", "I'm getting a member of staff for you."))
        if follow:
            return LLMResponse("", follow, model=self.name)
        return LLMResponse(" ".join(parts) or "Is there anything else?", model=self.name)
