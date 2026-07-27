# claude-matrix-bot

🇩🇪 [Deutsche Version](README.de.md)

A Matrix chat bot, powered by the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk),
that operates a [Home Assistant](https://www.home-assistant.io/) instance from your phone.
Message it in a private Matrix room and it reads states, edits automations, calls services,
inspects logs, and restarts Home Assistant Core on your behalf — the same kind of work you'd
do from a Claude Code session, reachable wherever your phone has signal.

> ⚠️ **Read this before running it.** This gives a chat bot the keys to your house. Most tool
> calls (shell commands, HA service calls) run **without a per-action approval prompt** — the
> main guard is the sender allowlist: the bot responds only to the Matrix IDs (and Signal
> numbers) you list and ignores everyone else. With `CONFIRM_DESTRUCTIVE=true` (default), destructive commands
> (deletes, HA restart/stop, backup deletion) additionally require a yes/no confirmation in
> the chat. Use a strong, unique password on the bot's Matrix account, keep the room private
> and encrypted, and treat the host running the container as trusted infrastructure. This is
> intentionally powerful; make sure that's what you want.

## Features

- **Chat-ops for Home Assistant** — read states, call services, edit automations, inspect
  logs, restart Core, all through the HA HTTP API.
- **Voice messages** — send a Matrix voice message; the bot transcribes it locally
  (faster-whisper, no cloud STT) and treats it as a prompt.
- **Images & files back** — the agent drops camera snapshots, charts, or config files into
  an outbox that gets uploaded to the room (E2E-encrypted where the room is).
- **Proactive notifications** — HA automations POST to the bot's webhook; the message is
  posted verbatim, or (with `"smart": true`) investigated and phrased by the agent.
- **Daily briefing** — an optional scheduled agent run (PV forecast, weather, waste
  collection, fuel prices, HA errors — see `system_prompt.md`).
- **Persistent memory** — the agent keeps a notes file on a mounted volume that survives
  restarts.
- **Destructive-action confirmation** — dangerous shell commands wait for a **yes/no**
  (or 👍/👎 reaction) from you in the chat.
- **Bilingual** — bot UI strings in German or English (`BOT_LANG`); the agent always mirrors
  the language you write in.
- **Multi-user & optional Signal channel** — allowlist several Matrix users (family), and/or
  enable Signal as a second chat surface via a `signal-cli-rest-api` sidecar.

## How it works

```
Phone (Element / any Matrix client)
        │  Matrix protocol (E2E-encrypted room)
        ▼
  ha-matrix-bot (Docker container, outbound-only)
   ├─ matrix-nio: joins one room, only reacts to the allowlisted user
   └─ Claude Agent SDK: persistent session with Bash / Read / Write / Edit /
      WebFetch / WebSearch tools; talks to HA over its HTTP API using curl
        │  outbound HTTPS
        ▼
  https://your-home-assistant/api/...   (e.g. via a Cloudflare Tunnel)
```

The bot never needs an inbound port or LAN access — it only makes outbound connections (to
matrix.org and to your HA's public URL). It talks to Home Assistant through the **REST/WebSocket
API** over your instance's public URL, so it works from anywhere. (It does *not* use SSH, so
file-level edits to `custom_components/` are out of scope for now — that's a possible v2.)

## Setup

### 1. Create two Matrix accounts

Register two accounts on [matrix.org](https://matrix.org) (or any homeserver):

- **Your** account — the one you'll chat from on your phone (via [Element](https://element.io/)).
- **The bot's** account — a separate account the container logs in as.

matrix.org signup has a captcha, so this step is manual.

### 2. Create a private, encrypted room

From your account in Element: create a new room, enable **encryption** in its settings, keep it
**private**, and **invite the bot's account**. The bot auto-accepts an invite only from your
allowlisted user ID, so you can just invite it and it'll join on next sync.

### 3. Configure

```bash
cp .env.example .env
```

Fill in `.env`:

- `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, uses your Claude subscription) **or**
  `ANTHROPIC_API_KEY` (pay-per-use, from the [Claude Console](https://platform.claude.com/)).
- `HA_BASE_URL` / `HA_TOKEN` — your HA public URL and a long-lived access token
  (Home Assistant → your profile → Security → Long-lived access tokens).
- `MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_PASSWORD` — the **bot's** account.
- `MATRIX_ALLOWED_USERS` — comma-separated Matrix IDs (e.g. `@you:matrix.org`). These are the
  only senders the bot will ever respond to or accept invites from.

Optional features (see comments in `.env.example`): `BOT_LANG` (de/en), `WEBHOOK_TOKEN`
(enables the notification webhook), `BRIEFING_TIME` (daily briefing, e.g. `07:00`),
`WHISPER_MODEL` (voice transcription, `off` to disable), `CONFIRM_DESTRUCTIVE`.

`.env` is gitignored — it never gets committed.

### 4. Run

```bash
docker compose up -d --build
docker compose logs -f        # watch it log in and start syncing
```

Then message the room from your phone. First reply may take a few seconds while the agent
gathers context.

The `store/` directory (created next to the compose file, mounted into the container) holds the
bot's Matrix device identity and encryption keys — keep it around so the bot doesn't re-key on
every restart.

## Optional: Signal as a second channel

If (part of) the family prefers Signal over Element, the bot can serve both at once. The
Signal side runs as a sidecar container ([signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api))
with its own dedicated phone number (a prepaid SIM you use once for registration is fine).

1. **Start the sidecar** (it's behind a compose profile, so it's off by default):

   ```bash
   docker compose --profile signal up -d
   ```

2. **Register the bot's number** (one-time). The API is exposed on `127.0.0.1:8380`:

   ```bash
   # Request SMS verification (if it fails with a captcha error, solve one at
   # https://signalcaptchas.org/registration/generate.html and pass it along):
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX \
     -H 'Content-Type: application/json' -d '{"captcha": "<token>"}'

   # Confirm with the SMS code:
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX/verify/<code>
   ```

3. **Configure the bot** in `.env` and restart it:

   ```bash
   SIGNAL_API_URL=http://signal:8080
   SIGNAL_NUMBER=+49XXXXXXXXX
   SIGNAL_ALLOWED_NUMBERS=+49...,+49...   # family members' numbers
   SIGNAL_NOTIFY=+49...                   # or group.<id>, for briefing/webhook pushes
   ```

Family members then simply message the bot's number on Signal (or you add the bot to a
Signal group — group ids via `GET /v1/groups/<SIGNAL_NUMBER>`). Voice messages, images from
the agent, and destructive-command confirmations all work on Signal too. Registration state
lives in `signal-data/` (gitignored) — keep it, or you'll have to re-register.

## Hooking up Home Assistant notifications

The webhook listens on port `8321` (mapped in `docker-compose.yml`). In Home Assistant,
define a `rest_command` pointing at the host running the bot:

```yaml
# configuration.yaml
rest_command:
  matrix_bot_notify:
    url: "http://<bot-host-ip>:8321/notify"
    method: post
    headers:
      X-Token: !secret matrix_bot_webhook_token
    content_type: application/json
    payload: '{"message": {{ message | tojson }}, "smart": {{ smart | default(false) | tojson }}}'
```

Then call it from any automation:

```yaml
actions:
  - action: rest_command.matrix_bot_notify
    data:
      message: "Battery below 30% — pool pump switched off."
      smart: true   # let the agent investigate and phrase the push message
```

With `smart: false` (default) the message is posted verbatim, prefixed with 🔔.

## Where to run it

Any host with outbound internet works (inbound is only needed for the optional
notification webhook):

- **A small always-on box** (Raspberry Pi, mini-PC, NAS with Docker/Container Manager, a cheap VPS).
- **This machine**, for testing (`docker compose up`) — but it's only reachable while that
  machine is on.
- **Kubernetes** — see below.

### Prebuilt image

Every push to `main` builds a multi-arch image (amd64 + arm64) via GitHub Actions:

```
ghcr.io/marcobockelbrink/claude-matrix-bot:latest
```

Tags (`vX.Y.Z`) get matching image tags. To use it with compose instead of building
locally, replace `build: .` with `image: ghcr.io/marcobockelbrink/claude-matrix-bot:latest`.

### Kubernetes (plain manifests)

```bash
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml   # fill in, don't commit
kubectl apply -f deploy/k8s/secret.yaml -f deploy/k8s/bot.yaml
# optional Signal sidecar:
kubectl apply -f deploy/k8s/signal.yaml
```

One replica only (the bot holds a Matrix device identity and a persistent agent session);
two PVCs persist the Matrix E2E store and the agent's memory/Whisper cache.

### Kubernetes (Helm)

```bash
helm install ha-matrix-bot deploy/helm/ha-matrix-bot \
  --namespace ha-matrix-bot --create-namespace \
  --values my-values.yaml
```

Minimal `my-values.yaml`:

```yaml
secrets:
  values:
    CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-..."
    HA_BASE_URL: "https://your-home-assistant.example.com"
    HA_TOKEN: "..."
    MATRIX_HOMESERVER: "https://matrix.org"
    MATRIX_USER: "@yourbot:matrix.org"
    MATRIX_PASSWORD: "..."
    MATRIX_ALLOWED_USERS: "@you:matrix.org"
    WEBHOOK_TOKEN: "long-random-string"
config:
  briefingTime: "07:00"
signal:
  enabled: false   # flip on + add SIGNAL_* secrets for the Signal channel
```

Alternatively point `secrets.existingSecret` at a Secret you manage (e.g. via
sealed-secrets / SOPS). See `deploy/helm/ha-matrix-bot/values.yaml` for all options.

## Notes / limitations

- **Allowlist-only.** The bot talks to the Matrix IDs / Signal numbers you list, nobody else.
  Webhook/briefing messages go to `NOTIFY_ROOM` if set, otherwise to the room someone
  allowlisted last wrote in (plus `SIGNAL_NOTIFY`, if configured).
- **Fresh conversation on restart.** The chat transcript is held in memory; the agent's
  `memory.md` notes file (in the `data/` volume) is what carries over.
- **REST/WS only, no SSH.** Automations, service calls, config-entry flows, and restarts are
  all covered; editing files inside `custom_components/` is not.
- **Serial.** One agent run at a time — fine for a single-user home-automation chat.
- **Voice transcription is CPU-bound.** The first voice message downloads the Whisper model
  (cached in `data/`); transcription of a short message takes a few seconds on a modern CPU.

## Security

- Commits and release tags are signed.
- CI runs [CodeQL](https://github.com/marcobockelbrink/claude-matrix-bot/security/code-scanning)
  and Trivy (filesystem, IaC, and container-image scans) on every push; Dependabot watches
  pip, Docker, and GitHub Actions dependencies. Secret scanning with push protection is on.
- Found a vulnerability? Please use
  [private vulnerability reporting](https://github.com/marcobockelbrink/claude-matrix-bot/security/advisories/new) —
  see [SECURITY.md](SECURITY.md).

## Development

`bot.py` is a single-file bridge — the matrix-nio event loop on one side, a persistent
`ClaudeSDKClient` on the other. `system_prompt.md` is the Home Assistant runbook fed to the
agent.

🤖 Built with [Claude Code](https://claude.com/claude-code)
