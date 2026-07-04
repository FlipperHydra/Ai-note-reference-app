// chat.js — the right-rail AI chat.
//
// Design after the mode-tab redesign:
//   * ONE surface. No "Recall" vs "Chat" toggle.
//   * The chat is always about the current note (whichever one is open in
//     the editor). Its title is shown in the header; on note switch, we
//     clear the conversation so contexts don't bleed.
//   * If the user asks about a different note ("what did I write about
//     dragons?"), the backend does tag-based recall on the message and
//     injects the top hits as extra context. No user action needed.
//   * The frontend emits an SSE-tagged "context" event at the start of
//     each turn so we can render a small chip row showing which notes
//     the AI is actually looking at.

(function () {
  "use strict";

  const log         = document.getElementById("chat-log");
  const form        = document.getElementById("chat-form");
  const input       = document.getElementById("chat-input");
  const modelSel    = document.getElementById("chat-model");
  const scopeLabel  = document.getElementById("chat-scope-label");
  const btnClear    = document.getElementById("btn-clear-chat");

  const state = {
    history: [],   // {role, content}[]  — in-memory only
    aborter: null,
  };

  // ─────────── Model dropdown ───────────
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
    } catch (_) {
      modelSel.innerHTML = '<option value="">Ollama offline</option>';
    }
  }
  modelSel.addEventListener("change", () => {
    try { sessionStorage.setItem("ai-notes:model", modelSel.value); } catch (_) {}
  });

  // ─────────── Scope label (shows the current note's title) ───────────
  function refreshScopeLabel() {
    const title = window.__currentNoteTitle;
    if (window.__currentNote && title) {
      scopeLabel.textContent = `About: ${title}`;
      scopeLabel.title = title;
    } else if (window.__currentNote) {
      scopeLabel.textContent = "About: Untitled";
    } else {
      scopeLabel.textContent = "No note open";
    }
  }

  // ─────────── Message rendering ───────────
  function appendMsg(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "user" ? "You" : "AI";
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    div.appendChild(who);
    div.appendChild(body);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return body;
  }

  function renderContextChips(ctx) {
    // ctx: { current: {id, title}|null, recalled: [{id, title}, ...] }
    const chips = [];
    if (ctx.current) chips.push({ id: ctx.current.id, title: ctx.current.title, kind: "current" });
    for (const r of ctx.recalled || []) chips.push({ ...r, kind: "recalled" });
    if (!chips.length) return;

    const row = document.createElement("div");
    row.className = "context-row";
    row.dataset.testid = "row-context";
    for (const c of chips) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "context-chip " + c.kind;
      chip.dataset.testid = `chip-note-${c.id}`;
      chip.textContent = (c.kind === "current" ? "▸ " : "↳ ") + (c.title || "Untitled");
      chip.title =
        c.kind === "current"
          ? "The note you're editing (attached automatically)"
          : "Pulled from your notes by tag recall — click to open";
      chip.addEventListener("click", () => {
        const target = document.querySelector(`#note-list li[data-id="${c.id}"]`);
        if (target) target.click();
      });
      row.appendChild(chip);
    }
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  function renderAssistantStream() {
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
        body.innerHTML = DOMPurify.sanitize(marked.parse(raw));
        log.scrollTop = log.scrollHeight;
      },
      finalize() {
        if (!started) {
          body.textContent = "(no response)";
          return;
        }
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
      getRaw() { return raw; },
    };
  }

  // ─────────── Submit ───────────
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;

    const model = modelSel.value;
    if (!model) {
      appendMsg("assistant", "Pick a model first (or start Ollama).");
      return;
    }

    input.value = "";
    appendMsg("user", q);

    // The current note (if any) is ALWAYS attached — the backend handles
    // that from note_id. No scope toggle to think about.
    const noteId = window.__currentNote ?? null;

    const messages = [
      ...state.history,
      { role: "user", content: q },
    ];
    state.history.push({ role: "user", content: q });

    let renderer = null;
    let acc = "";

    state.aborter = API.streamChat({
      model,
      messages,
      noteId,
      onContext(ctx) {
        renderContextChips(ctx);
      },
      onToken(t) {
        if (!renderer) renderer = renderAssistantStream();
        acc += t;
        renderer.onToken(t);
      },
      onDone() {
        if (!renderer) renderer = renderAssistantStream();
        renderer.finalize();
        state.history.push({ role: "assistant", content: acc });
        state.aborter = null;
      },
      onError(err) {
        if (!renderer) renderer = renderAssistantStream();
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

  btnClear.addEventListener("click", () => {
    if (state.aborter) { try { state.aborter(); } catch (_) {} state.aborter = null; }
    state.history = [];
    log.innerHTML = "";
  });

  // ─────────── Init ───────────
  loadModels();
  refreshScopeLabel();

  // Editor tells us which note is loaded — clear history on switch so the
  // AI isn't answering with stale note context.
  window.addEventListener("note-loaded", () => {
    state.history = [];
    log.innerHTML = "";
    refreshScopeLabel();
  });

  // Live title updates while the user types in the title field.
  window.addEventListener("note-title-changed", refreshScopeLabel);
})();
