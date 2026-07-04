// chat.js — the right-rail companion.
//   Recall mode → hits /api/recall (SQLite FTS, no LLM, exact tokens)
//   Chat mode   → streams from Ollama with a system prompt that forbids
//                 modifying source notes.

(function () {
  "use strict";

  const log        = document.getElementById("chat-log");
  const form       = document.getElementById("chat-form");
  const input      = document.getElementById("chat-input");
  const modelSel   = document.getElementById("chat-model");
  const scopeCb    = document.getElementById("scope-current");
  const tabs       = document.querySelectorAll(".mode-btn");

  const SYSTEM_PROMPT =
    "You are a read-only assistant for the user's local notes. " +
    "You may quote, summarize, and re-explain the note contents provided, " +
    "but you must NEVER invent additions to a note or claim to have edited " +
    "one. If asked to modify a note, respond with the proposed change as " +
    "plain text and remind the user to press 'Save reply as new note' to " +
    "keep the original untouched.";

  const state = {
    mode: "recall",       // "recall" | "chat"
    history: [],          // in-memory only; chat isn't persisted for now
    aborter: null,
  };

  // ─────────── Mode switching ───────────
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabs.forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      state.mode = btn.dataset.mode;
      input.placeholder =
        state.mode === "recall"
          ? "Search your notes exactly (FTS, no AI matching)…"
          : "Ask about the current note. AI won't modify it.";
    });
  });

  // ─────────── Model list ───────────
  async function loadModels() {
    try {
      const { models } = await API.listModels();
      modelSel.innerHTML = "";
      if (!models || !models.length) {
        modelSel.innerHTML = '<option value="">no model available</option>';
        return;
      }
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.name;
        modelSel.appendChild(opt);
      }
      try {
        const last = sessionStorage.getItem("ai-notes:model");
        if (last && [...modelSel.options].some((o) => o.value === last)) {
          modelSel.value = last;
        }
      } catch (_) {}
    } catch (err) {
      modelSel.innerHTML = '<option value="">Ollama offline</option>';
    }
  }
  modelSel.addEventListener("change", () => {
    try { sessionStorage.setItem("ai-notes:model", modelSel.value); } catch (_) {}
  });

  // ─────────── Rendering helpers ───────────
  function appendMsg(role, htmlOrText, isHtml = false) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "user" ? "You" : role === "assistant" ? "AI" : "System";
    const body = document.createElement("div");
    body.className = "body";
    if (isHtml) body.innerHTML = htmlOrText;
    else body.textContent = htmlOrText;
    div.appendChild(who);
    div.appendChild(body);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return body;
  }

  function renderRecallHits(hits) {
    if (!hits.length) {
      appendMsg("assistant", "No exact matches. Try different keywords.");
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = `Recall · ${hits.length} match${hits.length === 1 ? "" : "es"}`;
    wrap.appendChild(who);
    for (const h of hits) {
      const card = document.createElement("div");
      card.className = "recall-hit";
      card.dataset.testid = `hit-note-${h.id}`;
      card.innerHTML =
        `<div class="hit-title">${escapeHtml(h.title || "Untitled")}</div>` +
        `<div class="hit-snippet">${h.snippet /* server-sanitized */}</div>`;
      card.addEventListener("click", () => {
        // Fire a synthetic click on the corresponding history-rail entry.
        const target = document.querySelector(
          `#note-list li[data-id="${h.id}"]`
        );
        if (target) target.click();
      });
      wrap.appendChild(card);
    }
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  function renderAssistantStream() {
    // create the shell, return two callbacks: onToken + finalize
    const shell = document.createElement("div");
    shell.className = "msg assistant";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = "AI";
    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML = '<span class="thinking">thinking</span>';
    shell.appendChild(who);
    shell.appendChild(body);
    log.appendChild(shell);
    log.scrollTop = log.scrollHeight;

    let raw = "";
    let started = false;
    return {
      onToken(tok) {
        if (!started) {
          body.innerHTML = "";
          started = true;
        }
        raw += tok;
        // Re-render markdown safely on each token. For very long replies this
        // is fine — DOMPurify + marked are fast enough for note-sized text.
        body.innerHTML = DOMPurify.sanitize(marked.parse(raw));
        log.scrollTop = log.scrollHeight;
      },
      finalize() {
        if (!started) {
          body.textContent = "(no response)";
          return;
        }
        // Add a "Save reply as new note" affordance — keeps the guardrail
        // explicit: AI never writes to notes without the user's click.
        const save = document.createElement("button");
        save.className = "btn ghost small save-as-note";
        save.type = "button";
        save.textContent = "Save reply as new note";
        save.dataset.testid = "button-save-reply";
        save.addEventListener("click", () => {
          const firstLine = raw.split("\n").find((l) => l.trim()) || "AI reply";
          window.createNoteFromChat(firstLine.slice(0, 80), raw);
        });
        shell.appendChild(save);
      },
    };
  }

  // ─────────── Submit handler ───────────
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    appendMsg("user", q);

    const noteId = scopeCb.checked ? (window.__currentNote ?? null) : null;

    if (state.mode === "recall") {
      try {
        const hits = await API.recall(q, noteId);
        renderRecallHits(hits || []);
      } catch (err) {
        appendMsg("assistant", "Recall failed: " + err.message);
      }
      return;
    }

    // Chat mode
    const model = modelSel.value;
    if (!model) {
      appendMsg("assistant", "Pick a model first (or start Ollama).");
      return;
    }

    // Build message list. The backend also injects the note context, but
    // we send our conversation history so multi-turn works.
    const messages = [
      { role: "system", content: SYSTEM_PROMPT },
      ...state.history,
      { role: "user", content: q },
    ];
    state.history.push({ role: "user", content: q });

    const renderer = renderAssistantStream();
    let acc = "";
    state.aborter = API.streamChat({
      model,
      messages,
      noteId,
      onToken(t) {
        acc += t;
        renderer.onToken(t);
      },
      onDone() {
        renderer.finalize();
        state.history.push({ role: "assistant", content: acc });
        state.aborter = null;
      },
      onError(err) {
        renderer.onToken("\n\n**[error]** " + err.message);
        renderer.finalize();
        state.aborter = null;
      },
    });
  });

  // Cmd/Ctrl+Enter to send
  input.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ─────────── Init ───────────
  loadModels();

  // If the user switches notes, clear conversation history so the AI doesn't
  // conflate contexts. Recall hits are already scoped per-query.
  window.addEventListener("note-loaded", () => {
    state.history = [];
  });
})();
