"""One voice turn: audio -> ASR -> agent (LLM + tools) -> sentence-level TTS -> audio frames.

Barge-in: ``interrupt()`` stops playback at the next frame boundary, cancels pending synthesis,
and rewrites the agent's history so the model knows the caller did not hear the rest.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from .agent import Agent, TurnResult

Emit = Callable[[dict], Awaitable[None]]
FILLER = "One moment, let me check that."
# Whisper vocabulary hint: dish names it would otherwise mishear ("gnocchi" -> "nokai").
MENU_PROMPT = ("Trattoria Verde, Hyderabad. Bruschetta, burrata, calamari, arancini, minestrone, caprese, Caesar, "
               "trofie al pesto, carbonara, arrabbiata, linguine frutti di mare, lasagna, gnocchi gorgonzola, "
               "margherita, diavola, cacciatora, branzino, risotto, parmigiana, tiramisu, panna cotta, torta caprese, "
               "sorbetto, focaccia. Booking code TV-D4F8.")


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text.strip())
    return [p.strip() for p in parts if p.strip()]


@dataclass
class TurnMetrics:
    turn: int = 0
    transcript: str = ""
    asr_ms: float | None = None
    asr_confidence: float | None = None
    llm_ms: float = 0.0
    llm_calls: int = 0
    llm_cached_calls: int = 0
    tool_ms: float = 0.0
    tts_first_ms: float | None = None
    tts_total_ms: float = 0.0
    ttfa_any_ms: float | None = None      # first audio of any kind (incl. "one moment" filler)
    ttfa_answer_ms: float | None = None   # first audio of the actual answer
    total_ms: float = 0.0
    audio_ms_sent: float = 0.0
    interrupted: bool = False
    spoken_text: str = ""
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class VoiceSession:
    def __init__(self, agent: Agent, asr, tts, emit: Emit, frame_ms: int = 200, realtime: bool = False,
                 filler: bool = True, asr_prompt: str | None = None):
        self.agent = agent
        self.asr = asr
        self.tts = tts
        self.emit = emit
        self.frame_ms = frame_ms
        self.realtime = realtime
        self.use_filler = filler
        self.asr_prompt = asr_prompt
        self._interrupt = asyncio.Event()
        self._filler_audio: tuple[bytes, int] | None = None
        self.turns = 0

    async def warmup(self) -> None:
        if self.use_filler:
            self._filler_audio = await self.tts.synth(FILLER)

    def interrupt(self) -> None:
        self._interrupt.set()

    async def _send_audio(self, pcm: bytes, sr: int, sentence: str, m: TurnMetrics, t0: float,
                          answer: bool) -> bool:
        """Stream one sentence as frames. Returns False if interrupted part-way."""
        step = int(sr * self.frame_ms / 1000) * 2
        for i in range(0, len(pcm), step):
            if self._interrupt.is_set():
                return False
            now_ms = (time.perf_counter() - t0) * 1000
            if m.ttfa_any_ms is None:
                m.ttfa_any_ms = now_ms
            if answer and m.ttfa_answer_ms is None:
                m.ttfa_answer_ms = now_ms
            frame = pcm[i:i + step]
            await self.emit({"type": "audio", "data": base64.b64encode(frame).decode(), "sample_rate": sr,
                             "sentence": sentence, "filler": not answer})
            m.audio_ms_sent += len(frame) / 2 / sr * 1000
            if answer:
                self._answer_ms_sent = getattr(self, "_answer_ms_sent", 0.0) + len(frame) / 2 / sr * 1000
                self._partial = (sentence, (i + len(frame)) / max(1, len(pcm)))
                if self._barge_in_after_ms is not None and self._answer_ms_sent >= self._barge_in_after_ms:
                    self.interrupt()  # simulated caller talking over the agent (eval / tests)
            if self.realtime:
                await asyncio.sleep(len(frame) / 2 / sr * 0.9)
            else:
                await asyncio.sleep(0)
        return True

    async def run_turn(self, audio: bytes | None = None, sample_rate: int = 16000, text: str | None = None,
                       asr_confidence: float | None = None,
                       barge_in_after_sentences: int | None = None,
                       barge_in_after_ms: float | None = None) -> TurnMetrics:
        self.turns += 1
        self._interrupt.clear()
        self._barge_in_after_ms = barge_in_after_ms
        m = TurnMetrics(turn=self.turns)
        t0 = time.perf_counter()
        if audio is not None:
            try:
                tr = await self.asr.transcribe(audio, sample_rate, self.asr_prompt)
            except Exception as e:  # ASR down: say so, don't guess
                await self.emit({"type": "error", "stage": "asr", "message": str(e)[:200]})
                tr = None
            if tr is None:
                text, asr_confidence = "", 0.0
            else:
                m.asr_ms, text, asr_confidence = tr.total_ms or tr.ms, tr.text, tr.confidence
                if tr.no_speech_prob > 0.8:
                    text = ""
            m.asr_confidence = asr_confidence
            await self.emit({"type": "transcript", "text": text, "confidence": asr_confidence})
        m.transcript = text or ""

        filler_task: asyncio.Task | None = None

        async def on_tool_start():
            nonlocal filler_task
            if self.use_filler and self._filler_audio and m.ttfa_any_ms is None:
                pcm, sr = self._filler_audio
                filler_task = asyncio.create_task(self._send_audio(pcm, sr, FILLER, m, t0, answer=False))

        result: TurnResult = await self.agent.handle_turn(m.transcript, asr_confidence, on_tool_start)
        if filler_task:
            await filler_task
        m.llm_ms = sum(result.llm_ms)
        m.llm_calls = len(result.llm_ms)
        m.llm_cached_calls = sum(result.llm_cached)
        m.tool_ms = sum(c.ms for c in result.tool_calls)
        for c in result.tool_calls:
            await self.emit({"type": "tool_call", "name": c.name, "args": c.args, "result": c.result})
        await self.emit({"type": "reply", "text": result.text, "guard_rewrote": result.guard_rewrote,
                         "handoff": result.handoff})

        sentences = split_sentences(result.text)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)

        async def producer():
            for s in sentences:
                ts = time.perf_counter()
                pcm, sr = await self.tts.synth(s)
                ms = (time.perf_counter() - ts) * 1000
                m.tts_total_ms += ms
                if m.tts_first_ms is None:
                    m.tts_first_ms = ms
                await q.put((s, pcm, sr))
            await q.put(None)

        self._answer_ms_sent = 0.0
        self._partial = None
        prod = asyncio.create_task(producer())
        spoken: list[str] = []
        try:
            n = 0
            while True:
                item = await q.get()
                if item is None:
                    break
                s, pcm, sr = item
                ok = await self._send_audio(pcm, sr, s, m, t0, answer=True)
                if not ok:
                    break
                spoken.append(s)
                n += 1
                if barge_in_after_sentences is not None and n >= barge_in_after_sentences and n < len(sentences):
                    self.interrupt()  # simulated caller talking over the agent (eval / tests)
        finally:
            if self._interrupt.is_set():
                prod.cancel()
                m.interrupted = True
                # anything still queued must NOT be played
                while not q.empty():
                    q.get_nowait()
                heard = " ".join(spoken)
                if self._partial and self._partial[0] not in spoken:
                    words = self._partial[0].split()
                    heard += " " + " ".join(words[:max(1, int(len(words) * self._partial[1]))]) + " ..."
                self.agent.mark_interrupted(heard.strip())
                spoken = [heard.strip()]
            else:
                await prod
        m.spoken_text = " ".join(spoken)
        m.total_ms = (time.perf_counter() - t0) * 1000
        m.result = result.to_dict()
        await self.emit({"type": "metrics", "metrics": {k: v for k, v in m.to_dict().items() if k != "result"}})
        await self.emit({"type": "turn_done", "status": "interrupted" if m.interrupted else "ok"})
        return m
