"""Ollama subprocess controller + thin HTTP client.

Two modes, switched by AI_NOTES_OLLAMA_MANAGED:

* Managed (default): the app calls `ollama serve` itself and kills it on
  request. This matches the launcher UX — "Start Application" spawns it,
  "Stop app" tears it down.
* Unmanaged: assumes an external Ollama is reachable at OLLAMA_HOST
  (docker-compose, remote server). Start/stop become no-ops.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import socket
import subprocess
import sys
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from .settings import (
    OLLAMA_BIN,
    OLLAMA_HOST,
    OLLAMA_MANAGED,
    OLLAMA_START_TIMEOUT_S,
)

_process: subprocess.Popen | None = None


# ─────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────
def is_binary_available() -> bool:
    return shutil.which(OLLAMA_BIN) is not None


def _host_port() -> tuple[str, int]:
    u = urlparse(OLLAMA_HOST)
    return (u.hostname or "127.0.0.1"), (u.port or 11434)


def _port_open() -> bool:
    host, port = _host_port()
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


async def start() -> dict:
    """Spawn ollama serve and wait until the port is accepting.

    In unmanaged mode (docker-compose, remote server) we don't spawn anything
    but we DO wait for the external Ollama to become reachable — otherwise the
    launcher races the container-startup order and thinks nothing is running.
    """
    global _process
    if not OLLAMA_MANAGED:
        ok = await _wait_ready(OLLAMA_START_TIMEOUT_S)
        if not ok:
            raise RuntimeError(
                f"Ollama unreachable at {OLLAMA_HOST}. Is the ollama service running?"
            )
        return {"ok": True, "managed": False, "host": OLLAMA_HOST}
    if _process and _process.poll() is None:
        return {"ok": True, "already_running": True}
    if not is_binary_available():
        raise RuntimeError(
            f"'{OLLAMA_BIN}' not found on PATH. Install from https://ollama.com/download"
        )
    # Route ollama's logs to our stderr so users can debug in one place.
    _process = subprocess.Popen(
        [OLLAMA_BIN, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
        # Put ollama in its own process group so we can kill children too.
        start_new_session=True,
    )
    ok = await _wait_ready(OLLAMA_START_TIMEOUT_S)
    if not ok:
        stop()
        raise RuntimeError("Ollama started but never opened its HTTP port.")
    return {"ok": True, "pid": _process.pid}


async def _wait_ready(timeout_s: int) -> bool:
    for _ in range(timeout_s * 4):
        if _port_open():
            return True
        await asyncio.sleep(0.25)
    return False


def stop() -> dict:
    """Kill the child process group. Idempotent.

    Killing the ollama process makes the OS reclaim all its RAM/VRAM
    immediately, so this is sufficient in managed mode. In unmanaged mode
    (docker-compose, remote server) we don't own the process — callers
    should invoke unload_models_sync() first to evict resident model
    weights, otherwise Ollama holds them for its keep_alive window (5m
    default) after we go away.
    """
    global _process
    if not OLLAMA_MANAGED:
        return {"ok": True, "managed": False}
    if not _process:
        return {"ok": True, "already_stopped": True}
    try:
        # Kill the whole group in case ollama spawned model workers.
        import os
        os.killpg(_process.pid, signal.SIGTERM)
        try:
            _process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(_process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        _process = None
    return {"ok": True}


# ────────────────────────────────────────────────────────────────────
# Memory release — explicit model eviction
# ────────────────────────────────────────────────────────────────────
async def loaded_models() -> list[str]:
    """Return names of models currently resident in Ollama's memory.

    Uses /api/ps (Ollama's "which models are loaded right now" endpoint).
    Returns [] if Ollama is unreachable.
    """
    if not _port_open():
        return []
    try:
        async with httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=3) as x:
            r = await x.get("/api/ps")
            r.raise_for_status()
            return [m.get("name") for m in r.json().get("models", []) if m.get("name")]
    except httpx.HTTPError:
        return []


async def unload_model(model: str) -> bool:
    """Force Ollama to evict `model` from RAM/VRAM immediately.

    Ollama's convention: any /api/generate or /api/chat request with
    keep_alive=0 unloads the model as soon as the request completes.
    We send an empty prompt so no actual generation happens — it's a
    pure eviction signal.
    """
    if not _port_open():
        return False
    try:
        async with httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=10) as x:
            r = await x.post(
                "/api/generate",
                json={"model": model, "prompt": "", "keep_alive": 0, "stream": False},
            )
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def unload_all_models() -> dict:
    """Evict every currently loaded model. Idempotent, safe to call anytime."""
    names = await loaded_models()
    if not names:
        return {"ok": True, "unloaded": []}
    results = []
    for name in names:
        ok = await unload_model(name)
        results.append({"model": name, "unloaded": ok})
    return {"ok": all(r["unloaded"] for r in results), "unloaded": results}


def unload_all_models_sync(timeout_s: float = 8.0) -> dict:
    """Blocking variant — safe to call from signal handlers and atexit hooks
    where no event loop is running (or the running loop is shutting down).

    We use a fresh synchronous httpx client so we don't depend on asyncio.
    """
    if not _port_open():
        return {"ok": True, "unloaded": [], "reason": "ollama_unreachable"}
    try:
        with httpx.Client(base_url=OLLAMA_HOST, timeout=timeout_s) as x:
            r = x.get("/api/ps")
            if r.status_code != 200:
                return {"ok": False, "unloaded": [], "reason": "ps_failed"}
            names = [m.get("name") for m in r.json().get("models", []) if m.get("name")]
            results = []
            for name in names:
                try:
                    rr = x.post(
                        "/api/generate",
                        json={"model": name, "prompt": "", "keep_alive": 0, "stream": False},
                    )
                    results.append({"model": name, "unloaded": rr.status_code == 200})
                except httpx.HTTPError:
                    results.append({"model": name, "unloaded": False})
            return {"ok": all(r["unloaded"] for r in results), "unloaded": results}
    except httpx.HTTPError as e:
        return {"ok": False, "unloaded": [], "reason": f"http_error: {e}"}


def status() -> dict:
    return {
        "ready": _port_open(),
        "managed": OLLAMA_MANAGED,
        "host": OLLAMA_HOST,
        "pid": _process.pid if _process and _process.poll() is None else None,
    }


# ─────────────────────────────────────────────────────────────────────
# HTTP client
# ─────────────────────────────────────────────────────────────────────
async def list_models(retries: int = 3, delay: float = 0.5) -> list[dict]:
    """Fetch installed models from Ollama's /api/tags.

    Retries a few times because there's a brief window right after the port
    opens where the tags endpoint returns an empty list — particularly in
    Docker on first boot. Without retries, the launcher shows a spurious
    "No models installed" state even when the image has weights baked in.
    """
    if not _port_open():
        return []
    async with httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=5) as x:
        for attempt in range(retries):
            try:
                r = await x.get("/api/tags")
                r.raise_for_status()
                models = r.json().get("models", []) or []
                if models or attempt == retries - 1:
                    return models
            except httpx.HTTPError:
                if attempt == retries - 1:
                    return []
            await asyncio.sleep(delay)
    return []


async def chat_once(model: str, messages: list[dict], *, timeout: float = 120.0) -> str:
    """Non-streaming chat — used by the tagger (small deterministic reply)."""
    async with httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=timeout) as x:
        r = await x.post(
            "/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.1},
            },
        )
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")


async def chat_stream(
    model: str, messages: list[dict], *, timeout: float = 600.0
) -> AsyncIterator[str]:
    """Yield content chunks as Ollama streams them."""
    async with httpx.AsyncClient(base_url=OLLAMA_HOST, timeout=timeout) as x:
        async with x.stream(
            "POST",
            "/api/chat",
            json={"model": model, "messages": messages, "stream": True},
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = j.get("message") or {}
                if content := msg.get("content"):
                    yield content
                if j.get("done"):
                    return


async def pick_tagger_model(preferences: list[str]) -> str | None:
    models = await list_models()
    available = {m.get("name") for m in models}
    for name in preferences:
        if name in available:
            return name
    # Fallback: any installed model at all.
    return next(iter(available), None)
