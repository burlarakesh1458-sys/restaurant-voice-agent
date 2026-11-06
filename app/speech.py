"""Speech in and out.

ASR: Groq Whisper (whisper-large-v3-turbo) with a confidence score from segment log-probs.
TTS: Piper (local ONNX voice, CPU). Groq's Orpheus TTS needs a console terms click, so it is not used.
Fakes for offline tests: FakeASR (returns a scripted transcript) and FakeTTS (silence).
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import threading
import time
import wave
from dataclasses import dataclass

import httpx
import numpy as np


def pcm16_to_wav(pcm: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def resample_pcm16(pcm: bytes, sr_in: int, sr_out: int) -> bytes:
    if sr_in == sr_out:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = int(len(x) * sr_out / sr_in)
    y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


@dataclass
class Transcript:
    text: str
    confidence: float      # 0..1, exp(mean segment avg_logprob)
    no_speech_prob: float
    ms: float
    provider: str


class ASRError(Exception):
    pass


class GroqWhisperASR:
    def __init__(self, model: str = "whisper-large-v3-turbo", api_key: str | None = None,
                 base_url: str = "https://api.groq.com/openai/v1", timeout_s: float = 20.0, retries: int = 3):
        self.model = model
        self.name = f"groq:{model}"
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.base_url = base_url
        self.retries = retries
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def transcribe(self, pcm: bytes, sr: int, prompt: str | None = None) -> Transcript:
        wav = pcm16_to_wav(pcm, sr)
        data = {"model": self.model, "response_format": "verbose_json", "language": "en", "temperature": "0"}
        if prompt:
            data["prompt"] = prompt  # vocabulary hint: dish names
        t0 = time.perf_counter()
        last = ""
        for attempt in range(self.retries + 1):
            try:
                r = await self._client.post(f"{self.base_url}/audio/transcriptions",
                                            headers={"Authorization": f"Bearer {self.api_key}"},
                                            files={"file": ("turn.wav", wav, "audio/wav")}, data=data)
            except httpx.HTTPError as e:
                last = type(e).__name__
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            if r.status_code == 200:
                j = r.json()
                segs = j.get("segments") or []
                if segs:
                    w = [max(1e-3, s.get("end", 0) - s.get("start", 0)) for s in segs]
                    lp = sum(s.get("avg_logprob", 0) * wi for s, wi in zip(segs, w)) / sum(w)
                    nsp = max(s.get("no_speech_prob", 0) for s in segs)
                else:
                    lp, nsp = (0.0 if j.get("text") else -5.0), 0.0
                return Transcript(j.get("text", "").strip(), round(math.exp(lp), 3), round(nsp, 3),
                                  (time.perf_counter() - t0) * 1000, self.name)
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(min(20, 1.0 * (2 ** attempt)))
                continue
            break
        raise ASRError(last)


class FakeASR:
    """Offline ASR: the 'audio' is the UTF-8 transcript itself (or a mapping)."""

    name = "fake-asr"

    def __init__(self, confidence: float = 0.95, delay_s: float = 0.0):
        self.confidence = confidence
        self.delay_s = delay_s

    async def transcribe(self, pcm: bytes, sr: int, prompt: str | None = None) -> Transcript:
        await asyncio.sleep(self.delay_s)
        text = pcm.decode("utf-8", errors="ignore") if pcm and not pcm.startswith(b"RIFF") else ""
        return Transcript(text, self.confidence, 0.0, self.delay_s * 1000, self.name)


class PiperTTS:
    def __init__(self, model_path: str, threads: int = 1):
        self.model_path = model_path
        self.name = f"piper:{os.path.basename(model_path).removesuffix('.onnx')}"
        self.threads = threads
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._voice is None:
                os.environ.setdefault("OMP_NUM_THREADS", str(self.threads))
                from piper import PiperVoice
                self._voice = PiperVoice.load(self.model_path)
        return self._voice

    def synth_blocking(self, text: str) -> tuple[bytes, int]:
        voice = self._load()
        chunks = list(voice.synthesize(text))
        if not chunks:
            return b"", 16000
        return b"".join(c.audio_int16_bytes for c in chunks), chunks[0].sample_rate

    async def synth(self, text: str) -> tuple[bytes, int]:
        return await asyncio.to_thread(self.synth_blocking, text)

    async def warmup(self) -> None:
        await self.synth("Warm up.")


class FakeTTS:
    name = "fake-tts"

    def __init__(self, delay_s: float = 0.0, sr: int = 16000):
        self.delay_s = delay_s
        self.sr = sr

    async def synth(self, text: str) -> tuple[bytes, int]:
        await asyncio.sleep(self.delay_s)
        n = int(self.sr * 0.06 * max(1, len(text.split())))  # ~60 ms per word of silence
        return b"\x00\x00" * n, self.sr

    async def warmup(self) -> None:
        return None
