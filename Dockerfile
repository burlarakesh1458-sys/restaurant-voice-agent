FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OMP_NUM_THREADS=2
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
# Piper voice for the agent (local TTS, CPU)
RUN mkdir -p models/piper && cd models/piper \
 && curl -sfL -O https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx \
 && curl -sfL -O https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx.json
COPY app ./app
COPY data/*.csv ./data/
COPY web ./web
COPY scenarios ./scenarios
COPY scripts ./scripts
RUN useradd -m agent && chown -R agent /app
USER agent
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s CMD curl -fs localhost:8000/health || exit 1
# GROQ_API_KEY set -> Groq Whisper + gpt-oss; unset -> offline rule-based agent (no LLM) with typed input
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
