"""Wiring helpers shared by the server, the eval runner and the tests."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from .agent import Agent
from .clock import Clock
from .db import seed
from .llm import FallbackLLM, GroqLLM, ResponseCache, RuleBasedLLM
from .tools import FaultPlan, RestaurantTools, ToolExecutor

ROOT = Path(__file__).resolve().parents[1]


def dish_aliases(tools: RestaurantTools) -> dict[str, str]:
    out = {}
    for r in tools.conn.execute("SELECT name, aliases FROM dishes"):
        out[r["name"].lower()] = r["name"]
        for a in (r["aliases"] or "").split("|"):
            if a:
                out[a] = r["name"]
    return out


def make_llm(kind: str, tools: RestaurantTools, clock: Clock, cache_path: str | None = None):
    if kind == "rules":
        return RuleBasedLLM(dish_aliases(tools), clock.now)
    if kind.startswith("ollama:"):  # local model on CPU via Ollama's OpenAI-compatible API
        url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1")
        return FallbackLLM([GroqLLM(kind.split(":", 1)[1], api_key="ollama", base_url=url, timeout_s=600,
                                    cache=ResponseCache(cache_path) if cache_path else None, max_retries=3)])
    cache = ResponseCache(cache_path) if cache_path else None
    models = [m.strip() for m in os.environ.get("LLM_MODELS", "openai/gpt-oss-20b").split(",") if m.strip()]
    if kind != "groq":
        models = [kind]
    return FallbackLLM([GroqLLM(m, cache=cache) for m in models])


def build_agent(llm_kind: str, clock: Clock, db_path: str = ":memory:", caller_phone: str | None = None,
                faults: FaultPlan | None = None, base_date: date | None = None, cache_path: str | None = None,
                llm=None, tool_timeout_s: float = 2.0, hybrid: bool = False) -> Agent:
    conn = seed(db_path, base_date or clock.now().date())
    tools = RestaurantTools(conn, clock)
    executor = ToolExecutor(tools, timeout_s=tool_timeout_s, faults=faults)
    llm = llm or make_llm(llm_kind, tools, clock, cache_path)
    composer = RuleBasedLLM(dish_aliases(tools), clock.now) if hybrid else None
    return Agent(llm, tools, executor, clock, caller_phone=caller_phone, composer=composer)
