"""ASR-only eval: Groq Whisper on the Piper-generated caller audio (clean and 5 dB SNR noisy).

Reports word error rate, slot accuracy (party size / time / date / name parsed from the transcript
equal those parsed from the script), confidence, and latency. Writes reports/asr_eval.json.

  python -m scripts.asr_eval
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import nlu  # noqa: E402
from app.clock import FixedClock  # noqa: E402
from app.speech import GroqWhisperASR, wav_to_pcm16  # noqa: E402
from scenarios.suite import NOW, SCENARIOS  # noqa: E402
from scripts.run_eval import pct  # noqa: E402

AUDIO = ROOT / "data" / "audio"
PROMPT = "Trattoria Verde. Trofie al pesto, risotto, tiramisu, panna cotta, calamari."


def norm(s: str) -> list[str]:
    s = s.lower().replace("-", " ")
    s = re.sub(r"[^a-z0-9: ']", " ", s)
    return s.split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


def slots(text: str, now) -> dict:
    return {"party": nlu.find_party_size(text), "time": nlu.find_time(text), "date": nlu.find_date(text, now),
            "code": nlu.normalize_code(text)}


async def main() -> None:
    asr = GroqWhisperASR()
    now = FixedClock(NOW).now()
    rows = []
    for s in SCENARIOS:
        if not s["audio"]:
            continue
        for i, t in enumerate(s["turns"]):
            for variant in ("", "_noisy"):
                pcm, sr = wav_to_pcm16((AUDIO / f"{s['id']}_t{i}{variant}.wav").read_bytes())
                tr = await asr.transcribe(pcm, sr, PROMPT)
                e, n = wer(t["user"], tr.text)
                ref_slots, hyp_slots = slots(t["user"], now), slots(tr.text, now)
                rows.append({"id": f"{s['id']}_t{i}", "variant": variant.strip("_") or "clean", "ref": t["user"],
                             "hyp": tr.text, "errors": e, "words": n, "confidence": tr.confidence,
                             "no_speech_prob": tr.no_speech_prob, "ms": round(tr.ms, 1),
                             "slots_ok": {k: ref_slots[k] == hyp_slots[k] for k in ref_slots if ref_slots[k]},
                             "audio_s": round(len(pcm) / 2 / sr, 2)})
                print(f"{rows[-1]['id']:7s} {rows[-1]['variant']:5s} conf={tr.confidence:.2f} {e}/{n}  {tr.text[:80]}",
                      flush=True)
    summary = {}
    for v in ("clean", "noisy"):
        rs = [r for r in rows if r["variant"] == v]
        slot_checks = [ok for r in rs for ok in r["slots_ok"].values()]
        summary[v] = {
            "utterances": len(rs), "wer": round(sum(r["errors"] for r in rs) / sum(r["words"] for r in rs), 4),
            "slot_accuracy": round(sum(slot_checks) / max(1, len(slot_checks)), 3), "slot_checks": len(slot_checks),
            "confidence_mean": round(sum(r["confidence"] for r in rs) / len(rs), 3),
            "confidence_min": min(r["confidence"] for r in rs),
            "below_confirm_threshold_0.70": sum(r["confidence"] < 0.70 for r in rs),
            "below_reprompt_threshold_0.40": sum(r["confidence"] < 0.40 for r in rs),
            "latency_ms": {"p50": pct([r["ms"] for r in rs], .5), "p95": pct([r["ms"] for r in rs], .95)},
        }
    out = {"asr": asr.name, "caller_voice": "piper en_GB-alan-low", "noise": "white noise, 5 dB SNR",
           "summary": summary, "rows": rows}
    (ROOT / "reports" / "asr_eval.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
