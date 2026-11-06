import asyncio

from app.agent import Agent
from app.factory import build_agent
from app.llm import LLMError
from app.tools import ToolExecutor


def make(tools, clock, llm, **kw):
    return Agent(llm, tools, ToolExecutor(tools, timeout_s=0.5, backoff_s=0.01), clock,
                 caller_phone="+919811111111", **kw)


def test_rules_agent_multi_intent(clock):
    a = build_agent("rules", clock)
    r = asyncio.run(a.handle_turn("Can I reserve a table for four at 7:30 tonight? Also, does the pesto contain nuts?"))
    names = [c.name for c in r.tool_calls]
    assert "get_allergens" in names and "check_availability" in names
    assert "nuts" in r.text and "name" in r.text.lower()
    r2 = asyncio.run(a.handle_turn("It's Priya Sharma."))
    assert r2.tool_calls[0].name == "create_reservation" and r2.tool_calls[0].result["status"] == "confirmed"


def test_tool_loop_and_history(tools, clock, scripted):
    llm = scripted([[("get_allergens", {"dish": "pesto", "allergen": "nuts"})], "The pesto contains pine nuts."])
    a = make(tools, clock, llm)
    r = asyncio.run(a.handle_turn("does the pesto have nuts"))
    assert r.text == "The pesto contains pine nuts." and not r.guard_rewrote
    roles = [m["role"] for m in a.history]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_guard_rewrites_hallucinated_allergen_claim(tools, clock, scripted):
    llm = scripted([[("get_allergens", {"dish": "risotto"})], "Good news: the risotto is nut-free!"])
    r = asyncio.run(make(tools, clock, llm).handle_turn("is the risotto nut free?"))
    assert r.guard_rewrote and "can't confirm" in r.text and "nut-free" not in r.text
    assert r.guard_violations_draft[0]["verified"] == "unknown"


def test_llm_outage_degrades_to_callback(tools, clock, scripted):
    llm = scripted([LLMError("down", retryable=False)])
    r = asyncio.run(make(tools, clock, llm).handle_turn("table for two"))
    assert r.degraded and r.handoff and "call you back" in r.text


def test_low_confidence_reprompts_without_llm(tools, clock, scripted):
    llm = scripted([])
    r = asyncio.run(make(tools, clock, llm).handle_turn("uh mmm", asr_confidence=0.2))
    assert r.clarified and not llm.seen and "say it again" in r.text


def test_medium_confidence_flags_and_gates(tools, clock, scripted):
    llm = scripted([[("create_reservation", {"name": "Rohan", "party_size": 4, "date": "tonight", "time": "7:30"})],
                    "Just to confirm, four people at 7:30 tonight under Rohan?"])
    r = asyncio.run(make(tools, clock, llm).handle_turn("book for for at seven thirty", asr_confidence=0.55))
    assert r.tool_calls[0].result["status"] == "needs_confirmation"
    assert "confidence 0.55" in llm.seen[0][-1]["content"]


def test_hybrid_mode_uses_one_llm_call(tools, clock, scripted):
    from app.factory import dish_aliases
    from app.llm import RuleBasedLLM
    llm = scripted([[("check_availability", {"date": "saturday", "time": "8 pm", "party_size": 2})]])
    a = make(tools, clock, llm, composer=RuleBasedLLM(dish_aliases(tools), clock.now))
    r = asyncio.run(a.handle_turn("table for two saturday 8pm?"))
    assert len(llm.seen) == 1 and "10 PM" in r.text


def test_mark_interrupted(tools, clock, scripted):
    a = make(tools, clock, scripted(["We have bruschetta, burrata and arancini."]))
    asyncio.run(a.handle_turn("what starters do you have"))
    a.mark_interrupted("We have bruschetta ...")
    assert a.history[-1]["content"].startswith("We have bruschetta ...") and "interrupted" in a.history[-1]["content"]
