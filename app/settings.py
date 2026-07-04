"""Runtime settings for AI Notes.

Environment variables (all optional) override the defaults, so a Docker
container or a systemd service can point the app at a different DB path
or Ollama host without editing code.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Paths ────────────────────────────────────────────────────────────
DB_PATH       = Path(os.environ.get("AI_NOTES_DB",       PROJECT_ROOT / "notes.db"))
UPLOAD_DIR    = Path(os.environ.get("AI_NOTES_UPLOADS",  PROJECT_ROOT / "static" / "uploads"))
STATIC_DIR    = PROJECT_ROOT / "static"
TEMPLATE_DIR  = PROJECT_ROOT / "templates"

# ── HTTP ─────────────────────────────────────────────────────────────
HOST = os.environ.get("AI_NOTES_HOST", "127.0.0.1")
PORT = int(os.environ.get("AI_NOTES_PORT", "8000"))

# ── Ollama ───────────────────────────────────────────────────────────
# When AI_NOTES_OLLAMA_MANAGED=1 (default) the app spawns and kills
# `ollama serve` itself. When 0, it assumes Ollama is already running
# (useful for docker-compose where Ollama is its own service).
OLLAMA_MANAGED = os.environ.get("AI_NOTES_OLLAMA_MANAGED", "1") == "1"
OLLAMA_HOST    = os.environ.get("AI_NOTES_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_BIN     = os.environ.get("AI_NOTES_OLLAMA_BIN", "ollama")

# Health-check timeouts
OLLAMA_START_TIMEOUT_S = int(os.environ.get("AI_NOTES_OLLAMA_TIMEOUT", "30"))

# ── Tagger ───────────────────────────────────────────────────────────
# Which model to use for background tagging when the user hasn't picked
# one explicitly. We look for these in order and use the first installed.
TAGGER_MODEL_PREFERENCES = [
    "llama3.2:3b",
    "llama3.2",
    "phi3:mini",
    "phi3",
    "mistral:7b",
    "mistral",
]

# Upload limits
MAX_UPLOAD_MB = int(os.environ.get("AI_NOTES_MAX_UPLOAD_MB", "10"))
