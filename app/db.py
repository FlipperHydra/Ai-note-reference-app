"""SQLite layer for AI Notes.

Schema is created lazily on first connect. FTS5 is used as a fallback
recall path when the LLM-produced tag list finds nothing.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .settings import DB_PATH


# ─────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    body_html      TEXT NOT NULL DEFAULT '',
    body_text      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    needs_tagging  INTEGER NOT NULL DEFAULT 1,
    last_tagged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_needs   ON notes(needs_tagging);

-- Full-text index (fallback recall path)
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, body_text,
    content='notes', content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with notes
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body_text)
    VALUES (new.id, new.title, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body_text)
    VALUES('delete', old.id, old.title, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body_text)
    VALUES('delete', old.id, old.title, old.body_text);
    INSERT INTO notes_fts(rowid, title, body_text)
    VALUES (new.id, new.title, new.body_text);
END;

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id  INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    source   TEXT NOT NULL CHECK(source IN ('ai','user')) DEFAULT 'ai',
    PRIMARY KEY (note_id, tag_id),
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_note_tags_tag  ON note_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_note_tags_note ON note_tags(note_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


# ─────────────────────────────────────────────────────────────────────
# Notes
# ─────────────────────────────────────────────────────────────────────
def list_notes(query: str | None = None) -> list[dict]:
    with connect() as c:
        if query:
            rows = c.execute(
                """SELECT id, title, created_at, updated_at
                   FROM notes
                   WHERE title LIKE ?
                   ORDER BY updated_at DESC""",
                (f"%{query}%",),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT id, title, created_at, updated_at
                   FROM notes
                   ORDER BY updated_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]


def get_note(note_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return None
        note = dict(row)
        note["tags"] = _tags_for_note(c, note_id)
        return note


def create_note(title: str, body_html: str, body_text: str) -> dict:
    now = _now()
    with connect() as c:
        cur = c.execute(
            """INSERT INTO notes (title, body_html, body_text, created_at, updated_at, needs_tagging)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (title, body_html, body_text, now, now),
        )
        note_id = cur.lastrowid
    return get_note(note_id)  # type: ignore[return-value]


def update_note(note_id: int, title: str, body_html: str, body_text: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return None
        c.execute(
            """UPDATE notes
               SET title=?, body_html=?, body_text=?, updated_at=?, needs_tagging=1
               WHERE id=?""",
            (title, body_html, body_text, _now(), note_id),
        )
    return get_note(note_id)


def delete_note(note_id: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


def notes_needing_tags() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT id, title, body_text
               FROM notes
               WHERE needs_tagging = 1
               ORDER BY updated_at ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


def mark_tagged(note_id: int) -> None:
    with connect() as c:
        c.execute(
            "UPDATE notes SET needs_tagging = 0, last_tagged_at = ? WHERE id = ?",
            (_now(), note_id),
        )


# ─────────────────────────────────────────────────────────────────────
# Tags
# ─────────────────────────────────────────────────────────────────────
def _tags_for_note(c: sqlite3.Connection, note_id: int) -> list[str]:
    rows = c.execute(
        """SELECT t.name FROM tags t
           JOIN note_tags nt ON nt.tag_id = t.id
           WHERE nt.note_id = ?
           ORDER BY t.name""",
        (note_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def replace_ai_tags(note_id: int, tags: Iterable[str]) -> list[str]:
    """Delete existing AI tags for a note and insert the new set.
    User-added tags (source='user') are untouched."""
    tags = list(tags)
    with connect() as c:
        c.execute(
            """DELETE FROM note_tags
               WHERE note_id = ? AND source = 'ai'""",
            (note_id,),
        )
        for name in tags:
            c.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            tag_id = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            c.execute(
                """INSERT OR IGNORE INTO note_tags(note_id, tag_id, source)
                   VALUES (?, ?, 'ai')""",
                (note_id, tag_id),
            )
        return _tags_for_note(c, note_id)


def all_tags() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT t.name, COUNT(nt.note_id) AS count
               FROM tags t LEFT JOIN note_tags nt ON nt.tag_id = t.id
               GROUP BY t.id
               ORDER BY count DESC, t.name"""
        ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Recall
# ─────────────────────────────────────────────────────────────────────
def notes_by_tags(tags: list[str], note_id: int | None = None, limit: int = 12) -> list[dict]:
    """Rank notes by how many of the given tag names they carry."""
    if not tags:
        return []
    placeholders = ",".join("?" * len(tags))
    params: list = list(tags)
    where_note = ""
    if note_id is not None:
        where_note = "AND n.id = ?"
        params.append(note_id)
    params.append(limit)
    with connect() as c:
        rows = c.execute(
            f"""SELECT n.id, n.title, n.body_text, COUNT(*) AS score,
                       GROUP_CONCAT(t.name) AS matched
                FROM notes n
                JOIN note_tags nt ON nt.note_id = n.id
                JOIN tags t       ON t.id       = nt.tag_id
                WHERE t.name IN ({placeholders}) {where_note}
                GROUP BY n.id
                ORDER BY score DESC, n.updated_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["matched"] = (d["matched"] or "").split(",")
            out.append(d)
        return out


def fts_search(query: str, note_id: int | None = None, limit: int = 12) -> list[dict]:
    """Full-text fallback when tag lookup yields nothing.
    Uses SQLite's snippet() for highlighted excerpts."""
    if not query.strip():
        return []
    # FTS5 MATCH is picky about punctuation — split into safe tokens.
    safe = " OR ".join(
        f'"{tok}"' for tok in _fts_tokens(query) if tok
    )
    if not safe:
        return []
    params: list = [safe]
    filt = ""
    if note_id is not None:
        filt = "AND n.id = ?"
        params.append(note_id)
    params.append(limit)
    with connect() as c:
        rows = c.execute(
            f"""SELECT n.id, n.title,
                       snippet(notes_fts, 1, '<mark>', '</mark>', '…', 12) AS snippet,
                       bm25(notes_fts) AS score
                FROM notes_fts
                JOIN notes n ON n.id = notes_fts.rowid
                WHERE notes_fts MATCH ? {filt}
                ORDER BY score
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def _fts_tokens(q: str) -> list[str]:
    import re
    return [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if len(t) >= 2]
