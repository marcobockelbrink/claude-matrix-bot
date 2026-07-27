# ── Build stage ───────────────────────────────────────────────────────────
# Compiles the wheels that have no prebuilt distribution (python-olm for
# matrix-nio[e2e]); the toolchain stays out of the runtime image.
FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libolm-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────
# No compiler, no headers — only what the bot needs at run time:
#  - libolm3: E2E encryption for matrix-nio
#  - curl + ca-certificates: how the agent's Bash tool talks to the HA HTTP API
#  - git: some Agent SDK tools expect it on PATH
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libolm3 \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY bot.py system_prompt.md ./

# The Agent SDK's bypassPermissions mode refuses to run as root — use an
# unprivileged user. It also needs a writable $HOME for its CLI state.
RUN useradd -m -u 1000 bot \
    && mkdir -p /app/store /app/data /app/outbox \
    && chown -R bot:bot /app
USER bot
ENV HOME=/home/bot

# Baked in at build time (CI passes the tag/branch, compose passes git describe).
ARG VERSION=dev
ENV BOT_VERSION=$VERSION
# Whisper models are downloaded on first use — cache them in the persistent
# data volume so they survive container rebuilds.
ENV HF_HOME=/app/data/hf

# Persist the Matrix E2E store (device keys, sync token) across restarts.
VOLUME ["/app/store"]

# Liveness of the Matrix sync loop (only meaningful when WEBHOOK_PORT is set).
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEBHOOK_PORT:-8321}/healthz" || exit 1

CMD ["python", "bot.py"]
