import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OMP_NUM_THREADS", "1")

from app.clock import FixedClock  # noqa: E402
from app.db import seed  # noqa: E402
from app.llm import LLMResponse, ToolCall  # noqa: E402
from app.tools import CallContext, RestaurantTools  # noqa: E402

NOW = "2026-11-13T16:00:00+05:30"  # Friday


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def tools(clock):
    return RestaurantTools(seed(":memory:", date(2026, 11, 13)), clock)


@pytest.fixture
def ctx():
    return CallContext(caller_phone="+919811111111")


class ScriptedLLM:
    """Returns pre-baked responses in order; records what it was sent."""

    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    async def complete(self, messages, tools=None, tool_choice="auto"):
        self.seen.append(messages)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, str):
            return LLMResponse(r, model="scripted")
        return LLMResponse("", [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(r)], model="scripted")


@pytest.fixture
def scripted():
    return ScriptedLLM
