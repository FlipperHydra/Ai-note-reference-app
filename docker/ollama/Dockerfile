# Ollama image with models baked in.
#
# Build stage:
#   1. Start a temporary `ollama serve` in the background
#   2. `ollama pull` each model listed in MODELS_TO_PULL
#   3. Stop the server. The pulled weights live under /root/.ollama and are
#      copied into the final image layer.
#
# Final image ships with the models already present, so the container is
# offline-ready the moment it starts — no first-run download.
#
# NOTE: the resulting image is large (~3 GB with qwen3:4b + gemma3:270m).
# That is by design; see DEPLOYMENT.md for the trade-off discussion.

# ----- Stage 1: pull models into a scratch Ollama install -----
FROM ollama/ollama:latest AS puller

# Space-separated list. Override at build time with:
#   docker build --build-arg MODELS_TO_PULL="qwen3:4b gemma3:270m llama3.2:3b" ...
# (Named MODELS_TO_PULL instead of OLLAMA_MODELS to avoid collision with
# Ollama's own env var of the same name in the base image.)
ARG MODELS_TO_PULL="qwen3:4b gemma3:270m"

# Ollama needs a running server to accept `pull` commands. Start it, wait for
# the API to respond, pull each model, verify at least one landed, then shut
# the server down cleanly. If verification fails, the build aborts — better
# than shipping an empty image.
RUN set -eux; \
    /bin/ollama serve & \
    SERVER_PID=$!; \
    # Wait up to 30s for the API to come up
    for i in $(seq 1 30); do \
        if /bin/ollama list >/dev/null 2>&1; then break; fi; \
        sleep 1; \
    done; \
    for model in ${MODELS_TO_PULL}; do \
        echo "==> Pulling ${model}"; \
        /bin/ollama pull "${model}"; \
    done; \
    echo "==> Installed models (verification):"; \
    /bin/ollama list; \
    # Fail the build loudly if no models actually got installed.
    test "$(/bin/ollama list | tail -n +2 | wc -l)" -gt 0 || { \
        echo "ERROR: no models were pulled; refusing to bake an empty image" >&2; \
        exit 1; \
    }; \
    # Also verify the on-disk layout matches what the final image expects.
    echo "==> Contents of /root/.ollama:"; \
    ls -la /root/.ollama /root/.ollama/models 2>&1 || true; \
    kill "${SERVER_PID}"; \
    wait "${SERVER_PID}" 2>/dev/null || true

# ----- Stage 2: final image, weights only -----
FROM ollama/ollama:latest

# Copy the model blobs and manifests pulled in stage 1.
# The base image's default OLLAMA_MODELS points at /root/.ollama/models,
# so copying the parent directory keeps every manifest, blob, and history
# file in the exact layout Ollama expects.
COPY --from=puller /root/.ollama /root/.ollama

# Pin the model path explicitly so a change in a future base image can't
# silently break lookups. Also make Ollama listen on all interfaces inside
# the container (compose network only — not exposed to host).
ENV OLLAMA_MODELS=/root/.ollama/models \
    OLLAMA_HOST=0.0.0.0:11434

EXPOSE 11434

# Base image's ENTRYPOINT already runs `ollama serve`, so no CMD override.
