PY ?= python
.PHONY: install dataset audio test lint eval-rules eval-llm eval-hybrid eval-audio asr-eval serve docker

install:      ; $(PY) -m pip install -r requirements-dev.txt
dataset:      ; $(PY) scripts/make_dataset.py && $(PY) scenarios/suite.py
voices:
	mkdir -p models/piper && cd models/piper && \
	for v in en/en_US/lessac/low/en_US-lessac-low en/en_GB/alan/low/en_GB-alan-low; do \
	  curl -sfL -O https://huggingface.co/rhasspy/piper-voices/resolve/main/$$v.onnx; \
	  curl -sfL -O https://huggingface.co/rhasspy/piper-voices/resolve/main/$$v.onnx.json; done
audio:        ; $(PY) -m scripts.gen_audio
test:         ; $(PY) -m pytest -q
lint:         ; ruff check .
eval-rules:   ; $(PY) -m scripts.run_eval --llm rules
eval-llm:     ; LLM_MODELS=openai/gpt-oss-120b $(PY) -m scripts.run_eval --llm groq
eval-hybrid:  ; LLM_MODELS=openai/gpt-oss-120b $(PY) -m scripts.run_eval --llm groq --hybrid
eval-audio:   ; LLM_MODELS=openai/gpt-oss-120b $(PY) -m scripts.run_eval --llm groq --mode audio
asr-eval:     ; $(PY) -m scripts.asr_eval
serve:        ; uvicorn app.server:app --port 8000
docker:       ; docker build -t trattoria-voice-agent . && docker run -p 8000:8000 -e GROQ_API_KEY trattoria-voice-agent
