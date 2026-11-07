import asyncio

from app.tools import CallContext, FaultPlan, ToolExecutor


def test_available_slot(tools, ctx):
    r = tools.check_availability(ctx, "tonight", "7:30 pm", 4)
    assert r["available"] and r["date"] == "2026-11-13" and r["time"] == "19:30"


def test_fully_booked_offers_alternatives(tools, ctx):
    r = tools.check_availability(ctx, "tonight", "8 pm", 4)
    assert not r["available"] and r["reason"] == "fully_booked"
    assert r["alternatives"] and all(
        tools.check_availability(ctx, "tonight", a, 4)["available"] for a in r["alternatives"]
    )


def test_closed_monday_and_outside_hours(tools, ctx):
    assert tools.check_availability(ctx, "monday", "8 pm", 2)["reason"] == "closed"
    r = tools.check_availability(ctx, "tonight", "11:30 pm", 2)
    assert r["reason"] == "outside_hours" and "22:00" in r["alternatives"]


def test_rejects_past_large_and_zero(tools, ctx):
    assert tools.check_availability(ctx, "today", "2 pm", 2)["error"] == "time_in_past"
    assert tools.create_reservation(ctx, "A", 14, "saturday", "8 pm")["error"] == "large_party"
    assert tools.create_reservation(ctx, "A", 0, "tonight", "8 pm")["error"] == "invalid_party_size"
    assert tools.create_reservation(ctx, "", 2, "tonight", "8 pm")["error"] == "missing_name"


def test_create_is_idempotent(tools, ctx):
    a = tools.create_reservation(ctx, "Priya Sharma", 4, "tonight", "7:30 pm")
    b = tools.create_reservation(ctx, "priya sharma", "four", "2026-11-13", "19:30")
    assert a["status"] == "confirmed" and b["duplicate"] and a["code"] == b["code"]
    n = tools.conn.execute("SELECT COUNT(*) FROM reservations WHERE name LIKE 'priya%'").fetchone()[0]
    assert n == 1


def test_low_confidence_gates_writes(tools):
    ctx = CallContext(caller_phone="+919811111111", low_confidence=True)
    r = tools.create_reservation(ctx, "Rohan", 4, "tonight", "7:30")
    assert r["status"] == "needs_confirmation"
    assert tools.conn.execute("SELECT COUNT(*) FROM reservations WHERE name='Rohan'").fetchone()[0] == 0


def test_cancel_requires_code_or_matching_phone(tools, ctx):
    assert tools.cancel_reservation(ctx, name="Daniel Fernandes")["error"] == "verification_required"
    assert (
        tools.cancel_reservation(CallContext(caller_phone="+919800000101"), name="Daniel Fernandes")["status"]
        == "cancelled"
    )
    assert tools.cancel_reservation(ctx, code="TV-D4F8")["error"] == "already_cancelled"


def test_modify_time_and_party(tools):
    ctx = CallContext(caller_phone="+919800000110")
    r = tools.modify_reservation(ctx, code="tv v3m9", new_time="2 pm")
    assert r["status"] == "modified"
    row = tools.conn.execute("SELECT * FROM reservations WHERE code='TV-V3M9'").fetchone()
    assert (row["time"], row["party_size"]) == ("14:00", 6)


def test_allergen_verified_contains_and_free(tools, ctx):
    r = tools.get_allergens(ctx, "pesto", "nuts")
    assert r["verified"] and r["query"] == {"tree nuts": "contains", "peanuts": "free"}
    assert tools.get_allergens(ctx, "penne arrabbiata", "dairy")["query"] == {"milk": "free"}


def test_allergen_missing_unverified_and_gap(tools, ctx):
    assert tools.get_allergens(ctx, "risotto", "nuts")["verified"] is False
    assert tools.get_allergens(ctx, "lemon sorbet")["verified"] is False
    gap = tools.get_allergens(ctx, "margherita", "sesame")
    assert gap["query"] == {"sesame": "unknown"} and "can't confirm" in gap["answer"]


def test_allergen_fuzzy_never_answers(tools, ctx):
    r = tools.get_allergens(ctx, "presto", "nuts")
    assert r["did_you_mean"] == "Trofie al Pesto Genovese" and "answer" not in r


def test_free_of_search_excludes_unverified(tools, ctx):
    names = {d["name"] for d in tools.search_menu(ctx, query="dessert", free_of="nuts")["dishes"]}
    assert "Tiramisu" in names and "Sorbetto al Limone" not in names and "Torta Caprese" not in names


def test_executor_retries_transient_failure(tools, ctx):
    ex = ToolExecutor(tools, timeout_s=0.5, backoff_s=0.01, faults=FaultPlan({"check_availability": ("error", 1)}))
    rec = asyncio.run(ex.execute("check_availability", {"date": "tonight", "time": "7:30", "party_size": 2}, ctx))
    assert rec.attempts == 2 and rec.result["available"]


def test_executor_persistent_write_failure_opens_ticket(tools, ctx):
    ex = ToolExecutor(tools, timeout_s=0.5, backoff_s=0.01, faults=FaultPlan({"create_reservation": ("error", 99)}))
    rec = asyncio.run(
        ex.execute("create_reservation", {"name": "M", "party_size": 2, "date": "sunday", "time": "1 pm"}, ctx)
    )
    assert rec.result["error"] == "service_unavailable" and rec.result["handoff_created"]
    assert tools.conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


def test_executor_timeout_and_bad_args(tools, ctx):
    ex = ToolExecutor(
        tools, timeout_s=0.2, retries=1, backoff_s=0.01, faults=FaultPlan({"get_allergens": ("timeout", 9)})
    )
    rec = asyncio.run(ex.execute("get_allergens", {"dish": "tiramisu"}, ctx))
    assert rec.result["error"] == "service_unavailable" and rec.attempts == 2
    assert asyncio.run(ex.execute("search_menu", {"colour": "red"}, ctx)).result["error"] == "bad_arguments"
    assert asyncio.run(ex.execute("drop_tables", {}, ctx)).result["error"] == "unknown_tool"
