# Resume bullets (measured numbers only)

Every number traces to a file in `reports/` (named in brackets). Model is named on every LLM figure.

* Built a streaming restaurant voice agent (Whisper ASR → LLM tool calling → SQLite tools → Piper TTS) over a FastAPI WebSocket, with a 44-scenario evaluation harness and 85 offline pytest tests.
* Tool-calling agent on Groq gpt-oss-safeguard-20b: 31/37 (84%) scenarios fully passed, tool-call accuracy 31/34 (91%) (partial run: 37 of 44 scenarios completed) [results_gpt-oss-safeguard-20b_text.json].
* Ran the same suite on a local 3B model (Ollama qwen2.5:3b, CPU only): 33/44 (75%) task completion; a hybrid mode (LLM plans tool calls, templates speak) raised it to 36/44 (82%) with tool accuracy 30/36 (83%) and handoff accuracy 100% [results_qwen2.5-3b-ollama_*_text.json].
* Allergen guardrail (verified-records-only answers + claim auditor): 0 unsafe allergen claims reached the caller across all 8 measured runs (rules, two LLMs, text and audio); the guard caught and rewrote 3 unsafe qwen2.5:3b drafts in the text run (the only scenario-regex flag in any run was an over-cautious "can't confirm" on a verified dairy-free dish, qwen2.5:3b, A02).
* Deterministic no-LLM baseline: 40/44 (91%) task completion, tool accuracy 33/36 (92%), handoff accuracy 100% [results_rules_text.json].
* Speech in the loop (11 scenarios as synthetic caller audio → Groq whisper-large-v3-turbo with a menu vocabulary prompt): WER 1.9% clean / 5.2% at 5 dB SNR [asr_eval_menu.json]; end-to-end task completion 11/11 (100%) clean and 9/11 (82%) noisy (rules), 9/11 (82%) / 8/11 (73%) (qwen2.5:3b).
* Latency (rules agent, clean audio, 2 vCPU no GPU): ASR p50 279 ms, first answer audio p50 575 ms / p95 887 ms; Groq gpt-oss-safeguard-20b LLM call p50 309 ms / p95 435 ms [results_rules_audio.json, results_gpt-oss-safeguard-20b_text.json].
