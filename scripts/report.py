"""Build reports/SUMMARY.md (metrics tables + sample transcripts) from the JSON results.

  python -m scripts.report
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REP = ROOT / "reports"


def _f(v, pct=False):
    if v is None:
        return "–"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:,.0f}" if isinstance(v, int | float) and abs(v) >= 10 else str(v)


def run_table(files: list[Path]) -> str:
    rows = ["| run | LLM | scenarios | task completion | tool-call accuracy (right tool + args) | unexpected writes | "
            "allergen violations heard (guard audit / scenario regex) | drafts rewritten by guard | "
            "handoff accuracy (P / R) |",
            "|---|---|---|---|---|---|---|---|---|"]
    for f in files:
        s = json.loads(f.read_text())["summary"]
        m = s["meta"]
        tc = s["tool_calling"]
        ag = s["allergen_guardrail"]
        ho = s["handoff"]
        name = f.stem.removeprefix("results_")
        llm = m["llm"] + (" (hybrid: LLM plans, templates speak)" if m.get("hybrid") else "")
        n_total = 44 if m["mode"] == "text" else s["scenarios"]
        rows.append(
            f"| `{name}` | {llm} | {s['scenarios']}{'' if s.get('complete', True) else f' of {n_total} (partial)'} | "
            f"{s['passed']}/{s['scenarios']} = {_f(s['task_completion_rate'], True)} | "
            f"{tc['correct']}/{tc['expected_calls']} = {_f(tc['accuracy'], True)} "
            f"(wrong args {tc['wrong_args']}, missing {tc['missing']}) | {tc['unjustified_mutating_calls']} | "
            f"{ag['final_reply_violations']} / {ag['scenario_regex_violations']} | "
            f"{ag['llm_draft_violations_caught_and_rewritten']} | "
            f"{_f(ho['accuracy'], True)} ({_f(ho['precision'])} / {_f(ho['recall'])}) |")
    return "\n".join(rows)


def latency_table(files: list[Path]) -> str:
    rows = ["| run | turns measured | LLM calls/turn | LLM per call p50 / p95 | LLM per turn p50 / p95 | "
            "tools p50 / p95 | TTS 1st sentence p50 / p95 | ASR p50 / p95 | first audio (filler) p50 / p95 | "
            "first answer audio p50 / p95 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for f in files:
        s = json.loads(f.read_text())["summary"]
        lat = s["latency"]

        def pp(k, lat=lat):
            v = lat.get(k)
            return "–" if not v or v.get("p50") is None else f"{_f(v['p50'])} / {_f(v['p95'])}"
        name = f.stem.removeprefix("results_")
        rows.append(f"| `{name}` | {lat['turns_measured']} | {lat['llm_calls_per_turn_mean']} "
                    f"| {pp('llm_call_ms')} | {pp('llm_turn_ms')} | {pp('tool_ms')} | {pp('tts_first_sentence_ms')} | "
                    f"{pp('asr_ms')} | {pp('ttfa_any_ms')} | {pp('ttfa_answer_ms')} |")
    return "\n".join(rows)


def category_table(files: list[Path]) -> str:
    cats = sorted({c for f in files for c in json.loads(f.read_text())["summary"]["by_category"]})
    rows = ["| category | " + " | ".join(f"`{f.stem.removeprefix('results_')}`" for f in files) + " |",
            "|---|" + "---|" * len(files)]
    for c in cats:
        rows.append(f"| {c} | " + " | ".join(json.loads(f.read_text())["summary"]["by_category"].get(c, "–")
                                             for f in files) + " |")
    return "\n".join(rows)


def asr_table() -> str:
    rows = ["| Whisper prompt | variant | utterances | WER | slot accuracy | mean confidence | below 0.70 (confirm) | "
            "request latency p50 / p95 (ms) |", "|---|---|---|---|---|---|---|---|"]
    for p in ("none", "short", "menu"):
        f = REP / f"asr_eval_{p}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        for v, s in d["summary"].items():
            rows.append(f"| {p} | {v} | {s['utterances']} | {s['wer'] * 100:.1f}% | {s['slot_accuracy'] * 100:.0f}% "
                        f"({s['slot_checks']} slots) | {s['confidence_mean']} | {s['below_confirm_threshold_0.70']} | "
                        f"{_f(s['latency_ms']['p50'])} / {_f(s['latency_ms']['p95'])} |")
    return "\n".join(rows)


def transcripts(f: Path, ids: list[str]) -> str:
    d = json.loads(f.read_text())
    out = []
    for r in d["results"]:
        if r["id"] not in ids:
            continue
        out.append(f"**{r['id']} · {r['title']}** ({'PASS' if r['passed'] else 'FAIL'})\n")
        out.append("```text")
        for t in r["turns"]:
            out.append(f"Caller: {t['user']}")
            if t.get("asr_confidence") is not None and t.get("asr_ms"):
                out.append(f"  [ASR heard: \"{t['transcript']}\" conf={t['asr_confidence']}]")
            for c in t["result"]["tool_calls"]:
                res = c["result"]
                short = {k: res[k] for k in list(res)[:4]}
                out.append(f"  -> {c['name']}({json.dumps(c['args'])})  => {json.dumps(short)[:160]}")
            if t["result"]["guard_rewrote"]:
                out.append(f"  [guard rewrote draft: \"{t['result']['draft'][:160]}\"]")
            if t.get("interrupted"):
                out.append(f"Agent (interrupted, caller heard): {t['spoken_text']}")
            else:
                out.append(f"Agent: {t['result']['text']}")
        out.append("```\n")
    return "\n".join(out)


def main() -> None:
    order = ["rules_text", "groqpool_text", "groqpool_hybrid_text", "gpt-oss-120b_text", "gpt-oss-20b_hybrid_text",
             "groqpool_audio", "groqpool_audio_noisy", "rules_audio", "rules_audio_noisy"]
    files = [REP / f"results_{n}.json" for n in order if (REP / f"results_{n}.json").exists()]
    md = ["# Measured results\n", "Generated by `python -m scripts.report` from `reports/*.json`.\n",
          "## Scenario suite\n", run_table(files), "\n## By category (passed / run)\n", category_table(files),
          "\n## Latency (ms, live turns only; cached LLM responses excluded)\n", latency_table(files),
          "\n## ASR (Groq whisper-large-v3-turbo on Piper-synthesised caller audio)\n", asr_table()]
    (REP / "SUMMARY.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
