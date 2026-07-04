"""FastAPI entrypoint for AI Notes.

Wire diagram
------------
  /                  -> launcher.html
  /notes             -> notes.html
  /api/ollama/*      -> subprocess lifecycle
  /api/models        -> proxied tag list
  /api/notes[/id]    -> CRUD
  /api/upload        -> image writes to static/uploads/
  /api/tags          -> flat tag list with counts
  /api/recall        -> tag-driven recall (LLM query parser + SQL join,
                        with FTS5 fallback)
  /api/chat          -> SSE streamed chat, note context injected server-side
  /api/tag/run       -> SSE streamed shutdown tagger
"""

from __future__ import annotations

import atexit
import json
import re
import secrets
import signal
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from prompts.prompts import (
    CHAT_SYSTEM,
    build_recall_messages,
    normalize_tags,
)

from . import db, ollama, tagger
from .settings import (
    HOST,
    MAX_UPLOAD_MB,
    PORT,
    STATIC_DIR,
    TAGGER_MODEL_PREFERENCES,
    TEMPLATE_DIR,
    UPLOAD_DIR,
)

app = FastAPI(title="AI Notes", version="0.1.0")

# ─────────────────────────────────────────────────────────────────────
# Static + templates (plain FileResponse — no Jinja templating needed,
# the HTML files are static since all dynamic content is rendered by JS)
# ─────────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Guard so cleanup only runs once even if multiple hooks fire (atexit +
# signal + FastAPI shutdown all racing to release memory).
_cleanup_done = False


def _release_ollama_memory(reason: str = "shutdown") -> None:
    """Guarantee Ollama releases model memory before the app exits.

    Runs in this order, all idempotent:
      1. POST /api/generate with keep_alive=0 for every loaded model —
         evicts weights immediately instead of waiting 5m keep_alive.
         Critical in unmanaged mode (docker-compose) where we don't own
         the Ollama process.
      2. In managed mode, kill the ollama serve child process group.
         The OS then reclaims all its RAM/VRAM.

    Safe to call from signal handlers, atexit, and FastAPI shutdown.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    try:
        result = ollama.unload_all_models_sync(timeout_s=6.0)
        print(f"[ai-notes] ollama unload ({reason}): {result}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[ai-notes] ollama unload failed ({reason}): {e}", file=sys.stderr, flush=True)
    try:
        ollama.stop()
    except Exception as e:
        print(f"[ai-notes] ollama.stop failed ({reason}): {e}", file=sys.stderr, flush=True)


def _install_signal_handlers() -> None:
    """Trap the signals uvicorn/docker use for shutdown so we release memory
    even when FastAPI's on_event('shutdown') doesn't fire (e.g. abrupt SIGKILL
    isn't catchable, but SIGTERM/SIGINT/SIGHUP are).
    """
    def _handler(signum, _frame):
        _release_ollama_memory(reason=f"signal_{signum}")
        # Re-raise the default behavior so uvicorn still exits.
        signal.signal(signum, signal.SIG_DFL)
        try:
            import os
            os.kill(os.getpid(), signum)
        except Exception:
            sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # SIGHUP not available on Windows; ignore.
            pass


@app.on_event("startup")
def _startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    _install_signal_handlers()
    # Last-resort net: if the process exits without ever hitting a signal
    # or the FastAPI shutdown hook, atexit still fires.
    atexit.register(_release_ollama_memory, "atexit")


@app.on_event("shutdown")
def _shutdown() -> None:
    # Primary path on graceful uvicorn shutdown. Evicts loaded models
    # from Ollama memory, then — in managed mode — kills the child.
    _release_ollama_memory(reason="fastapi_shutdown")


@app.get("/")
def launcher() -> FileResponse:
    return FileResponse(TEMPLATE_DIR / "launcher.html")


@app.get("/notes")
def notes_page() -> FileResponse:
    return FileResponse(TEMPLATE_DIR / "notes.html")


# ═════════════════════════════════════════════════════════════════════
# Ollama lifecycle
# ═════════════════════════════════════════════════════════════════════
@app.post("/api/ollama/start")
async def api_ollama_start():
    try:
        return await ollama.start()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ollama/stop")
async def api_ollama_stop():
    # Unload models FIRST so we release RAM/VRAM even in unmanaged mode
    # where ollama.stop() is a no-op.
    unload = await ollama.unload_all_models()
    kill   = ollama.stop()
    return {"unload": unload, "stop": kill}


@app.post("/api/ollama/unload")
async def api_ollama_unload():
    """Force Ollama to evict every currently-loaded model from memory.
    Works in both managed and unmanaged mode. Leaves the ollama server
    itself running — use /api/ollama/stop to also tear that down.
    """
    return await ollama.unload_all_models()


@app.get("/api/ollama/loaded")
async def api_ollama_loaded():
    """Report which models are currently resident in Ollama's memory."""
    return {"loaded": await ollama.loaded_models()}


@app.get("/api/ollama/status")
async def api_ollama_status():
    return ollama.status()


@app.get("/api/models")
async def api_models():
    return {"models": await ollama.list_models()}


# ═════════════════════════════════════════════════════════════════════
# Notes CRUD
# ═════════════════════════════════════════════════════════════════════
class NotePayload(BaseModel):
    title:     str = Field(default="Untitled", max_length=200)
    body_html: str = Field(default="")
    body_text: str = Field(default="")


@app.get("/api/notes")
def api_list_notes(q: Optional[str] = None):
    return db.list_notes(q)


@app.get("/api/notes/{note_id}")
def api_get_note(note_id: int):
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "note not found")
    return note


@app.post("/api/notes")
def api_create_note(p: NotePayload):
    return db.create_note(p.title, p.body_html, p.body_text)


@app.put("/api/notes/{note_id}")
def api_update_note(note_id: int, p: NotePayload):
    note = db.update_note(note_id, p.title, p.body_html, p.body_text)
    if not note:
        raise HTTPException(404, "note not found")
    return note


@app.delete("/api/notes/{note_id}", status_code=204)
def api_delete_note(note_id: int):
    if not db.delete_note(note_id):
        raise HTTPException(404, "note not found")
    return Response(status_code=204)


# ═════════════════════════════════════════════════════════════════════
# Image upload
# ═════════════════════════════════════════════════════════════════════
_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, f"unsupported file type: {ext or 'unknown'}")
    # Read with a size cap so a giant paste can't blow up memory.
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_MB}MB limit")
    name = secrets.token_hex(8) + ext
    (UPLOAD_DIR / name).write_bytes(data)
    return {"url": f"/static/uploads/{name}"}


# ═════════════════════════════════════════════════════════════════════
# Tags + recall
# ═════════════════════════════════════════════════════════════════════
@app.get("/api/tags")
def api_tags():
    return db.all_tags()


_TOKEN_RE = re.compile(r"[A-Za-z0-9\-]+")


def _snippet_for(note_body: str, tokens: list[str], radius: int = 90) -> str:
    """Build a highlighted snippet around the first hit."""
    body = note_body or ""
    if not body:
        return ""
    lo = body.lower()
    hit = -1
    for t in tokens:
        t = t.lower().replace("-", " ")
        i = lo.find(t)
        if i >= 0 and (hit == -1 or i < hit):
            hit = i
            break
    if hit < 0:
        excerpt = body[: radius * 2]
    else:
        start = max(0, hit - radius)
        end = min(len(body), hit + radius)
        excerpt = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
    # HTML-escape, then highlight
    from html import escape
    safe = escape(excerpt)
    for t in tokens:
        pattern = re.compile(re.escape(t.replace("-", " ")), re.IGNORECASE)
        safe = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe)
    return safe


@app.get("/api/recall")
async def api_recall(q: str, note_id: Optional[int] = None):
    """
    Two-stage recall:
      1. LLM translates the query into candidate tags.
      2. SQL joins note_tags on those tags.
      3. If nothing matches, fall through to FTS5 on note bodies.
    If Ollama isn't running we skip straight to FTS5 so recall still works.
    """
    tag_candidates: list[str] = []
    if ollama.status()["ready"]:
        model = await ollama.pick_tagger_model(TAGGER_MODEL_PREFERENCES)
        if model:
            try:
                reply = await ollama.chat_once(model, build_recall_messages(q))
                m = re.search(r"\{.*\}", reply, re.DOTALL)
                if m:
                    obj = json.loads(m.group(0))
                    tag_candidates = normalize_tags(str(x) for x in obj.get("tags", []))
            except Exception:  # noqa: BLE001 — graceful fallback
                tag_candidates = []

    if tag_candidates:
        hits = db.notes_by_tags(tag_candidates, note_id=note_id)
        if hits:
            return [
                {
                    "id":      h["id"],
                    "title":   h["title"],
                    "snippet": _snippet_for(h["body_text"], h["matched"]),
                    "matched": h["matched"],
                    "score":   h["score"],
                }
                for h in hits
            ]

    # Fallback: full-text search
    fts = db.fts_search(q, note_id=note_id)
    return [
        {
            "id":      h["id"],
            "title":   h["title"],
            "snippet": h["snippet"],
            "matched": [],
            "score":   h["score"],
        }
        for h in fts
    ]


# ═════════════════════════════════════════════════════════════════════
# Chat (SSE stream)
# ═════════════════════════════════════════════════════════════════════
class ChatPayload(BaseModel):
    model: str
    messages: list[dict]
    note_id: Optional[int] = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _recall_hits_for(query: str, exclude_note_id: Optional[int]) -> list[dict]:
    """Same two-stage recall used by /api/recall, but returns raw hits with
    body_text so we can inject them as chat context. Falls through to FTS if
    the LLM tag translation misses or Ollama is unreachable.
    """
    tag_candidates: list[str] = []
    if ollama.status()["ready"]:
        model = await ollama.pick_tagger_model(TAGGER_MODEL_PREFERENCES)
        if model:
            try:
                reply = await ollama.chat_once(model, build_recall_messages(query))
                m = re.search(r"\{.*\}", reply, re.DOTALL)
                if m:
                    obj = json.loads(m.group(0))
                    tag_candidates = normalize_tags(str(x) for x in obj.get("tags", []))
            except Exception:  # noqa: BLE001
                tag_candidates = []

    if tag_candidates:
        hits = db.notes_by_tags(tag_candidates)
        hits = [h for h in hits if h["id"] != exclude_note_id]
        if hits:
            # Enrich with body_text for context injection
            return [
                {**h, "body_text": (db.get_note(h["id"]) or {}).get("body_text", "")}
                for h in hits[:3]
            ]

    fts = db.fts_search(query)
    fts = [h for h in fts if h["id"] != exclude_note_id]
    return [
        {**h, "body_text": (db.get_note(h["id"]) or {}).get("body_text", "")}
        for h in fts[:3]
    ]


@app.post("/api/chat")
async def api_chat(p: ChatPayload):
    if not ollama.status()["ready"]:
        raise HTTPException(503, "Ollama is not running")

    # Server-side injection: the guardrail is always first, then the current
    # note (if one is open), then any recall hits that look relevant to the
    # user's latest message. The frontend never has to think about scope —
    # the current note is ALWAYS attached, and the model can reference other
    # notes by asking questions about them (recall is auto-invoked below).
    user_msgs = [m for m in p.messages if m.get("role") != "system"]
    system_msgs: list[dict] = [{"role": "system", "content": CHAT_SYSTEM}]

    current_note = None
    if p.note_id is not None:
        current_note = db.get_note(p.note_id)
        if current_note:
            # Cap the current-note body so we don't blow the context window
            # on small local models. Matches the tagger's 6000-char cap.
            cur_body = (current_note["body_text"] or "").strip()
            if len(cur_body) > 6000:
                cur_body = cur_body[:6000] + "\n[…truncated]"
            system_msgs.append({
                "role": "system",
                "content": (
                    f'CURRENT NOTE — the user is looking at this note right now.\n'
                    f'Title: "{current_note["title"]}"\n\n{cur_body}'
                ),
            })

    # Auto-recall: use the latest user turn as a recall query. Anything we
    # find that ISN'T the current note gets attached as extra context, so
    # the model can answer cross-note questions without a separate mode.
    last_user = next(
        (m["content"] for m in reversed(user_msgs) if m.get("role") == "user"),
        "",
    )
    other_hits: list[dict] = []
    if last_user.strip():
        try:
            other_hits = await _recall_hits_for(last_user, exclude_note_id=p.note_id)
        except Exception:  # noqa: BLE001
            other_hits = []

    if other_hits:
        blocks = []
        for h in other_hits:
            body = (h.get("body_text") or "").strip()
            if len(body) > 1500:
                body = body[:1500] + "\n[…truncated]"
            blocks.append(f'--- Note: "{h["title"]}" (id={h["id"]})\n{body}')
        system_msgs.append({
            "role": "system",
            "content": (
                "OTHER RELEVANT NOTES — pulled by tag-based recall. Cite by\n"
                "title when you reference them. Do NOT invent details not\n"
                "present below.\n\n" + "\n\n".join(blocks)
            ),
        })

    async def stream() -> AsyncIterator[bytes]:
        # Signal to the client which notes we attached (for the UI chip row).
        yield _sse({
            "context": {
                "current": (
                    {"id": current_note["id"], "title": current_note["title"]}
                    if current_note else None
                ),
                "recalled": [{"id": h["id"], "title": h["title"]} for h in other_hits],
            }
        }).encode()
        try:
            async for tok in ollama.chat_stream(p.model, system_msgs + user_msgs):
                yield _sse({"token": tok}).encode()
            yield _sse({"done": True}).encode()
        except Exception as e:  # noqa: BLE001
            yield _sse({"error": str(e)}).encode()

    return StreamingResponse(stream(), media_type="text/event-stream")


# ═════════════════════════════════════════════════════════════════════
# Shutdown tagger (SSE stream)
# ═════════════════════════════════════════════════════════════════════
@app.post("/api/tag/run")
async def api_tag_run(model: Optional[str] = Body(default=None, embed=True)):
    async def stream() -> AsyncIterator[bytes]:
        async for event in tagger.run(model):
            yield _sse(event).encode()

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────
# Convenience entrypoint: `python -m app.main`
# ─────────────────────────────────────────────────────────────────────
def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
