"""FastAPI app: browser UI, REST helpers and the /ws voice session.

WebSocket protocol (JSON frames)
  client -> server
    {"type": "start", "caller_phone": "+91..."}                 open a call
    {"type": "audio", "data": <b64 PCM16 mono>, "sample_rate": 16000}   append mic audio
    {"type": "end_utterance"}                                    caller stopped talking -> run a turn
    {"type": "text", "text": "..."}                              typed turn (skips ASR)
    {"type": "interrupt"}                                        barge-in: stop speaking now
  server -> client
    started | transcript | tool_call | reply | audio (b64 PCM16 + sample_rate) | metrics | turn_done | error
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .clock import Clock, FixedClock
from .factory import ROOT, build_agent
from .pipeline import MENU_PROMPT, VoiceSession
from .speech import FakeASR, FakeTTS, GroqWhisperASR, PiperTTS

LLM_KIND = os.environ.get("LLM_KIND", "groq" if os.environ.get("GROQ_API_KEY") else "rules")
HYBRID = os.environ.get("HYBRID", "0") == "1"
PIPER_MODEL = os.environ.get("PIPER_MODEL", str(ROOT / "models" / "piper" / "en_US-lessac-low.onnx"))
DB_PATH = os.environ.get("DB_PATH", ":memory:")
CLOCK = FixedClock(os.environ["FIXED_NOW"]) if os.environ.get("FIXED_NOW") else Clock()

app = FastAPI(title="Trattoria Verde voice agent")
_tts = None


def get_tts():
    global _tts
    if _tts is None:
        if os.environ.get("TTS", "piper") == "piper" and Path(PIPER_MODEL).exists():
            _tts = PiperTTS(PIPER_MODEL)
        else:
            _tts = FakeTTS()
    return _tts


def get_asr():
    if os.environ.get("ASR", "groq") == "groq" and os.environ.get("GROQ_API_KEY"):
        return GroqWhisperASR()
    return FakeASR()


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "llm": LLM_KIND, "hybrid": HYBRID, "tts": get_tts().name}


@app.get("/api/menu")
def menu():
    agent = build_agent("rules", CLOCK)
    rows = agent.tools.conn.execute("SELECT name, category, price_inr, allergen_info_status FROM dishes")
    return JSONResponse([dict(r) for r in rows])


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def emit(event: dict):
        async with send_lock:
            await websocket.send_text(json.dumps(event, default=str))

    session: VoiceSession | None = None
    buf = bytearray()
    sr = 16000
    turn_task: asyncio.Task | None = None
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            kind = msg.get("type")
            if kind == "start":
                agent = build_agent(
                    LLM_KIND,
                    CLOCK,
                    DB_PATH if DB_PATH != ":memory:" else ":memory:",
                    caller_phone=msg.get("caller_phone") or "+919811111111",
                    hybrid=HYBRID,
                    cache_path=None,
                )
                agent.session_id = uuid.uuid4().hex[:8]
                session = VoiceSession(agent, get_asr(), get_tts(), emit, realtime=True, asr_prompt=MENU_PROMPT)
                await session.warmup()
                await emit({"type": "started", "session_id": agent.session_id, "llm": LLM_KIND})
            elif session is None:
                await emit({"type": "error", "message": "send start first"})
            elif kind == "audio":
                if turn_task and not turn_task.done():
                    session.interrupt()  # caller speaking over the agent = barge-in
                buf.extend(base64.b64decode(msg["data"]))
                sr = int(msg.get("sample_rate", 16000))
            elif kind == "interrupt":
                session.interrupt()
            elif kind in ("end_utterance", "text"):
                if turn_task and not turn_task.done():
                    session.interrupt()
                    await turn_task
                if kind == "text":
                    turn_task = asyncio.create_task(session.run_turn(text=msg.get("text", "")))
                else:
                    audio, buf = bytes(buf), bytearray()
                    turn_task = asyncio.create_task(session.run_turn(audio=audio, sample_rate=sr))
    except WebSocketDisconnect:
        if session:
            session.interrupt()
