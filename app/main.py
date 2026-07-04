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

import json
import re
import secrets
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


@app.on_event("startup")
def _startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


@app.on_event("shutdown")
def _shutdown() -> None:
    # Belt-and-braces: if the process is killed while Ollama is still
    # under our control, don't leak the child.
    ollama.stop()


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
    return ollama.stop()


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


@app.post("/api/chat")
async def api_chat(p: ChatPayload):
    if not ollama.status()["ready"]:
        raise HTTPException(503, "Ollama is not running")

    # Server-side injection: guarantee the guardrail is present and that
    # note context (when scoped) is attached even if the frontend didn't
    # send it. The frontend also sends CHAT_SYSTEM — we dedupe by
    # dropping any client-supplied system messages.
    user_msgs = [m for m in p.messages if m.get("role") != "system"]
    system_msgs = [{"role": "system", "content": CHAT_SYSTEM}]

    if p.note_id is not None:
        note = db.get_note(p.note_id)
        if note:
            ctx = f'NOTE CONTEXT — title: "{note["title"]}"\n\n{note["body_text"]}'
            system_msgs.append({"role": "system", "content": ctx})

    async def stream() -> AsyncIterator[bytes]:
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
