"""Run the scripted scenario suite through the full voice pipeline and score it.

  python -m scripts.run_eval --llm rules                     # no LLM (baseline, offline)
  python -m scripts.run_eval --llm groq                      # LLM plans tools and phrases replies
  python -m scripts.run_eval --llm groq --hybrid             # LLM plans tools, templates phrase replies
  python -m scripts.run_eval --llm groq --mode audio         # Piper-generated caller audio -> Groq Whisper

Results are written incrementally to reports/results_<tag>.json, so a run that hits a rate limit
can be resumed (finished scenarios are skipped, LLM responses are cached on disk).
Turns whose LLM calls were served from the cache are excluded from latency statistics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import nlu  # noqa: E402
from app.clock import FixedClock  # noqa: E402
from app.factory import build_agent  # noqa: E402
from app.guardrails import AllergenGuard  # noqa: E402
from app.llm import FaultyLLM, _retry_after  # noqa: E402
from app.pipeline import MENU_PROMPT, VoiceSession  # noqa: E402
from app.speech import FakeTTS, GroqWhisperASR, PiperTTS, wav_to_pcm16  # noqa: E402
from app.tools import FaultPlan, canonical_allergens  # noqa: E402
from scenarios.suite import NOW, SCENARIOS  # noqa: E402

MUTATING = {"create_reservation", "modify_reservation", "cancel_reservation", "handoff_to_human"}
AUDIO_DIR = ROOT / "data" / "audio"


class QuotaExhausted(Exception):
    pass


# ---------------------------------------------------------------------------------- arg matching
def _resolve(tools, dish: str) -> str | None:
    r = tools._resolve_dish(str(dish))
    if "dish" in r:
        return r["dish"]["name"]
    return r.get("did_you_mean")


def arg_match(tools, now, k: str, exp, act) -> bool:
    if act is None:
        return k == "allergen"  # get_allergens without an allergen still returns the full verified record
    if k in ("date", "new_date"):
        return nlu.parse_date(exp, now) == nlu.parse_date(act, now)
    if k in ("time", "new_time"):
        return nlu.parse_time(exp) == nlu.parse_time(act)
    if k in ("party_size", "new_party_size"):
        return nlu.parse_party_size(exp) == nlu.parse_party_size(act)
    if k == "dish":
        e, a = _resolve(tools, exp), _resolve(tools, act)
        return e is not None and e == a
    if k in ("allergen", "free_of"):
        return bool(set(canonical_allergens(str(exp))) & set(canonical_allergens(str(act))))
    if k == "code":
        return nlu.normalize_code(exp) == nlu.normalize_code(act)
    if k == "name":
        return str(exp).strip().lower() == str(act).strip().lower()
    if isinstance(exp, bool):
        return bool(act) == exp
    return str(exp).strip().lower() == str(act).strip().lower()


def call_matches(tools, now, exp: dict, act: dict) -> bool:
    return exp["tool"] == act["name"] and all(arg_match(tools, now, k, v, act["args"].get(k))
                                              for k, v in exp.get("args", {}).items())


def score_tool_calls(tools, now, expected: list[dict], actual: list[dict]) -> dict:
    out = {"expected": 0, "correct": 0, "wrong_args": 0, "missing": 0, "details": []}
    used: set[int] = set()
    for exp in expected:
        alts = exp["any_of"] if "any_of" in exp else [exp]
        out["expected"] += 1
        hit = next(((i, a) for i, a in enumerate(actual) if i not in used and any(
            call_matches(tools, now, e, a) for e in alts)), None)
        if hit:
            used.add(hit[0])
            out["correct"] += 1
            out["details"].append({"expected": exp, "status": "correct"})
        elif any(a["name"] == e["tool"] for a in actual for e in alts):
            out["wrong_args"] += 1
            out["details"].append({"expected": exp, "status": "wrong_args",
                                   "actual": [a for a in actual if a["name"] in {e["tool"] for e in alts}]})
        else:
            out["missing"] += 1
            out["details"].append({"expected": exp, "status": "missing"})
    return out


# ---------------------------------------------------------------------------------- scenario run
async def run_scenario(sc: dict, args, tts, asr) -> dict:
    clock = FixedClock(NOW)
    faults = FaultPlan({k: (v[0], v[1]) for k, v in sc["faults"].items()})
    agent = build_agent(args.llm, clock, ":memory:", caller_phone=sc["caller_phone"], faults=faults,
                        base_date=date.fromisoformat(NOW[:10]), cache_path=str(ROOT / "reports" / ".llm_cache.sqlite"),
                        tool_timeout_s=1.0, hybrid=args.hybrid)
    if sc.get("llm_fault"):
        agent.llm = FaultyLLM(agent.llm, n=sc["llm_fault"])
    tools = agent.tools
    before = {r["code"]: dict(r) for r in tools.conn.execute("SELECT * FROM reservations")}
    events: list[dict] = []

    async def emit(e):
        events.append({k: v for k, v in e.items() if k != "data"})

    session = VoiceSession(agent, asr, tts, emit, realtime=False, filler=True, asr_prompt=MENU_PROMPT)
    await session.warmup()
    turns = []
    use_audio = args.mode == "audio"
    for i, t in enumerate(sc["turns"]):
        kw = {"barge_in_after_ms": t.get("barge_in_after_ms")}
        if use_audio:
            wav = AUDIO_DIR / f"{sc['id']}_t{i}{args.audio_suffix}.wav"
            pcm, sr = wav_to_pcm16(wav.read_bytes())
            m = await session.run_turn(audio=pcm, sample_rate=sr, **kw)
        else:
            m = await session.run_turn(text=t["user"], asr_confidence=t.get("asr_confidence"), **kw)
        err = getattr(agent, "last_error", "") or ""
        if "quota" in err and not sc.get("llm_fault"):
            raise QuotaExhausted(err)
        turns.append({"user": t["user"], **m.to_dict()})
    after = {r["code"]: dict(r) for r in tools.conn.execute("SELECT * FROM reservations")}
    handoffs = [dict(r) for r in tools.conn.execute("SELECT * FROM handoffs")]
    return evaluate(sc, turns, before, after, handoffs, tools, clock)


def evaluate(sc, turns, before, after, handoffs, tools, clock) -> dict:
    exp = sc["expect"]
    now = clock.now()
    checks: list[tuple[str, bool, str]] = []
    new = [r for c, r in after.items() if c not in before and r["status"] == "confirmed"]
    actual_calls = [c for t in turns for c in t["result"]["tool_calls"]]
    for want in exp.get("reservations", []):
        ok = any(r["name"].lower() == want["name"].lower() and r["date"] == want["date"] and r["time"] == want["time"]
                 and r["party_size"] == want["party_size"] for r in new)
        checks.append(("reservation_created", ok, json.dumps(want)))
    for name, n in exp.get("reservation_count", {}).items():
        got = sum(1 for r in after.values() if r["name"].lower() == name.lower() and r["status"] == "confirmed")
        checks.append(("reservation_count", got == n, f"{name}: {got} (want {n})"))
    if exp.get("no_new_reservation"):
        checks.append(("no_new_reservation", not new, f"{len(new)} new"))
    for code in exp.get("cancelled", []):
        checks.append(("cancelled", after[code]["status"] == "cancelled", code))
    for code in exp.get("not_cancelled", []):
        checks.append(("not_cancelled", after[code]["status"] == "confirmed", code))
    for code, fields in exp.get("modified", {}).items():
        ok = all(str(after[code][k]) == str(v) for k, v in fields.items()) and after[code]["status"] == "confirmed"
        checks.append(("modified", ok, f"{code} {fields}"))
    want_handoff = exp.get("handoff", False)
    did_handoff = len(handoffs) > 0
    checks.append(("handoff", did_handoff == want_handoff, f"want {want_handoff} got {did_handoff}"))
    for turn_idx in exp.get("no_booking_on_turn", []):
        booked = any(c["name"] == "create_reservation" and c["result"].get("status") == "confirmed"
                     for c in turns[turn_idx]["result"]["tool_calls"])
        checks.append(("no_booking_on_turn", not booked, f"turn {turn_idx}"))
    for turn_idx in exp.get("interrupted_turns", []):
        checks.append(("interrupted", turns[turn_idx]["interrupted"], f"turn {turn_idx}"))
    if "max_tool_calls" in exp:
        checks.append(("max_tool_calls", len(actual_calls) <= exp["max_tool_calls"], f"{len(actual_calls)} calls"))
    replies = [t["result"]["text"] for t in turns]
    for r in exp.get("reply_any", []):
        txt = replies[r["turn"]].replace("\u2019", "'")
        ok = any(re.search(p, txt, re.I) for p in r["patterns"])
        checks.append(("reply_any", ok, f"turn {r['turn']}: {r['patterns'][0][:60]}"))
    regex_violations = []
    for r in exp.get("reply_none", []):
        idxs = range(len(replies)) if r["turn"] == "all" else [r["turn"]]
        for i in idxs:
            txt = replies[i].replace("\u2019", "'")
            bad = [p for p in r["patterns"] if re.search(p, txt, re.I)]
            if bad:
                regex_violations.append({"turn": i, "pattern": bad[0], "reply": replies[i]})
            checks.append(("reply_none", not bad, f"turn {i}"))
    # independent allergen-claim audit of what the caller actually heard
    guard = AllergenGuard(tools)
    final_violations, draft_violations = [], []
    for t in turns:
        ctx = t["user"] + " " + " ".join(json.dumps(c["result"]) for c in t["result"]["tool_calls"])
        for v in guard.find_violations(t["result"]["text"], ctx):
            final_violations.append({"dish": v.dish, "allergen": v.allergen, "claim": v.polarity,
                                     "verified": v.truth, "clause": v.clause})
        draft_violations += t["result"]["guard_violations_draft"]
    checks.append(("allergen_guard_final", not final_violations, f"{len(final_violations)}"))
    tool_score = score_tool_calls(tools, now, exp.get("calls", []), actual_calls)
    justified = {e.get("tool") for e in exp.get("calls", [])} | \
        {a["tool"] for e in exp.get("calls", []) for a in e.get("any_of", [])}
    if want_handoff:
        justified.add("handoff_to_human")
    if exp.get("reservations"):
        justified.add("create_reservation")
    if exp.get("cancelled"):
        justified.add("cancel_reservation")
    if exp.get("modified"):
        justified.add("modify_reservation")
    succeeded = {"confirmed", "cancelled", "modified", "handoff_created"}
    # a write that actually happened (validation rejections don't count) and that the scenario did not call for
    unjustified = [c for c in actual_calls if c["name"] in MUTATING and c["name"] not in justified
                   and c["result"].get("status") in succeeded and not c["result"].get("duplicate")]
    return {
        "id": sc["id"], "category": sc["category"], "title": sc["title"],
        "passed": all(ok for _, ok, _ in checks),
        "failed_checks": [{"check": n, "detail": d} for n, ok, d in checks if not ok],
        "tool_score": tool_score, "unjustified_mutating_calls": unjustified,
        "handoff_expected": want_handoff, "handoff_done": did_handoff,
        "allergen_final_violations": final_violations, "allergen_draft_violations": draft_violations,
        "allergen_regex_violations": regex_violations,
        "turns": [{k: v for k, v in t.items()} for t in turns],
    }


# ---------------------------------------------------------------------------------- aggregation
def pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def _model_counts(results: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in results:
        for t in r["turns"]:
            for m in t["result"].get("llm_models", []):
                out[m] = out.get(m, 0) + 1
    return out


def aggregate(results: list[dict], meta: dict) -> dict:
    n = len(results)
    ts = [r["tool_score"] for r in results]
    exp = sum(t["expected"] for t in ts)
    ho = [(r["handoff_expected"], r["handoff_done"]) for r in results]
    tp = sum(1 for e, d in ho if e and d)
    fp = sum(1 for e, d in ho if not e and d)
    fn = sum(1 for e, d in ho if e and not d)
    live_turns = [t for r in results for t in r["turns"]
                  if t["llm_calls"] and t["llm_cached_calls"] == 0 and t["result"]["llm_ms"]
                  and not t["result"]["degraded"]]
    per_call = [x for t in live_turns for x in t["result"]["llm_ms"]]
    lat = {
        "turns_measured": len(live_turns),
        "llm_turn_ms": {"p50": pct([t["llm_ms"] for t in live_turns], .5),
                        "p95": pct([t["llm_ms"] for t in live_turns], .95)},
        "llm_call_ms": {"p50": pct(per_call, .5), "p95": pct(per_call, .95), "n": len(per_call)},
        "llm_calls_per_turn_mean": round(sum(t["llm_calls"] for t in live_turns) / max(1, len(live_turns)), 2),
        "tool_ms": {"p50": pct([t["tool_ms"] for t in live_turns], .5),
                    "p95": pct([t["tool_ms"] for t in live_turns], .95)},
        "tts_first_sentence_ms": {"p50": pct([t["tts_first_ms"] for t in live_turns], .5),
                                  "p95": pct([t["tts_first_ms"] for t in live_turns], .95)},
        "ttfa_any_ms": {"p50": pct([t["ttfa_any_ms"] for t in live_turns], .5),
                        "p95": pct([t["ttfa_any_ms"] for t in live_turns], .95)},
        "ttfa_answer_ms": {"p50": pct([t["ttfa_answer_ms"] for t in live_turns], .5),
                           "p95": pct([t["ttfa_answer_ms"] for t in live_turns], .95)},
    }
    asr = [t["asr_ms"] for r in results for t in r["turns"] if t.get("asr_ms")]
    if asr:
        lat["asr_ms"] = {"p50": pct(asr, .5), "p95": pct(asr, .95), "n": len(asr)}
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    allergen_scen = [r for r in results if r["category"] in ("allergen", "adversarial", "multi_intent", "asr_error",
                                                             "tool_failure")]
    return {
        "meta": meta,
        "scenarios": n,
        "task_completion_rate": round(sum(r["passed"] for r in results) / max(1, n), 3),
        "passed": sum(r["passed"] for r in results),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "tool_calling": {"expected_calls": exp, "correct": sum(t["correct"] for t in ts),
                         "wrong_args": sum(t["wrong_args"] for t in ts), "missing": sum(t["missing"] for t in ts),
                         "accuracy": round(sum(t["correct"] for t in ts) / max(1, exp), 3),
                         "unjustified_mutating_calls": sum(len(r["unjustified_mutating_calls"]) for r in results)},
        "allergen_guardrail": {
            "final_reply_violations": sum(len(r["allergen_final_violations"]) for r in results),
            "scenario_regex_violations": sum(len(r["allergen_regex_violations"]) for r in results
                                             if r in allergen_scen or r["allergen_regex_violations"]),
            "llm_draft_violations_caught_and_rewritten": sum(len(r["allergen_draft_violations"]) for r in results),
        },
        "handoff": {"accuracy": round(sum(1 for e, d in ho if e == d) / max(1, n), 3), "tp": tp, "fp": fp, "fn": fn,
                    "tn": n - tp - fp - fn,
                    "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                    "recall": round(tp / (tp + fn), 3) if tp + fn else None},
        "latency": lat,
        "llm_calls_by_model": _model_counts(results),
        "failures": [{"id": r["id"], "failed_checks": r["failed_checks"]} for r in results if not r["passed"]],
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="rules", help="rules | groq | <groq model id>")
    ap.add_argument("--hybrid", action="store_true", help="LLM plans tool calls, templates phrase the reply")
    ap.add_argument("--mode", default="text", choices=["text", "audio"])
    ap.add_argument("--audio-suffix", default="", help="'' for clean audio, '_noisy' for the noisy variant")
    ap.add_argument("--tts", default="piper", choices=["piper", "fake"])
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--tag", default="")
    ap.add_argument("--fresh", action="store_true", help="ignore previous partial results")
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between scenarios (rate limits)")
    ap.add_argument("--max-wait-s", type=float, default=6 * 3600, help="total time to wait out daily token quotas")
    args = ap.parse_args()
    model = os.environ.get("LLM_MODELS", "openai/gpt-oss-20b") if args.llm == "groq" else args.llm
    base = args.llm if args.llm != "groq" else ("groqpool" if "," in model else model.split("/")[-1])
    base = base.replace("ollama:", "").replace(":", "-") + ("-ollama" if args.llm.startswith("ollama:") else "")
    tag = args.tag or f"{base}{'_hybrid' if args.hybrid else ''}_{args.mode}{args.audio_suffix}"
    out = ROOT / "reports" / f"results_{tag}.json"
    scen = [s for s in SCENARIOS if not args.only or s["id"] in args.only.split(",")]
    if args.mode == "audio":
        scen = [s for s in scen if s["audio"] and (AUDIO_DIR / f"{s['id']}_t0{args.audio_suffix}.wav").exists()]
    done: dict[str, dict] = {}
    if out.exists() and not args.fresh:
        done = {r["id"]: r for r in json.loads(out.read_text()).get("results", [])}
    tts = PiperTTS(str(ROOT / "models" / "piper" / "en_US-lessac-low.onnx")) if args.tts == "piper" else FakeTTS()
    await tts.warmup()
    asr = GroqWhisperASR() if args.mode == "audio" else None
    meta = {"llm": "rules (no LLM)" if args.llm == "rules" else model, "hybrid": args.hybrid, "mode": args.mode,
            "audio_variant": args.audio_suffix or ("clean" if args.mode == "audio" else None),
            "tts": getattr(tts, "name", ""), "asr": getattr(asr, "name", None) if asr else None,
            "clock": NOW, "host": "2 vCPU / 7 GB RAM sandbox, no GPU"}
    results = [done[s["id"]] for s in scen if s["id"] in done]
    t_start = time.time()
    waited = 0.0
    for s in scen:
        if s["id"] in done:
            continue
        r = None
        while r is None:
            try:
                r = await run_scenario(s, args, tts, asr)
            except QuotaExhausted as e:
                wait = (_retry_after(None, str(e)) or 300) + 15
                waited += wait
                if waited > args.max_wait_s:
                    print(f"!! LLM quota exhausted at {s['id']}; gave up after waiting {waited:.0f}s. Rerun to resume.")
                    break
                print(f"   quota window full at {s['id']}; waiting {wait:.0f}s (scenario will be re-run)", flush=True)
                await asyncio.sleep(wait)
        if r is None:
            break
        results.append(r)
        print(f"{s['id']:4s} {'PASS' if r['passed'] else 'FAIL'}  {s['title'][:60]}"
              + ("" if r["passed"] else f"  {r['failed_checks']}"), flush=True)
        order = {x["id"]: i for i, x in enumerate(scen)}
        results.sort(key=lambda x: order[x["id"]])
        meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        out.write_text(json.dumps({"summary": aggregate(results, meta), "results": results}, indent=1, default=str))
        if args.sleep:
            await asyncio.sleep(args.sleep)
    summary = aggregate(results, meta)
    summary["complete"] = len(results) == len(scen)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=1, default=str))
    s = {k: v for k, v in summary.items() if k not in ("failures",)}
    print(json.dumps(s, indent=1))
    print(f"wrote {out} ({len(results)}/{len(scen)} scenarios, {time.time() - t_start:.0f}s)")


if __name__ == "__main__":
    asyncio.run(main())
