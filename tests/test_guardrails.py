import pytest

from app.guardrails import AllergenGuard


@pytest.mark.parametrize(
    "text,bad",
    [
        ("The Trofie al Pesto Genovese contains pine nuts, so it is not safe for a nut allergy.", False),
        ("Good news, the pesto is nut-free!", True),
        ("The risotto has no nuts.", True),
        ("I can't confirm whether the Risotto del Giorno contains nuts.", False),
        ("I can\u2019t confirm whether the risotto contains nuts.", False),
        ("The pesto pasta contains tree nuts and is verified free of peanuts.", False),
        ("The tiramisu contains eggs and dairy.", False),
        ("The margherita pizza is sesame free.", True),
        ("The lemon sorbet is vegan and has no nuts.", True),
        ("The panna cotta does not contain peanuts.", True),
        ("The calamari is safe for a shellfish allergy.", True),
        ("I have noted your nut allergy on the booking.", False),
        ("Your table for 4 is booked for 7:30 PM.", False),
    ],
)
def test_claim_detection(tools, text, bad):
    assert bool(AllergenGuard(tools).find_violations(text)) == bad


def test_rewrite_replaces_only_bad_clause(tools):
    g = AllergenGuard(tools)
    text = "Your table for four is booked. The risotto is nut-free."
    out = g.safe_rewrite(text, g.find_violations(text))
    assert "booked" in out and "can't confirm" in out and "nut-free" not in out
    assert not g.find_violations(out)


def test_context_carries_dish_reference(tools):
    g = AllergenGuard(tools)
    assert g.find_violations("Yes, it has no peanuts.", context="Is the panna cotta ok for peanuts?")
