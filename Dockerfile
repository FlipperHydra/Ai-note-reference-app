# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd --create-home --uid 1000 ainotes
WORKDIR /app

# Install Python deps first for better layer caching
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code
COPY app/       ./app/
COPY prompts/   ./prompts/
COPY templates/ ./templates/
COPY static/    ./static/
COPY scripts/   ./scripts/

# Writable dirs for DB and uploads (mounted as volumes in compose)
RUN mkdir -p /data /app/static/uploads \
 && chown -R ainotes:ainotes /app /data

USER ainotes

ENV AI_NOTES_HOST=0.0.0.0 \
    AI_NOTES_PORT=8765 \
    AI_NOTES_DB=/data/notes.db \
    AI_NOTES_UPLOADS=/data/uploads \
    AI_NOTES_OLLAMA_MANAGED=0 \
    AI_NOTES_OLLAMA_HOST=http://ollama:11434

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/ollama/status || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
