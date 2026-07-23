# claude-matrix-bot

A Matrix chat bot, powered by the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk),
that operates a [Home Assistant](https://www.home-assistant.io/) instance from your phone.
Message it in a private Matrix room and it reads states, edits automations, calls services,
inspects logs, and restarts Home Assistant Core on your behalf — the same kind of work you'd
do from a Claude Code session, reachable wherever your phone has signal.

> ⚠️ **Read this before running it.** This gives a chat bot the keys to your house. It runs
> with `bypassPermissions`, so it executes tool calls (shell commands, HA service calls,
> restarts) **without a per-action approval prompt**. The only thing standing between "anyone"
> and "restart my heating" is the Matrix sender allowlist — the bot responds to exactly one
> Matrix user ID and ignores everyone else. Use a strong, unique password on the bot's Matrix
> account, keep the room private and encrypted, and treat the host running the container as
> trusted infrastructure. This is intentionally powerful; make sure that's what you want.

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

- `ANTHROPIC_API_KEY` — from the [Claude Console](https://platform.claude.com/).
- `HA_BASE_URL` / `HA_TOKEN` — your HA public URL and a long-lived access token
  (Home Assistant → your profile → Security → Long-lived access tokens).
- `MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_PASSWORD` — the **bot's** account.
- `MATRIX_ALLOWED_USER` — **your** Matrix ID (e.g. `@you:matrix.org`). This is the only sender
  the bot will ever respond to or accept invites from.

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

## Where to run it

Any host with outbound internet works — no inbound ports needed:

- **A small always-on box** (Raspberry Pi, mini-PC, NAS with Docker/Container Manager, a cheap VPS).
- **This machine**, for testing (`docker compose up`) — but it's only reachable while that
  machine is on.

## Notes / limitations (v1)

- **One room, one user.** The allowlist is a single Matrix ID.
- **Fresh session on restart.** Conversation context is held in memory; restarting the
  container starts a new agent session (no persisted transcript).
- **REST/WS only, no SSH.** Automations, service calls, config-entry flows, and restarts are
  all covered; editing files inside `custom_components/` is not.
- **Serial.** One agent run at a time — fine for a single-user home-automation chat.

## Development

`bot.py` is a single-file bridge — the matrix-nio event loop on one side, a persistent
`ClaudeSDKClient` on the other. `system_prompt.md` is the Home Assistant runbook fed to the
agent. See `CLAUDE.md` for notes aimed at future Claude Code sessions working on this repo.

🤖 Built with [Claude Code](https://claude.com/claude-code)
