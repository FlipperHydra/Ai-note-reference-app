# Deployment Plan — Shipping AI Notes as a Real App

This repo is a working reference. To get from "runs on my laptop with `uvicorn`" to "an app I (or anyone) can install and use," there are four realistic paths. Each has trade-offs — I've laid them out honestly rather than picking a winner up front.

The core deployment problem is the same in every case:

1. **Two processes** — the FastAPI server and Ollama. Somebody has to start and stop both.
2. **One big dependency** — the model weights (2–8 GB per model). They can't ride inside the app bundle.
3. **Persistent state** — `notes.db` and `static/uploads/` must survive updates and reinstalls.
4. **Offline requirement** — after the first-run model pull, nothing may phone home.

---

## Path A — Native desktop wrapper (PyInstaller / py2app)

**Best for:** Windows and macOS users who want a single `.exe` / `.app` icon.

### How it works
- PyInstaller (Win/Linux) or py2app (macOS) freezes Python + the FastAPI app into one binary.
- On launch, the binary starts uvicorn on a random localhost port, opens the default browser to it, and shells out to Ollama.
- First run: detect Ollama; if missing, prompt the user to install it (link to ollama.com) or ship the Ollama binary alongside (macOS/Linux only — Windows Ollama is a full installer).

### Steps
1. Add a `launcher.py` at repo root that:
   - Picks a free port.
   - Starts uvicorn in a thread.
   - Uses `webbrowser.open()` to hit the URL.
   - Waits on the uvicorn thread; exits cleanly on window close (harder — see below).
2. `pip install pyinstaller && pyinstaller --onefile --add-data "templates:templates" --add-data "static:static" --add-data "prompts:prompts" launcher.py`
3. Sign the binary (macOS: `codesign` + notarize; Windows: an EV cert or accept SmartScreen warnings).
4. Ship as `.dmg` (macOS) or NSIS installer (Windows).

### Trade-offs
- ✅ True double-click experience. No terminal.
- ✅ Single artifact per OS.
- ⚠️ **The "close the app" problem.** A browser tab isn't a window we own. Options:
  - Add a system-tray icon (`pystray`) with "Quit" that hits `/api/tag/run` then shuts down.
  - Poll `/health` from the tray; if the last tab was closed >30s ago, run tagger + exit.
  - Accept a manual "Stop" click in the UI as the shutdown trigger (what the app does today).
- ⚠️ Binary size: 30–60 MB after freezing. Not counting Ollama or models.
- ⚠️ Auto-update needs a separate mechanism (Sparkle on macOS, Squirrel on Windows) or a "check GitHub releases" nudge inside the app.
- ⚠️ macOS notarization requires a paid Apple Developer account ($99/yr).

**When to pick this:** the app is for humans who don't want to think about servers or Docker.

---

## Path B — Tauri or Electron wrapper

**Best for:** the same audience as Path A, but with a proper native window instead of a browser tab.

### How it works
- Tauri (Rust + system webview) or Electron (Chromium) hosts the FastAPI server as a sidecar process and renders the UI in a native window.
- Same FastAPI code, same static assets — the wrapper just replaces the browser.

### Tauri sidecar sketch
```rust
// src-tauri/tauri.conf.json
"tauri": {
  "bundle": {
    "externalBin": ["../dist/ai-notes-server"]  // PyInstaller output
  }
}
// on app startup, spawn the sidecar, then load http://127.0.0.1:<port>/
```

### Trade-offs
- ✅ Real native window, real close button, tray icon support built-in.
- ✅ Tauri bundles are small (~10 MB) — Chromium isn't shipped.
- ✅ Electron is easier if you know JS but adds ~100 MB.
- ⚠️ You still need PyInstaller for the Python side.
- ⚠️ Two build toolchains to maintain (Rust/Node + Python).
- ⚠️ Auto-updates come free with Tauri's updater or electron-updater — a real win vs Path A.

**When to pick this:** you want to distribute a polished product and don't mind a bigger build pipeline.

---

## Path C — Docker Compose for self-hosters

**Best for:** homelab users, teams, or anyone who runs a NAS / mini-PC.

This is what the `docker` branch of this repo ships.

### How it works
- Two services in `docker-compose.yml`:
  - `app` — this FastAPI project. `AI_NOTES_OLLAMA_MANAGED=0`, points at the `ollama` service.
  - `ollama` — official `ollama/ollama` image with a volume for model weights.
- Volumes: `./data/notes.db` and `./data/uploads` for user data; `ollama-models` for weights.
- Port 8765 published on the host.

### First-run
```bash
git clone https://github.com/FlipperHydra/Ai-note-reference-app.git
cd Ai-note-reference-app
git checkout docker
docker compose up -d
docker compose exec ollama ollama pull llama3.2:3b
```
Open http://your-server:8765.

### Trade-offs
- ✅ Zero packaging work — `docker compose up` and done.
- ✅ Runs on Linux, Windows (WSL2), macOS.
- ✅ Update path is just `git pull && docker compose up -d --build`.
- ✅ Easy to put behind Caddy/Traefik with HTTPS + basic auth if you expose it beyond localhost.
- ⚠️ Ollama in Docker on macOS runs on CPU only — the GPU passthrough only works on Linux with NVIDIA Container Toolkit.
- ⚠️ Requires the user to be comfortable with Compose. Not a "click and go" install.
- ⚠️ GPU support requires extra flags (`deploy.resources.reservations.devices` for NVIDIA).

**When to pick this:** you have a home server or want to share the app with a small team.

---

## Path D — Systemd service on a single Linux box

**Best for:** the "I just want it running on my Debian VPS / Raspberry Pi 5" case.

### How it works
Two unit files:

**`/etc/systemd/system/ai-notes-ollama.service`**
```ini
[Unit]
Description=Ollama for AI Notes
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ollama serve
User=ai-notes
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/ai-notes.service`**
```ini
[Unit]
Description=AI Notes
After=ai-notes-ollama.service
Requires=ai-notes-ollama.service

[Service]
Type=simple
WorkingDirectory=/opt/ai-notes
Environment=AI_NOTES_OLLAMA_MANAGED=0
Environment=AI_NOTES_HOST=127.0.0.1
Environment=AI_NOTES_PORT=8765
ExecStart=/opt/ai-notes/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
User=ai-notes
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now ai-notes` and reverse-proxy with Caddy for HTTPS.

### Trade-offs
- ✅ Zero container overhead. Ideal for low-RAM boxes (Pi 5, cheap VPS).
- ✅ `journalctl -u ai-notes -f` is a nice debugging experience.
- ✅ Ollama stays warm — no cold-start when the user opens the app.
- ⚠️ Manual install steps (git clone, venv, systemd files). Fine for one machine, tedious for many.
- ⚠️ You lose the app's "start/stop on demand" idea — Ollama runs 24/7 and holds ~2–4 GB of RAM.

**When to pick this:** you have exactly one machine and want the least ceremony.

---

## Cross-cutting concerns

### Model management
No path can ship model weights inside the artifact — they're too big and licensing varies. In every case:
1. On first launch, list installed models via `/api/models`.
2. If none, open a "pick a model" screen with recommendations (`llama3.2:3b` for speed, `qwen2.5:7b` for chat quality, `llama3.1:8b` as middle ground).
3. Call `ollama pull` from the UI with progress streaming. **This is the only online step, ever.**

### Persistence and backups
- `notes.db` is a single SQLite file — trivial to back up. Add an `Export all notes` button that zips `notes.db` + `static/uploads/`.
- Docker: bind-mount `./data/`; back it up like any other host directory.
- Native: put the DB in the OS-appropriate app-data dir (`~/Library/Application Support/AI Notes/` on macOS, `%APPDATA%\AI Notes\` on Windows) — set `AI_NOTES_DB` accordingly at launch.

### Security
The app binds to `127.0.0.1` by default and has **no auth**. It assumes single-user, local-only. If you expose it (Path C or D):
- Put a reverse proxy in front (Caddy is easiest).
- Add HTTP basic auth at the proxy layer.
- Or add a proper session-cookie login in FastAPI — worth doing before any multi-user scenario. Not shipped in v1.

### Telemetry
There is none, and there should never be any. Verify with `tcpdump` or Little Snitch after packaging — the only outbound traffic should be `ollama pull` on first-run and (optionally) an update check against GitHub releases.

### Update strategy
| Path | Mechanism |
|------|-----------|
| A (PyInstaller) | Sparkle (macOS) / Squirrel (Windows), or "check GitHub Releases" polling. |
| B (Tauri/Electron) | Built-in updater — recommended. |
| C (Docker) | `git pull && docker compose up -d --build`. |
| D (systemd) | `git pull && systemctl restart ai-notes`. Consider a `Makefile` target. |

---

## Recommended sequence

If I were rolling this out for real:

1. **Ship Path C first.** The `docker` branch is done today. It's the fastest path to "actually usable" and validates the architecture with real users.
2. **Add Path D docs** — a one-page install guide for the systemd version. Costs almost nothing.
3. **Then Path B (Tauri).** Once the UX is proven and the API is stable, wrap it in Tauri for a real product experience with an updater.
4. **Skip Path A** unless there's specific demand for a no-Rust build pipeline. Tauri does the same job better.

Path A is a fine fallback if you don't want to touch Rust — but the "close the app" problem with a browser-tab UI is genuinely annoying, and Tauri solves it for free.
