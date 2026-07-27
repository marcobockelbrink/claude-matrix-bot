FROM python:3.14-slim

# System deps:
#  - libolm-dev + gcc: build/runtime for matrix-nio[e2e] (python-olm)
#  - curl + ca-certificates: how the agent's Bash tool talks to the HA HTTP API
#  - git: some Agent SDK tools expect it on PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
        libolm-dev \
        gcc \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py system_prompt.md ./

# The Agent SDK's bypassPermissions mode refuses to run as root — use an
# unprivileged user. It also needs a writable $HOME for its CLI state.
RUN useradd -m -u 1000 bot \
    && mkdir -p /app/store /app/data /app/outbox \
    && chown -R bot:bot /app
USER bot
ENV HOME=/home/bot
# Whisper models are downloaded on first use — cache them in the persistent
# data volume so they survive container rebuilds.
ENV HF_HOME=/app/data/hf

# Persist the Matrix E2E store (device keys, sync token) across restarts.
VOLUME ["/app/store"]

CMD ["python", "bot.py"]
