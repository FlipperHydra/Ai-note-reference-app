// editor.js — Quill init, note CRUD, autosave, image upload, history rail.

(function () {
  "use strict";

  // ─────────── State ───────────
  const state = {
    currentId: null,          // id of the loaded note, null = new/unsaved
    dirty: false,
    saveTimer: null,
    notes: [],                // history cache
    filter: "",
  };

  // ─────────── Elements ───────────
  const titleInput   = document.getElementById("note-title");
  const saveState    = document.getElementById("save-state");
  const btnDelete    = document.getElementById("btn-delete");
  const btnNew       = document.getElementById("btn-new");
  const btnStopApp   = document.getElementById("btn-stop-app");
  const filterInput  = document.getElementById("filter-input");
  const noteListEl   = document.getElementById("note-list");
  const imagePicker  = document.getElementById("image-picker");

  // ─────────── Quill setup ───────────
  const quill = new Quill("#editor", {
    theme: "snow",
    modules: {
      toolbar: {
        container: "#toolbar",
        handlers: {
          image: pickImage,     // route image button through our uploader
        },
      },
      history: { userOnly: true },
    },
    placeholder: "Start writing. Everything stays on this machine.",
  });
  window.__quill = quill; // expose for chat.js (needs current body for scope)

  // ─────────── Image upload flow ───────────
  function pickImage() {
    imagePicker.value = "";
    imagePicker.click();
  }
  imagePicker.addEventListener("change", async () => {
    const file = imagePicker.files && imagePicker.files[0];
    if (!file) return;
    markSaving();
    try {
      const { url } = await API.uploadImage(file);
      const range = quill.getSelection(true);
      quill.insertEmbed(range.index, "image", url, "user");
      quill.setSelection(range.index + 1);
      markDirty();
    } catch (err) {
      console.error(err);
      alert("Image upload failed: " + err.message);
      markSaved();
    }
  });

  // ─────────── Autosave ───────────
  function markDirty() {
    state.dirty = true;
    saveState.textContent = "Unsaved";
    saveState.className = "save-state dirty";
    scheduleSave();
  }
  function markSaving() {
    saveState.textContent = "Saving…";
    saveState.className = "save-state saving";
  }
  function markSaved() {
    state.dirty = false;
    saveState.textContent = "Saved";
    saveState.className = "save-state";
  }
  function scheduleSave() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(saveNow, 900);
  }
  async function saveNow() {
    if (!state.dirty) return;
    markSaving();
    const payload = collectPayload();
    try {
      if (state.currentId == null) {
        const note = await API.createNote(payload);
        state.currentId = note.id;
      } else {
        await API.updateNote(state.currentId, payload);
      }
      markSaved();
      await refreshHistory();      // titles / order may have changed
      highlightActive();
    } catch (err) {
      console.error(err);
      saveState.textContent = "Save failed";
      saveState.className = "save-state dirty";
    }
  }

  function collectPayload() {
    const html = quill.root.innerHTML;
    const text = quill.getText();     // plain text used for FTS
    return {
      title: (titleInput.value || "Untitled").trim(),
      body_html: html,
      body_text: text,
    };
  }

  // ─────────── Change listeners ───────────
  quill.on("text-change", (_delta, _old, source) => {
    if (source === "user") markDirty();
  });
  titleInput.addEventListener("input", markDirty);

  // Save immediately on blur/tab-away so we don't lose the last keystrokes.
  window.addEventListener("beforeunload", () => {
    if (state.dirty) {
      const payload = collectPayload();
      const url = state.currentId == null
        ? "/api/notes"
        : `/api/notes/${state.currentId}`;
      const method = state.currentId == null ? "POST" : "PUT";
      // sendBeacon can't set method, but we can fire-and-forget:
      try {
        fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          keepalive: true,
        });
      } catch (_) {}
    }
  });

  // ─────────── New / Delete ───────────
  btnNew.addEventListener("click", async () => {
    if (state.dirty) await saveNow();
    loadNoteIntoEditor(null);
    titleInput.focus();
  });

  btnDelete.addEventListener("click", async () => {
    if (state.currentId == null) {
      // just a scratch buffer — clear
      loadNoteIntoEditor(null);
      return;
    }
    if (!confirm("Delete this note? This cannot be undone.")) return;
    try {
      await API.deleteNote(state.currentId);
      state.currentId = null;
      await refreshHistory();
      const next = state.notes[0];
      if (next) selectNote(next.id);
      else loadNoteIntoEditor(null);
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  });

  // ─────────── Stop app ───────────
  // On stop we DO NOT kill Ollama here — the launcher's shutdown flow
  // needs it alive to tag any note flagged with needs_tagging=1. We just
  // flush pending edits and hand off to the launcher with ?shutdown=1.
  btnStopApp.addEventListener("click", async () => {
    if (state.dirty) await saveNow();
    window.location.href = "/?shutdown=1";
  });

  // ─────────── History rail ───────────
  filterInput.addEventListener("input", (e) => {
    state.filter = e.target.value.trim();
    renderHistory();
  });

  async function refreshHistory() {
    try {
      state.notes = (await API.listNotes()) || [];
    } catch (err) {
      console.error(err);
      state.notes = [];
    }
    renderHistory();
  }
  function renderHistory() {
    const q = state.filter.toLowerCase();
    const filtered = q
      ? state.notes.filter((n) => n.title.toLowerCase().includes(q))
      : state.notes;

    noteListEl.innerHTML = "";
    if (filtered.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = q ? "No matching titles." : "No notes yet. Create one →";
      noteListEl.appendChild(li);
      return;
    }
    for (const n of filtered) {
      const li = document.createElement("li");
      li.dataset.id = n.id;
      li.dataset.testid = `list-item-note-${n.id}`;
      li.innerHTML =
        `<span class="n-title">${escapeHtml(n.title || "Untitled")}</span>` +
        `<span class="n-meta">${formatDate(n.updated_at)}</span>`;
      li.addEventListener("click", () => selectNote(n.id));
      noteListEl.appendChild(li);
    }
    highlightActive();
  }
  function highlightActive() {
    for (const li of noteListEl.children) {
      li.classList.toggle(
        "active",
        String(state.currentId) === li.dataset.id
      );
    }
  }

  async function selectNote(id) {
    if (state.dirty) await saveNow();
    try {
      const note = await API.getNote(id);
      loadNoteIntoEditor(note);
    } catch (err) {
      alert("Failed to open note: " + err.message);
    }
  }

  function loadNoteIntoEditor(note) {
    if (note == null) {
      state.currentId = null;
      titleInput.value = "";
      quill.setContents([{ insert: "\n" }], "silent");
    } else {
      state.currentId = note.id;
      titleInput.value = note.title || "";
      // Use dangerouslyPasteHTML because Quill treats setContents as Delta.
      quill.clipboard.dangerouslyPasteHTML(note.body_html || "", "silent");
    }
    markSaved();
    highlightActive();
    // Expose current note id for the chat drawer:
    window.__currentNote = state.currentId;
    window.dispatchEvent(new CustomEvent("note-loaded", { detail: { id: state.currentId } }));
  }

  // ─────────── Utilities ───────────
  function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const today = new Date();
    const sameDay =
      d.getFullYear() === today.getFullYear() &&
      d.getMonth() === today.getMonth() &&
      d.getDate() === today.getDate();
    if (sameDay) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // Expose a helper the chat module uses to append AI replies as new notes.
  window.createNoteFromChat = async function (title, markdown) {
    try {
      const html = DOMPurify.sanitize(marked.parse(markdown));
      const note = await API.createNote({
        title: title || "From chat",
        body_html: html,
        body_text: markdown,
      });
      await refreshHistory();
      selectNote(note.id);
    } catch (err) {
      alert("Could not save as note: " + err.message);
    }
  };

  // ─────────── Init ───────────
  (async function init() {
    await refreshHistory();
    if (state.notes.length > 0) {
      selectNote(state.notes[0].id);
    } else {
      loadNoteIntoEditor(null);
    }
  })();
})();
