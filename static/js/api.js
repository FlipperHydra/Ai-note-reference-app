// api.js — thin fetch wrapper shared by launcher, editor, and chat.
// Everything is same-origin (FastAPI on 127.0.0.1:8000), so no CORS games.

(function (global) {
  "use strict";

  async function request(method, url, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch (_) {}
      throw new Error(`${res.status} ${detail}`);
    }
    // 204 or empty body → return null
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) return null;
    return res.json();
  }

  const api = {
    // ── Ollama lifecycle ──
    ollamaStart:  () => request("POST", "/api/ollama/start"),
    ollamaStop:   () => request("POST", "/api/ollama/stop"),
    ollamaStatus: () => request("GET",  "/api/ollama/status"),
    listModels:   () => request("GET",  "/api/models"),

    // ── Notes CRUD ──
    listNotes:  (q = "")        => request("GET",  `/api/notes${q ? `?q=${encodeURIComponent(q)}` : ""}`),
    getNote:    (id)            => request("GET",  `/api/notes/${id}`),
    createNote: (data)          => request("POST", "/api/notes", data),
    updateNote: (id, data)      => request("PUT",  `/api/notes/${id}`, data),
    deleteNote: (id)            => request("DELETE", `/api/notes/${id}`),

    // ── Tag-driven recall ──
    //   Backend flow: LLM translates q → tag list, then SQL joins note_tags.
    //   Falls back to FTS5 server-side if the tag lookup returns nothing.
    recall: (q, noteId = null) =>
      request(
        "GET",
        `/api/recall?q=${encodeURIComponent(q)}` +
          (noteId ? `&note_id=${noteId}` : "")
      ),

    // ── List existing tags (used by tag chips in the editor) ──
    listTags: () => request("GET", "/api/tags"),

    // ── Image upload (multipart, not JSON) ──
    uploadImage: async (file) => {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      if (!res.ok) throw new Error(`upload failed: ${res.status}`);
      return res.json(); // { url: "/static/uploads/xyz.png" }
    },

    /**
     * Run the shutdown tagger. Streams SSE events describing per-note
     * progress. Each event is one of:
     *   { total: 4 }                                  — start
     *   { index: 1, id: 12, title: "...", tags: ["fantasy","dragons"] }
     *   { done: true, tagged: 4 }                     — finished
     *   { error: "..." }
     * After onDone the backend has ALREADY killed the Ollama process.
     */
    streamTagRun({ onStart, onNote, onDone, onError }) {
      const ctrl = new AbortController();
      (async () => {
        try {
          const res = await fetch("/api/tag/run", {
            method: "POST",
            signal: ctrl.signal,
          });
          if (!res.ok || !res.body) throw new Error(`tag run failed: ${res.status}`);
          const reader = res.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const raw = buf.slice(0, idx).trim();
              buf = buf.slice(idx + 2);
              if (!raw.startsWith("data:")) continue;
              const payload = raw.slice(5).trim();
              try {
                const j = JSON.parse(payload);
                if (j.error) { onError && onError(new Error(j.error)); return; }
                if (j.done)  { onDone && onDone(j); return; }
                if (typeof j.total === "number" && j.index == null) {
                  onStart && onStart(j);
                } else {
                  onNote && onNote(j);
                }
              } catch (_) { /* partial frame */ }
            }
          }
          onDone && onDone({ done: true });
        } catch (err) {
          if (err.name !== "AbortError") onError && onError(err);
        }
      })();
      return () => ctrl.abort();
    },

    /**
     * Stream a chat from the backend (which proxies Ollama).
     * @param {Object} p
     * @param {string} p.model
     * @param {Array}  p.messages   Ollama-shaped [{role, content}, ...]
     * @param {number|null} p.noteId Attach note context server-side
     * @param {(chunk:string) => void} p.onToken  Called per token
     * @param {() => void} p.onDone
     * @param {(err:Error) => void} p.onError
     * @returns {() => void} abort function
     */
    streamChat({ model, messages, noteId, onToken, onDone, onError }) {
      const ctrl = new AbortController();
      (async () => {
        try {
          const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model, messages, note_id: noteId }),
            signal: ctrl.signal,
          });
          if (!res.ok || !res.body) {
            throw new Error(`chat failed: ${res.status}`);
          }
          const reader = res.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            // Server-Sent-Event framing: each event is "data: {...}\n\n"
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const raw = buf.slice(0, idx).trim();
              buf = buf.slice(idx + 2);
              if (!raw.startsWith("data:")) continue;
              const payload = raw.slice(5).trim();
              if (payload === "[DONE]") { onDone && onDone(); return; }
              try {
                const j = JSON.parse(payload);
                if (j.token) onToken && onToken(j.token);
                if (j.done)  { onDone && onDone(); return; }
                if (j.error) throw new Error(j.error);
              } catch (e) {
                // ignore parse errors on partial frames
              }
            }
          }
          onDone && onDone();
        } catch (err) {
          if (err.name !== "AbortError") onError && onError(err);
        }
      })();
      return () => ctrl.abort();
    },
  };

  global.API = api;
})(window);
