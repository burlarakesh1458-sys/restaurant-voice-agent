import os
import sys

os.environ["LLM_KIND"] = "rules"
os.environ["TTS"] = "fake"
os.environ["ASR"] = "fake"
os.environ["FIXED_NOW"] = "2026-11-13T16:00:00+05:30"

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402


def test_health_and_menu():
    c = TestClient(app)
    assert c.get("/health").json()["ok"]
    menu = c.get("/api/menu").json()
    assert len(menu) == 25 and {m["allergen_info_status"] for m in menu} == {"verified", "unverified", "missing"}


def test_websocket_text_turn():
    c = TestClient(app)
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start"})
        assert ws.receive_json()["type"] == "started"
        ws.send_json({"type": "text", "text": "Does the panna cotta contain peanuts?"})
        seen = []
        while True:
            e = ws.receive_json()
            seen.append(e)
            if e["type"] == "turn_done":
                break
        reply = next(e for e in seen if e["type"] == "reply")
        assert "may contain traces of peanuts" in reply["text"]
        assert any(e["type"] == "audio" for e in seen)


def test_eval_harness_offline(monkeypatch, tmp_path):
    import asyncio

    sys.argv = [
        "run_eval",
        "--llm",
        "rules",
        "--tts",
        "fake",
        "--only",
        "B03,A03,R02,R06,R07,C03",
        "--tag",
        "pytest_dev",
        "--fresh",
    ]
    from scripts import run_eval

    asyncio.run(run_eval.main())
    import json

    out = run_eval.ROOT / "reports" / "results_pytest_dev.json"
    s = json.loads(out.read_text())["summary"]
    out.unlink()
    assert s["scenarios"] == 6 and s["allergen_guardrail"]["final_reply_violations"] == 0
    assert s["handoff"]["fn"] == 0 and s["task_completion_rate"] == 1.0


def test_arg_matching_normalises(tools, clock):
    from scripts.run_eval import call_matches

    exp = {
        "tool": "create_reservation",
        "args": {"name": "Priya Sharma", "party_size": 4, "date": "2026-11-13", "time": "19:30"},
    }
    act = {
        "name": "create_reservation",
        "args": {"name": "priya sharma", "party_size": "four", "date": "tonight", "time": "7:30 PM"},
    }
    assert call_matches(tools, clock.now(), exp, act)
    act["args"]["time"] = "8 PM"
    assert not call_matches(tools, clock.now(), exp, act)
