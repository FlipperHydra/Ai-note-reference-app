// launcher.js — controls the start/stop lifecycle of Ollama and gates
// entry to the notes page until the local LLM server is healthy.

(function () {
  "use strict";

  const statusCard = document.getElementById("status-card");
  const dot        = document.getElementById("status-dot");
  const label      = document.getElementById("status-label");
  const modelRow   = document.getElementById("model-row");
  const modelSel   = document.getElementById("model-select");
  const btnStart   = document.getElementById("btn-start");
  const btnStop    = document.getElementById("btn-stop");
  const btnOpen    = document.getElementById("btn-open");
  const hint       = document.getElementById("hint");

  const tagCard    = document.getElementById("tagging-card");
  const tagBar     = document.getElementById("tag-progress");
  const tagCount   = document.getElementById("tag-count");
  const tagCurrent = document.getElementById("tag-current");
  const tagStream  = document.getElementById("tag-stream");

  const STATES = {
    offline:  { text: "Ollama is offline",           start: true,  stop: false, open: false },
    starting: { text: "Starting Ollama…",            start: false, stop: true,  open: false },
    ready:    { text: "Ollama is ready",             start: false, stop: true,  open: true  },
    error:    { text: "Something went wrong",        start: true,  stop: false, open: false },
  };

  function setState(name, hintText) {
    const s = STATES[name] || STATES.offline;
    dot.dataset.state = name;
    label.textContent = s.text;
    btnStart.hidden = !s.start;
    btnStop.hidden  = !s.stop;
    btnOpen.hidden  = !s.open;
    modelRow.hidden = name !== "ready";
    if (hintText) hint.textContent = hintText;
  }

  async function refreshModels() {
    try {
      const { models } = await API.listModels();
      modelSel.innerHTML = "";
      if (!models || models.length === 0) {
        modelSel.innerHTML =
          '<option value="">No models installed — run `ollama pull llama3.2`</option>';
        modelSel.disabled = true;
        return;
      }
      modelSel.disabled = false;
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.name;
        modelSel.appendChild(opt);
      }
      // Remember last pick across launches (sessionStorage — sandbox-safe on
      // a real desktop; falls back gracefully if unavailable)
      try {
        const last = sessionStorage.getItem("ai-notes:model");
        if (last && [...modelSel.options].some((o) => o.value === last)) {
          modelSel.value = last;
        }
      } catch (_) {}
    } catch (err) {
      console.warn("model list failed:", err);
    }
  }

  modelSel.addEventListener("change", () => {
    try { sessionStorage.setItem("ai-notes:model", modelSel.value); } catch (_) {}
  });

  async function pollUntilReady(timeoutMs = 30000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      try {
        const s = await API.ollamaStatus();
        if (s && s.ready) return true;
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 700));
    }
    return false;
  }

  btnStart.addEventListener("click", async () => {
    setState("starting", "Spawning ollama serve…");
    try {
      await API.ollamaStart();
      const ok = await pollUntilReady();
      if (!ok) throw new Error("timed out waiting for Ollama");
      await refreshModels();
      setState("ready", "Pick a model and open Notes.");
    } catch (err) {
      console.error(err);
      setState("error", err.message || "Start failed. Is Ollama installed?");
    }
  });

  btnStop.addEventListener("click", () => runShutdownTagging());

  // ------------------------------------------------------------
  //  Shutdown tagging flow
  //    1. Show tagging-card, hide status-card.
  //    2. Open /api/tag/run SSE stream.
  //    3. Update progress bar + stream list per event.
  //    4. When the backend signals done, it has already killed Ollama.
  //    5. Show a short summary, then restore the launcher state.
  //
  //  Entry point is also exposed as `window.runShutdownTagging()` so
  //  the notes-page Stop button can trigger it after navigating home.
  // ------------------------------------------------------------
  function runShutdownTagging() {
    statusCard.hidden = true;
    tagCard.hidden = false;
    tagBar.style.width = "0%";
    tagCount.textContent = "0 / 0";
    tagCurrent.textContent = "";
    tagStream.innerHTML = "";

    let total = 0;
    let done = 0;

    API.streamTagRun({
      onStart({ total: t }) {
        total = t || 0;
        tagCount.textContent = `0 / ${total}`;
        if (total === 0) {
          tagCurrent.textContent = "No new notes to tag.";
        }
      },
      onNote({ index, title, tags }) {
        done = index;
        const pct = total ? Math.round((done / total) * 100) : 100;
        tagBar.style.width = pct + "%";
        tagCount.textContent = `${done} / ${total}`;
        tagCurrent.textContent = title || "Untitled";

        const li = document.createElement("li");
        const t = document.createElement("div");
        t.className = "tl-title";
        t.textContent = title || "Untitled";
        const chips = document.createElement("div");
        chips.className = "tl-tags";
        for (const tag of tags || []) {
          const c = document.createElement("span");
          c.className = "tag-chip";
          c.textContent = tag;
          chips.appendChild(c);
        }
        li.appendChild(t);
        li.appendChild(chips);
        tagStream.prepend(li);   // newest on top
      },
      async onDone({ tagged } = {}) {
        tagBar.style.width = "100%";
        tagCurrent.textContent = tagged
          ? `Tagged ${tagged} note${tagged === 1 ? "" : "s"}. Ollama stopped.`
          : "Ollama stopped. Idle CPU is near zero.";
        // Restore the launcher card after a beat so the user can restart.
        setTimeout(() => {
          tagCard.hidden = true;
          statusCard.hidden = false;
          setState("offline");
        }, 1800);
      },
      onError(err) {
        console.error(err);
        tagCurrent.textContent = "Tagging failed: " + err.message;
        // Best-effort stop even if tagging blew up.
        API.ollamaStop().catch(() => {});
        setTimeout(() => {
          tagCard.hidden = true;
          statusCard.hidden = false;
          setState("error", "Ollama stopped. Tagging errored — see console.");
        }, 2500);
      },
    });
  }
  window.runShutdownTagging = runShutdownTagging;

  // If the notes page navigated us here with ?shutdown=1, kick off tagging.
  if (new URLSearchParams(location.search).get("shutdown") === "1") {
    // Clean the URL so refresh doesn't re-trigger.
    history.replaceState({}, "", "/");
    runShutdownTagging();
  }

  btnOpen.addEventListener("click", () => {
    if (modelSel.value) {
      try { sessionStorage.setItem("ai-notes:model", modelSel.value); } catch (_) {}
    }
    window.location.href = "/notes";
  });

  // On load, check if Ollama is already running (user may have restarted the tab).
  // In unmanaged mode (docker-compose), Ollama is a sibling container that's
  // always meant to be up — so we poll until it's ready instead of waiting for
  // a user click on "Start Application".
  (async function bootstrap() {
    setState("offline");
    try {
      const s = await API.ollamaStatus();
      if (s && s.ready) {
        await refreshModels();
        setState("ready", "Ollama was already running.");
        return;
      }
      if (s && s.managed === false) {
        // Docker / external Ollama: wait for it to come up automatically.
        setState("starting", "Waiting for the Ollama container…");
        const ok = await pollUntilReady(60000);
        if (ok) {
          await refreshModels();
          setState("ready", "Connected to Ollama.");
        } else {
          setState("error", "Ollama container never became ready.");
        }
      }
    } catch (_) {
      /* backend not up yet — leave in offline state */
    }
  })();
})();
