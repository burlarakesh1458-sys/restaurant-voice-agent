"""Synthesize the caller side of the audio-subset scenarios with Piper.

The caller uses a different voice (en_GB alan) from the agent (en_US lessac). Each turn is written
as 16 kHz mono WAV to data/audio/<id>_t<i>.wav, plus a "_noisy" copy (white noise at 5 dB SNR,
roughly a bad phone line in a busy street) so the eval can measure what noise does to ASR.

  python -m scripts.gen_audio
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.speech import PiperTTS, pcm16_to_wav, resample_pcm16  # noqa: E402
from scenarios.suite import SCENARIOS  # noqa: E402

VOICE = ROOT / "models" / "piper" / "en_GB-alan-low.onnx"
OUT = ROOT / "data" / "audio"
SNR_DB = 5.0


def add_noise(pcm: bytes, snr_db: float, seed: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    p_sig = float(np.mean(x ** 2)) or 1.0
    noise = np.random.default_rng(seed).normal(0, np.sqrt(p_sig / 10 ** (snr_db / 10)), x.shape)
    return np.clip(x + noise, -32768, 32767).astype(np.int16).tobytes()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tts = PiperTTS(str(VOICE if VOICE.exists() else ROOT / "models" / "piper" / "en_US-lessac-low.onnx"))
    n = 0
    for s in SCENARIOS:
        if not s["audio"]:
            continue
        for i, t in enumerate(s["turns"]):
            pcm, sr = tts.synth_blocking(t["user"])
            pcm = resample_pcm16(pcm, sr, 16000)
            pad = b"\x00\x00" * 4000  # 250 ms of silence either side, like a real endpointed utterance
            pcm = pad + pcm + pad
            (OUT / f"{s['id']}_t{i}.wav").write_bytes(pcm16_to_wav(pcm, 16000))
            (OUT / f"{s['id']}_t{i}_noisy.wav").write_bytes(pcm16_to_wav(add_noise(pcm, SNR_DB, seed=i + n), 16000))
            n += 1
    print(f"wrote {n} turns x 2 variants to {OUT}")


if __name__ == "__main__":
    main()
