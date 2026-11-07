import asyncio

from app.factory import build_agent
from app.pipeline import VoiceSession, split_sentences
from app.speech import FakeASR, FakeTTS


def run(session, **kw):
    return asyncio.run(session.run_turn(**kw))


def make(clock, events):
    async def emit(e):
        events.append(e)

    s = VoiceSession(build_agent("rules", clock), FakeASR(confidence=0.95), FakeTTS(), emit)
    asyncio.run(s.warmup())
    return s


def test_split_sentences():
    assert split_sentences("Hi there. Code is T V 6 Q 4 5. Bye!") == ["Hi there.", "Code is T V 6 Q 4 5.", "Bye!"]


def test_text_turn_streams_audio_and_metrics(clock):
    ev = []
    m = run(make(clock, ev), text="Do you have a table for two this Saturday at 8 pm?")
    types = [e["type"] for e in ev]
    assert "tool_call" in types and "reply" in types and types[-1] == "turn_done"
    assert m.ttfa_any_ms is not None and m.ttfa_answer_ms >= m.ttfa_any_ms


def test_filler_plays_before_answer_on_tool_turns(clock):
    ev = []
    run(make(clock, ev), text="Are you open on Monday?")
    audio = [e for e in ev if e["type"] == "audio"]
    assert audio[0]["filler"] is True and any(not a["filler"] for a in audio)


def test_audio_turn_goes_through_asr(clock):
    ev = []
    m = run(make(clock, ev), audio=b"Can I speak to a real person please?", sample_rate=16000)
    assert m.asr_confidence == 0.95 and ev[0]["type"] == "transcript"
    assert m.result["handoff"]


def test_barge_in_stops_audio_and_rewrites_history(clock):
    ev = []
    s = make(clock, ev)
    m = run(s, text="What vegetarian dishes do you have?", barge_in_after_ms=300)
    assert m.interrupted and len(m.spoken_text) < len(m.result["text"])
    after = [e for e in ev[[e["type"] for e in ev].index("reply") :] if e["type"] == "audio" and not e["filler"]]
    assert sum(len(e["data"]) for e in after) > 0
    assert "interrupted" in s.agent.history[-1]["content"]
    m2 = run(s, text="Actually book a table for two on Sunday at 7 pm, name Kavya Iyer.")
    assert any(c["name"] == "create_reservation" for c in m2.result["tool_calls"])
