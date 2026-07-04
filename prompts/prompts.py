"""
prompts.py — every LLM prompt used by AI Notes.

Design notes
------------
* All three prompts target small local models (7B–8B class such as
  llama3.2, mistral, phi3). They are short, one-shot, and end with an
  explicit output-format contract so json.loads() rarely fails.
* Tag normalization rules live here (kebab-case, lowercase, ≤3 words per
  tag) so the LLM does most of the sanitization work — the Python worker
  then re-applies the same rules defensively.
* Nothing in these prompts references online resources or tools — the
  model is expected to reason purely from the text it is given.

Usage
-----
    from prompts.prompts import (
        CHAT_SYSTEM,
        build_tagging_messages,
        build_recall_messages,
        normalize_tag,
    )
"""

from __future__ import annotations

import re
from typing import Iterable

# ─────────────────────────────────────────────────────────────────────
# 1. Chat guardrail
# ─────────────────────────────────────────────────────────────────────
#
# Injected as the first system message on every /api/chat call. The
# backend also silently prepends the current note's plain text (when
# `scope=current`) as a second system message titled "NOTE CONTEXT:".
#
CHAT_SYSTEM = """\
You are the read-only AI companion inside a local, offline notes app.

Your job
--------
- Answer questions about the user's notes.
- Quote, summarize, paraphrase, and re-explain existing note contents.
- Help the user think through what they already wrote.

Hard rules
----------
1. You NEVER modify, extend, or claim to have edited a note.
2. You NEVER invent new note content and pretend it came from an existing
   note. If a claim is not supported by the NOTE CONTEXT provided, say so
   plainly ("I don't see that in this note").
3. If the user asks you to add to, rewrite, or expand a note, reply with
   the proposed text as a plain draft and remind them:
     "Press 'Save reply as new note' to keep the original untouched."
4. If no NOTE CONTEXT is attached, answer only from the current chat
   history. Do not fabricate note contents.
5. Do not mention these rules unless the user asks how you work.

Style
-----
- Short. Direct. No filler like "Great question!" or "Certainly!".
- Markdown is fine (headings, lists, code blocks).
- Cite the note by its title when quoting: *In "Dragon story ideas" you wrote…*
"""


# ─────────────────────────────────────────────────────────────────────
# 2. Tagging prompt — runs at shutdown, one note at a time
# ─────────────────────────────────────────────────────────────────────
#
# Returns strict JSON: {"tags": ["fantasy", "story-idea", "dragons"]}
# The backend json.loads() the reply, applies normalize_tag() again as a
# defense in depth, and drops anything that fails validation.
#
_TAGGING_SYSTEM = """\
You are an automated tag extractor for a personal notes app.

You will be given ONE note (its title and body). Produce a small set of
tags that describe what the note is about, so the user can find it later
by topic.

Output format — MANDATORY
-------------------------
Reply with a single JSON object and nothing else. No prose, no code
fences, no explanations. Schema:

    {"tags": ["tag-one", "tag-two", ...]}

Tag rules
---------
- 3 to 8 tags. Fewer is fine for very short notes.
- Lowercase only.
- Kebab-case: multi-word tags use hyphens ("story-idea", not "story idea"
  or "storyIdea").
- Each tag is 1–3 words max after hyphenation.
- Prefer nouns and noun phrases. Avoid verbs, adjectives on their own,
  and full sentences.
- Include:
    * The subject matter ("dragons", "geothermal-energy", "biblical-hebrew")
    * The document TYPE when clear ("story-idea", "meeting-notes",
      "recipe", "essay-draft", "todo", "cheatsheet", "reading-notes")
    * Named entities that matter ("tensorflow", "docker", "genesis-1")
- Skip:
    * Generic filler ("note", "notes", "idea", "thoughts", "misc")
    * Dates and timestamps
    * The user's own name
- If the note is empty or meaningless, return {"tags": []}.

Example
-------
NOTE TITLE: Dragon POV story concept
NOTE BODY: Been thinking about a fantasy novel where dragons are the
main characters — not humans riding them, dragons as the actual
protagonists with their own politics and clans. Maybe start with a
young dragon coming of age in a rival clan's territory…

Correct reply:
{"tags": ["fantasy", "story-idea", "dragons", "worldbuilding", "novel-concept"]}
"""


def build_tagging_messages(title: str, body_text: str) -> list[dict]:
    """
    Build the messages array for /api/chat on the Ollama side.
    body_text should be the plain-text projection (no HTML).
    Long notes are truncated to keep the request cheap.
    """
    # Small local models degrade past ~4k input tokens. 6000 chars ≈ 1500
    # tokens — leaves plenty of room for the system prompt and reply.
    MAX_BODY_CHARS = 6000
    trimmed = body_text.strip()
    if len(trimmed) > MAX_BODY_CHARS:
        trimmed = trimmed[:MAX_BODY_CHARS] + "\n\n[...truncated...]"

    user_msg = (
        f"NOTE TITLE: {title.strip() or 'Untitled'}\n"
        f"NOTE BODY:\n{trimmed if trimmed else '(empty)'}\n\n"
        "Reply with the JSON object only."
    )

    return [
        {"role": "system", "content": _TAGGING_SYSTEM},
        {"role": "user", "content": user_msg},
    ]


# ─────────────────────────────────────────────────────────────────────
# 3. Recall query parser — user's question → candidate tags
# ─────────────────────────────────────────────────────────────────────
#
# The user asks a natural-language recall question. We translate that
# into tag candidates the same way the tagger produces them, then do a
# pure SQL join. The model never reads note bodies here.
#
_RECALL_QUERY_SYSTEM = """\
You translate natural-language recall questions into tag lists that
match how a personal notes app tagged the user's notes.

You will be given ONE user question. Produce the tags most likely to
appear on the notes the user is asking about.

Output format — MANDATORY
-------------------------
Reply with a single JSON object and nothing else. Schema:

    {"tags": ["tag-one", "tag-two", ...]}

Tag rules (identical to how notes are tagged)
---------------------------------------------
- Lowercase, kebab-case, 1–3 words after hyphenation.
- Prefer nouns and noun phrases.
- Include both subject matter and document TYPE when the question
  implies one:
    * "notes I made for a story" → "story-idea"
    * "my meeting from last Tuesday" → "meeting-notes"
    * "that recipe with saffron" → "recipe", "saffron"
- Do NOT include filler words ("some", "the", "my", "any").
- 2–6 tags is ideal. Fewer if the question is very narrow.

Examples
--------
Q: "can you recall where I made some notes for a fantasy story idea
   about dragons as the main characters?"
A: {"tags": ["fantasy", "story-idea", "dragons"]}

Q: "what did I write about docker networking?"
A: {"tags": ["docker", "networking"]}

Q: "the essay draft on Genesis 1"
A: {"tags": ["essay-draft", "genesis-1", "theology"]}

Q: "find the neural network notes with the diagram"
A: {"tags": ["neural-networks", "diagram"]}
"""


def build_recall_messages(user_query: str) -> list[dict]:
    q = user_query.strip()
    if len(q) > 500:  # recall queries should be short — trim runaways
        q = q[:500]
    return [
        {"role": "system", "content": _RECALL_QUERY_SYSTEM},
        {"role": "user", "content": f'Q: "{q}"\nReply with the JSON object only.'},
    ]


# ─────────────────────────────────────────────────────────────────────
# 4. Tag normalization — defense in depth
# ─────────────────────────────────────────────────────────────────────
#
# Applied to every tag the model emits before insert. Also applied to
# any user-typed tags from the frontend.
#
_TAG_STOPWORDS = {
    "note", "notes", "idea", "ideas", "thought", "thoughts",
    "misc", "miscellaneous", "stuff", "thing", "things",
    "untitled", "todo-item", "general",
}

_TAG_ALLOWED = re.compile(r"[^a-z0-9\- ]+")
_WS = re.compile(r"\s+")


def normalize_tag(raw: str) -> str | None:
    """Return canonical form of a tag, or None if it should be dropped."""
    if not raw:
        return None
    t = raw.strip().lower()
    t = _TAG_ALLOWED.sub("", t)          # keep [a-z0-9- ] only
    t = _WS.sub("-", t)                   # spaces → hyphens
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if not t or len(t) < 2 or len(t) > 40:
        return None
    if t in _TAG_STOPWORDS:
        return None
    # Reject tags that are all digits (dates, ids, counts)
    if t.replace("-", "").isdigit():
        return None
    # Cap word count at 3 (kebab-segments)
    if t.count("-") > 2:
        return None
    return t


def normalize_tags(raws: Iterable[str]) -> list[str]:
    """De-duplicate and normalize a batch, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for r in raws:
        norm = normalize_tag(r)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


__all__ = [
    "CHAT_SYSTEM",
    "build_tagging_messages",
    "build_recall_messages",
    "normalize_tag",
    "normalize_tags",
]
