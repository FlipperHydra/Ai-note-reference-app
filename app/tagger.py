"""Shutdown tagging worker.

Yields SSE-shaped dicts as it processes each note flagged with
needs_tagging=1, then kills Ollama (in managed mode) before signalling
done. Consumed by /api/tag/run in main.py.
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

from prompts.prompts import build_tagging_messages, normalize_tags

from . import db, ollama
from .settings import TAGGER_MODEL_PREFERENCES


# Extract the first {...} JSON object from a model reply. Small local
# models occasionally wrap JSON in code fences or add stray commentary
# despite the "reply with JSON only" instruction.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_tags(reply: str) -> list[str]:
    m = _JSON_RE.search(reply or "")
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    raw = obj.get("tags", [])
    if not isinstance(raw, list):
        return []
    return normalize_tags(str(x) for x in raw)


async def run(model: str | None = None) -> AsyncIterator[dict]:
    """Async generator of SSE-shaped payloads.

    Emits:
        {"total": N}
        {"index": i, "id": note_id, "title": ..., "tags": [...]}
        {"done": True, "tagged": N}
    On fatal error yields {"error": "..."} and stops.
    """
    # Pick a tagger model if the caller didn't specify one.
    if not model:
        model = await ollama.pick_tagger_model(TAGGER_MODEL_PREFERENCES)
    pending = db.notes_needing_tags()
    yield {"total": len(pending)}

    if not pending:
        # Nothing to tag — still release model memory + stop Ollama so
        # the UX matches (idle CPU/RAM returns to zero).
        await ollama.unload_all_models()
        ollama.stop()
        yield {"done": True, "tagged": 0}
        return

    if not model:
        yield {
            "error": (
                "No local models available for tagging. "
                "Run `ollama pull llama3.2:3b` and try again."
            )
        }
        return

    tagged = 0
    for i, note in enumerate(pending, start=1):
        try:
            messages = build_tagging_messages(note["title"], note["body_text"])
            reply = await ollama.chat_once(model, messages)
            tags = _parse_tags(reply)
            if tags:
                db.replace_ai_tags(note["id"], tags)
                db.mark_tagged(note["id"])
                tagged += 1
            # If tags is empty we deliberately leave needs_tagging=1 so
            # the next shutdown retries. The user still sees an entry in
            # the UI but with no chips — a signal that the model couldn't
            # extract anything useful.
            yield {
                "index": i,
                "id": note["id"],
                "title": note["title"],
                "tags": tags,
            }
        except Exception as e:  # noqa: BLE001 — surface everything to the UI
            yield {
                "index": i,
                "id": note["id"],
                "title": note["title"],
                "tags": [],
                "error": str(e),
            }

    # Tagging finished — release model memory first (works in both managed
    # and unmanaged mode) then kill the process in managed mode.
    await ollama.unload_all_models()
    ollama.stop()
    yield {"done": True, "tagged": tagged}
