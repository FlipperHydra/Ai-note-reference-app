# AI Notes — Reference App

A **fully offline**, AI-assisted notes app that runs on your own machine. FastAPI + SQLite + Ollama, wrapped in a Google-Docs-style web UI. No cloud, no telemetry, no internet required once the model is pulled.

- **Exact recall by tags** — the AI never scans every note. It translates your question into tags, and SQLite does the lookup. FTS5 full-text is the fallback when tags miss.
- **Tag writing happens at shutdown** — while you're typing, the AI stays out of your way. When you close the app, a short "writing tags…" screen runs the tagger, then kills Ollama.
- **Chat that quotes notes, never edits them** — the assistant is guardrailed to a read-only role. If you want the AI's reply saved, you click "Save reply as new note."
- **Rich-text editor** — Quill 1.3.7 with paste-from-Word support and inline image upload.
- **Model picker** — any installed Ollama model. Small models are fine for tagging; use a larger one for chat if you have the RAM.

---

## Quickstart

Requires Python 3.10+ and [Ollama](https://ollama.com) installed locally.

```bash
git clone https://github.com/FlipperHydra/Ai-note-reference-app.git
cd Ai-note-reference-app
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Pull at least one model (once, requires internet — this is the only online step)
ollama pull llama3.2:3b

# Run
bash scripts/run.sh
# or:  uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765 in your browser. You'll land on the **launcher** — click **Start Application**, wait for Ollama to come up, then you're in the notes page.

When you're done, click **Stop** in the top bar. The tagger runs on any notes you edited, then Ollama is killed. Nothing keeps running in the background.

---

## Architecture

```
┌─────────────────┐        ┌─────────────────────┐        ┌──────────────┐
│  Browser        │  HTTP  │  FastAPI (app/)     │  HTTP  │  Ollama      │
│  Quill editor   │◀──────▶│  SQLite + FTS5      │◀──────▶│  local model │
│  static/        │  SSE   │  Tagger worker      │        │  (subprocess)│
└─────────────────┘        └─────────────────────┘        └──────────────┘
                                     │
                                     ▼
                              notes.db (SQLite)
```

### Backend layout (`app/`)

| File          | Responsibility                                                              |
| ------------- | --------------------------------------------------------------------------- |
| `settings.py` | Env-driven config: DB path, host, port, Ollama managed/host                 |
| `db.py`       | SQLite schema (notes, tags, note_tags, notes_fts), CRUD, tag joins, FTS     |
| `ollama.py`   | Subprocess lifecycle (`start`/`stop`/`status`), `chat_once`, `chat_stream`  |
| `tagger.py`   | Shutdown tagging worker — streams SSE progress, then kills Ollama           |
| `main.py`     | Routes: pages, Ollama control, models, notes CRUD, upload, recall, chat    |

### Data model

```sql
notes(id, title, body_html, body_text, created_at, updated_at,
      needs_tagging, last_tagged_at)
notes_fts   -- FTS5 virtual table over (title, body_text), synced via triggers
tags(id, name UNIQUE COLLATE NOCASE)
note_tags(note_id, tag_id, source CHECK IN ('ai','user'))
```

The `source` column matters: **user-added tags are never touched by the AI re-tagger**. When you edit a note, `needs_tagging` flips to 1; the next shutdown pass will re-run AI tags on that note, but any tags you added by hand stay put.

### Recall flow

1. User types "where did I make notes about dragons?"
2. `/api/recall` calls Ollama with `build_recall_messages(query)` → returns `{"tags": ["fantasy", "story-idea", "dragons"]}`
3. Backend joins `note_tags` for those tags, returns matching notes.
4. If the tag join is empty **or Ollama is unreachable**, it falls back to FTS5 full-text search.

The AI never sees the note bodies during recall — only the query and tag list. Exact, fast, deterministic.

---

## API surface

| Method | Path                    | Purpose                                                   |
| ------ | ----------------------- | --------------------------------------------------------- |
| GET    | `/`                     | Launcher page                                             |
| GET    | `/notes`                | Notes app (also handles `?shutdown=1` for tagger screen)  |
| GET    | `/api/ollama/status`    | `{running, pid, host}`                                    |
| POST   | `/api/ollama/start`     | Spawn `ollama serve` (managed mode)                       |
| POST   | `/api/ollama/stop`      | Kill spawned Ollama                                       |
| GET    | `/api/models`           | List installed models                                     |
| GET    | `/api/notes`            | List notes                                                |
| POST   | `/api/notes`            | Create note                                               |
| GET    | `/api/notes/{id}`       | Fetch one note (with tags)                                |
| PUT    | `/api/notes/{id}`       | Update note (flips `needs_tagging=1`)                     |
| DELETE | `/api/notes/{id}`       | Delete                                                    |
| POST   | `/api/upload`           | Upload image → returns URL under `/static/uploads/`       |
| GET    | `/api/tags`             | List all tags                                             |
| POST   | `/api/recall`           | `{query} → {tags, notes}` (LLM → tag join → FTS fallback) |
| POST   | `/api/chat`             | SSE stream of assistant reply                             |
| POST   | `/api/tag/run`          | SSE stream — runs tagger on dirty notes, then stops Ollama |

---

## Prompts

All in `prompts/prompts.py`:

- **`CHAT_SYSTEM`** — locks the assistant to read-only. It can quote and summarize but must refuse to "update" or "save" notes; that's a UI-side action.
- **`build_tagging_messages(title, body_text)`** — returns strict JSON `{"tags": [...]}`. 3–8 kebab-case tags per note, always includes one doc-type tag (`story-idea`, `meeting-notes`, `recipe`, `essay-draft`, `cheatsheet`, `reading-notes`).
- **`build_recall_messages(user_query)`** — same rules, with a worked example: a query about dragons returns `{"tags": ["fantasy", "story-idea", "dragons"]}`.
- **`normalize_tag` / `normalize_tags`** — kebab-case; drops stopwords, digit-only strings, and anything too short or too long.

---

## Configuration

Set env vars before launch (or edit `app/settings.py`):

| Var                          | Default                     | Meaning                                            |
| ---------------------------- | --------------------------- | -------------------------------------------------- |
| `AI_NOTES_HOST`              | `127.0.0.1`                 | Bind address                                       |
| `AI_NOTES_PORT`              | `8765`                      | Bind port                                          |
| `AI_NOTES_DB`                | `./notes.db`                | SQLite file location                               |
| `AI_NOTES_UPLOADS`           | `./static/uploads`          | Where inline images land                           |
| `AI_NOTES_OLLAMA_HOST`       | `http://127.0.0.1:11434`    | Ollama URL                                         |
| `AI_NOTES_OLLAMA_MANAGED`    | `1`                         | `1` = app spawns/kills Ollama, `0` = external      |

Set `AI_NOTES_OLLAMA_MANAGED=0` when Ollama runs as its own service (e.g. in Docker or systemd). See `DEPLOYMENT.md`.

---

## Testing

```bash
python -m pytest tests/ -x
```

The smoke suite hits every route with an in-memory DB, verifies the tag join, and confirms the FTS fallback triggers when Ollama is unreachable.

---

## License

MIT — see [LICENSE](LICENSE). Built by Seth Clayton (@FlipperHydra).
