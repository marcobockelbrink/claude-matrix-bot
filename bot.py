#!/usr/bin/env python3
"""Matrix ↔ Claude Agent SDK bridge for operating a Home Assistant instance.

A single Matrix room is the chat surface. Messages from one allowlisted user are
handed to a persistent Claude Agent SDK session that has Bash/file/web tools and a
system prompt describing how to reach Home Assistant over its HTTP API. Everyone
else is ignored.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    LoginResponse,
    MatrixRoom,
    RoomMessageText,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ha-matrix-bot")

STORE_PATH = "./store"
# Matrix events tolerate large bodies, but keep chunks well under any server cap.
CHUNK_CHARS = 4000


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def chunk(text: str, size: int = CHUNK_CHARS):
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def main() -> None:
    # Either a Claude subscription OAuth token (`claude setup-token`) or a
    # pay-per-use API key from console.anthropic.com works; the Agent SDK
    # picks up whichever env var is set.
    claude_auth = {
        name: value
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        if (value := os.environ.get(name))
    }
    if not claude_auth:
        log.error(
            "Missing Claude credentials: set CLAUDE_CODE_OAUTH_TOKEN "
            "(from `claude setup-token`) or ANTHROPIC_API_KEY in .env"
        )
        sys.exit(1)
    ha_base_url = require_env("HA_BASE_URL").rstrip("/")
    ha_token = require_env("HA_TOKEN")
    homeserver = require_env("MATRIX_HOMESERVER")
    matrix_user = require_env("MATRIX_USER")
    matrix_password = require_env("MATRIX_PASSWORD")
    allowed_user = require_env("MATRIX_ALLOWED_USER")
    model = os.environ.get("CLAUDE_MODEL") or None

    system_prompt = Path(__file__).with_name("system_prompt.md").read_text()

    # ── Claude Agent SDK: one persistent session for the life of the process ──
    agent_options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=["Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"],
        # No human sits at a terminal to approve tool calls — the Matrix user is
        # the human, and granting full HA control is the whole point of this bot.
        permission_mode="bypassPermissions",
        # Passed into the agent's tool environment so `curl` can auth to HA
        # without the token ever appearing in the prompt or the chat.
        env={
            "HA_BASE_URL": ha_base_url,
            "HA_TOKEN": ha_token,
            **claude_auth,
        },
        model=model,
    )
    claude = ClaudeSDKClient(options=agent_options)
    await claude.connect()
    log.info("Claude Agent SDK session connected.")

    # Only one agent run at a time — a home-automation chat is naturally serial,
    # and it keeps the persistent session's turns from interleaving.
    agent_lock = asyncio.Lock()

    # ── Matrix client (end-to-end encrypted) ──────────────────────────────────
    os.makedirs(STORE_PATH, exist_ok=True)
    matrix = AsyncClient(
        homeserver,
        user=matrix_user,
        store_path=STORE_PATH,
        config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
    )

    login = await matrix.login(matrix_password, device_name="ha-matrix-bot")
    if not isinstance(login, LoginResponse):
        log.error("Matrix login failed: %s", login)
        await claude.disconnect()
        sys.exit(1)
    log.info("Logged in to Matrix as %s (device %s).", matrix.user_id, matrix.device_id)

    if matrix.should_upload_keys:
        await matrix.keys_upload()

    # Ignore any history the server replays on the first sync.
    start_ms = int(time.time() * 1000)

    async def send(room_id: str, text: str) -> None:
        for part in chunk(text):
            await matrix.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": part},
                ignore_unverified_devices=True,
            )

    async def run_agent(room_id: str, prompt: str) -> None:
        async with agent_lock:
            await send(room_id, "🤔 …")
            parts: list[str] = []
            try:
                await claude.query(prompt)
                async for message in claude.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        if message.subtype and message.subtype != "success":
                            log.warning("Agent turn ended: %s", message.subtype)
            except Exception:
                log.exception("Agent run failed")
                await send(room_id, "⚠️ Fehler bei der Verarbeitung — siehe Bot-Log.")
                return

            reply = "\n\n".join(parts).strip() or "(keine Textantwort)"
            await send(room_id, reply)

    async def on_message(room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == matrix.user_id:
            return  # our own messages
        if event.server_timestamp < start_ms:
            return  # replayed history
        if event.sender != allowed_user:
            log.warning("Ignoring message from non-allowlisted sender %s", event.sender)
            return
        log.info("Message from %s in %s: %s", event.sender, room.room_id, event.body)
        await run_agent(room.room_id, event.body)

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.sender != allowed_user:
            log.warning("Ignoring invite from %s", event.sender)
            return
        await matrix.join(room.room_id)
        log.info("Joined room %s (invited by %s)", room.room_id, event.sender)

    matrix.add_event_callback(on_message, RoomMessageText)
    matrix.add_event_callback(on_invite, InviteMemberEvent)

    log.info("Syncing. Allowlisted user: %s", allowed_user)
    try:
        await matrix.sync_forever(timeout=30000, full_state=True)
    finally:
        await claude.disconnect()
        await matrix.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
