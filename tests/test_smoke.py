"""Minimal smoke tests — verify the DB layer and prompts can be imported
and exercised without Ollama actually running."""

from __future__ import annotations

import os
import tempfile

# Point DB at a throwaway file BEFORE importing the app modules.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AI_NOTES_DB"] = _tmp.name

from app import db  # noqa: E402
from prompts.prompts import (  # noqa: E402
    build_recall_messages,
    build_tagging_messages,
    normalize_tag,
    normalize_tags,
)


def test_db_roundtrip():
    db.init_db()
    n = db.create_note("Dragons", "<p>hi</p>", "hi dragons fantasy")
    assert n["id"] > 0
    got = db.get_note(n["id"])
    assert got and got["title"] == "Dragons"
    db.replace_ai_tags(n["id"], ["fantasy", "dragons", "story-idea"])
    got = db.get_note(n["id"])
    assert set(got["tags"]) == {"fantasy", "dragons", "story-idea"}
    hits = db.notes_by_tags(["dragons"])
    assert len(hits) == 1
    assert db.delete_note(n["id"]) is True


def test_fts_fallback():
    db.init_db()
    n = db.create_note("Docker networks", "", "bridge overlay host networking")
    hits = db.fts_search("overlay")
    assert any(h["id"] == n["id"] for h in hits)


def test_normalize_tag():
    assert normalize_tag("Fantasy Story Ideas!!!") == "fantasy-story-ideas"
    assert normalize_tag("note") is None          # stopword
    assert normalize_tag("2024") is None          # all digits
    assert normalize_tag("a") is None             # too short
    assert normalize_tag("one two three four") is None  # too many words


def test_normalize_tags_dedupes():
    out = normalize_tags(["Dragons", "dragons", "  Dragons  ", "fantasy"])
    assert out == ["dragons", "fantasy"]


def test_prompts_build():
    m1 = build_tagging_messages("Dragons", "story about dragons")
    assert m1[0]["role"] == "system"
    assert "JSON" in m1[0]["content"]
    m2 = build_recall_messages("where are my dragon notes?")
    assert m2[-1]["role"] == "user"
