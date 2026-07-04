#!/usr/bin/env bash
# Development runner. Assumes ollama is installed on PATH — the app
# spawns it on demand from the launcher screen.
set -euo pipefail
cd "$(dirname "$0")/.."
export AI_NOTES_HOST="${AI_NOTES_HOST:-127.0.0.1}"
export AI_NOTES_PORT="${AI_NOTES_PORT:-8000}"
exec uvicorn app.main:app --host "$AI_NOTES_HOST" --port "$AI_NOTES_PORT" --reload
