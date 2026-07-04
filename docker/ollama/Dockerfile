# Ollama image with models baked in.
#
# Build stage:
#   1. Start a temporary `ollama serve` in the background
#   2. `ollama pull` each model listed in OLLAMA_MODELS
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
#   docker build --build-arg OLLAMA_MODELS="qwen3:4b gemma3:270m llama3.2:3b" ...
ARG OLLAMA_MODELS="qwen3:4b gemma3:270m"

# Ollama needs a running server to accept `pull` commands. Start it, wait for
# the API to respond, pull each model, then shut the server down cleanly.
RUN set -eux; \
    ollama serve & \
    SERVER_PID=$!; \
    # Wait up to 30s for the API to come up
    for i in $(seq 1 30); do \
        if ollama list >/dev/null 2>&1; then break; fi; \
        sleep 1; \
    done; \
    for model in ${OLLAMA_MODELS}; do \
        echo "==> Pulling ${model}"; \
        ollama pull "${model}"; \
    done; \
    echo "==> Installed models:"; \
    ollama list; \
    kill "${SERVER_PID}"; \
    wait "${SERVER_PID}" 2>/dev/null || true

# ----- Stage 2: final image, weights only -----
FROM ollama/ollama:latest

# Copy the model blobs and manifests pulled in stage 1.
COPY --from=puller /root/.ollama /root/.ollama

EXPOSE 11434

# Base image's ENTRYPOINT already runs `ollama serve`, so no CMD override.
