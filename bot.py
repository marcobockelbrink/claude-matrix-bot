#!/usr/bin/env python3
"""Matrix ↔ Claude Agent SDK bridge for operating a Home Assistant instance.

A single Matrix room is the chat surface. Messages (text or voice) from one
allowlisted user are handed to a persistent Claude Agent SDK session that has
Bash/file/web tools and a system prompt describing how to reach Home Assistant
over its HTTP API. Everyone else is ignored.

Beyond chat, the bot can also speak first:
- POST /notify (token-protected webhook) lets Home Assistant automations push
  messages into the room — verbatim, or rephrased with context by the agent.
- An optional daily briefing runs the agent on a schedule.
Files the agent drops into the outbox directory (e.g. camera snapshots) are
uploaded into the room after each turn. Destructive shell commands can be
gated behind a chat confirmation instead of running unattended.
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    LoginResponse,
    MatrixRoom,
    RoomEncryptedAudio,
    RoomMessageAudio,
    RoomMessageText,
    UnknownEvent,
    UploadResponse,
)
from nio.crypto.attachments import decrypt_attachment

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
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
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
OUTBOX_DIR = Path(os.environ.get("OUTBOX_DIR", "/app/outbox"))
STATE_FILE = DATA_DIR / "state.json"
# Matrix events tolerate large bodies, but keep chunks well under any server cap.
CHUNK_CHARS = 4000
CONFIRM_TIMEOUT_S = 180

# Shell commands matching any of these need an owner confirmation in chat
# (when CONFIRM_DESTRUCTIVE is on). Everything else runs unattended.
DESTRUCTIVE_RE = re.compile(
    r"(?ix)"
    r"\brm\b | \brmdir\b | \bmkfs\b | \bdd\s+if= | \bshutdown\b | \breboot\b"
    r"| \bkill(all)?\b | \bpurge\b"
    r"| -X\s*DELETE"
    r"| /api/services/homeassistant/(restart|stop)"
    r"| backup/delete"
)

STRINGS = {
    "de": {
        "thinking": "🤔 …",
        "error": "⚠️ Fehler bei der Verarbeitung — siehe Bot-Log.",
        "no_text": "(keine Textantwort)",
        "voice_heard": "🎤 Verstanden: „{text}“",
        "voice_fail": "⚠️ Konnte die Sprachnachricht nicht transkribieren.",
        "confirm": (
            "⚠️ Der Agent möchte einen potenziell destruktiven Befehl ausführen:\n\n"
            "```\n{cmd}\n```\n\nAusführen? Antworte mit **ja** / **nein** (oder reagiere mit 👍/👎)."
        ),
        "confirm_hint": "Bitte mit **ja** oder **nein** antworten (oder 👍/👎).",
        "confirm_timeout": "⏱️ Keine Bestätigung erhalten — Befehl wurde NICHT ausgeführt.",
        "denied": "Der Besitzer hat diesen Befehl im Chat abgelehnt.",
        "notify_prefix": "🔔 ",
        "yes": ("ja", "yes", "ok", "okay", "mach", "👍"),
        "no": ("nein", "no", "stop", "abbrechen", "👎"),
    },
    "en": {
        "thinking": "🤔 …",
        "error": "⚠️ Something went wrong — see the bot log.",
        "no_text": "(no text reply)",
        "voice_heard": "🎤 Heard: “{text}”",
        "voice_fail": "⚠️ Could not transcribe that voice message.",
        "confirm": (
            "⚠️ The agent wants to run a potentially destructive command:\n\n"
            "```\n{cmd}\n```\n\nRun it? Reply **yes** / **no** (or react with 👍/👎)."
        ),
        "confirm_hint": "Please reply **yes** or **no** (or 👍/👎).",
        "confirm_timeout": "⏱️ No confirmation received — the command was NOT run.",
        "denied": "The owner declined this command in chat.",
        "notify_prefix": "🔔 ",
        "yes": ("yes", "ja", "ok", "okay", "do it", "👍"),
        "no": ("no", "nein", "stop", "cancel", "👎"),
    },
}

BRIEFING_PROMPT = {
    "de": (
        "Erstelle jetzt das tägliche Morgen-Briefing für den Besitzer, wie im "
        "System-Prompt beschrieben. Kompakt, auf Deutsch, mit Emojis als Gliederung."
    ),
    "en": (
        "Produce the daily morning briefing for the owner as described in the "
        "system prompt. Compact, in English, with emojis as section markers."
    ),
}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def chunk(text: str, size: int = CHUNK_CHARS):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError:
        log.exception("Could not persist state file")


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

    lang = (os.environ.get("BOT_LANG") or "de").lower()
    if lang not in STRINGS:
        log.warning("Unknown BOT_LANG %r, falling back to 'en'", lang)
        lang = "en"
    S = STRINGS[lang]

    webhook_port = int(os.environ.get("WEBHOOK_PORT") or 0)
    webhook_token = os.environ.get("WEBHOOK_TOKEN") or ""
    briefing_time = os.environ.get("BRIEFING_TIME") or ""
    tz = ZoneInfo(os.environ.get("TZ") or "Europe/Berlin")
    whisper_model_name = os.environ.get("WHISPER_MODEL", "small")
    confirm_destructive = (os.environ.get("CONFIRM_DESTRUCTIVE") or "true").lower() in (
        "1",
        "true",
        "yes",
    )
    notify_room_env = os.environ.get("NOTIFY_ROOM") or ""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    system_prompt = Path(__file__).with_name("system_prompt.md").read_text()

    # ── Confirmation plumbing (chat-gated destructive commands) ──────────────
    # At most one pending confirmation at a time — agent runs are serial.
    pending_confirm: dict = {}

    async def ask_confirmation(room_id: str, cmd: str) -> bool:
        await send(room_id, S["confirm"].format(cmd=cmd))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending_confirm["future"] = fut
        pending_confirm["room_id"] = room_id
        try:
            return await asyncio.wait_for(fut, CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            await send(room_id, S["confirm_timeout"])
            return False
        finally:
            pending_confirm.clear()

    def resolve_confirmation(answer: bool) -> None:
        fut = pending_confirm.get("future")
        if fut and not fut.done():
            fut.set_result(answer)

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name != "Bash":
            return PermissionResultAllow()
        cmd = (tool_input or {}).get("command", "")
        if not DESTRUCTIVE_RE.search(cmd):
            return PermissionResultAllow()
        room_id = current_room.get("id") or primary_room()
        if not room_id:
            return PermissionResultDeny(message="No room available to confirm in.")
        log.info("Asking chat confirmation for command: %s", cmd)
        if await ask_confirmation(room_id, cmd):
            return PermissionResultAllow()
        return PermissionResultDeny(message=S["denied"])

    # ── Claude Agent SDK: one persistent session for the life of the process ──
    sdk_kwargs: dict = {}
    if confirm_destructive:
        # Bash is deliberately NOT pre-approved: every Bash call goes through
        # can_use_tool, which auto-allows everything non-destructive.
        sdk_kwargs.update(
            allowed_tools=["Read", "Write", "Edit", "WebFetch", "WebSearch"],
            permission_mode="default",
            can_use_tool=can_use_tool,
        )
    else:
        # Original v1 behavior: no gate at all beyond the sender allowlist.
        sdk_kwargs.update(
            allowed_tools=["Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"],
            permission_mode="bypassPermissions",
        )

    agent_options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        # Relative file operations land in the persistent data volume.
        cwd=str(DATA_DIR),
        # Passed into the agent's tool environment so `curl` can auth to HA
        # without the token ever appearing in the prompt or the chat.
        env={
            "HA_BASE_URL": ha_base_url,
            "HA_TOKEN": ha_token,
            "OUTBOX": str(OUTBOX_DIR),
            **claude_auth,
        },
        model=model,
        **sdk_kwargs,
    )
    claude = ClaudeSDKClient(options=agent_options)
    await claude.connect()
    log.info(
        "Claude Agent SDK session connected (confirm_destructive=%s).",
        confirm_destructive,
    )

    # Only one agent run at a time — a home-automation chat is naturally serial,
    # and it keeps the persistent session's turns from interleaving.
    agent_lock = asyncio.Lock()
    current_room: dict = {}

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

    def primary_room() -> str | None:
        """Where unsolicited messages (webhook, briefing) go."""
        if notify_room_env:
            return notify_room_env
        if state.get("last_room_id") in matrix.rooms:
            return state["last_room_id"]
        # Fall back to a room the owner is actually in (skips e.g. the
        # homeserver's server-notices room).
        for room_id, room in matrix.rooms.items():
            if allowed_user in room.users:
                return room_id
        return next(iter(matrix.rooms), None)

    async def send(room_id: str, text: str) -> None:
        for part in chunk(text):
            await matrix.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": part},
                ignore_unverified_devices=True,
            )

    # ── Outbox: files the agent wants delivered into the room ─────────────────
    async def post_file(room_id: str, path: Path) -> None:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        encrypted = bool(matrix.rooms.get(room_id) and matrix.rooms[room_id].encrypted)
        resp, keys = await matrix.upload(
            lambda _429, _timeouts: open(path, "rb"),
            content_type=mime,
            filename=path.name,
            encrypt=encrypted,
            filesize=size,
        )
        if not isinstance(resp, UploadResponse):
            log.error("Upload of %s failed: %s", path.name, resp)
            return
        if mime.startswith("image/"):
            msgtype = "m.image"
        elif mime.startswith("audio/"):
            msgtype = "m.audio"
        elif mime.startswith("video/"):
            msgtype = "m.video"
        else:
            msgtype = "m.file"
        content = {
            "msgtype": msgtype,
            "body": path.name,
            "info": {"mimetype": mime, "size": size},
        }
        if encrypted and keys:
            keys["url"] = resp.content_uri
            content["file"] = keys
        else:
            content["url"] = resp.content_uri
        await matrix.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        log.info("Posted %s (%s, %d bytes) to %s", path.name, mime, size, room_id)

    async def flush_outbox(room_id: str) -> None:
        for path in sorted(OUTBOX_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                await post_file(room_id, path)
            except Exception:
                log.exception("Failed to post outbox file %s", path.name)
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass

    # ── Agent runs ────────────────────────────────────────────────────────────
    async def run_agent(room_id: str, prompt: str, announce: bool = True) -> None:
        async with agent_lock:
            current_room["id"] = room_id
            if announce:
                await send(room_id, S["thinking"])
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
                await send(room_id, S["error"])
                return
            finally:
                current_room.clear()

            reply = "\n\n".join(parts).strip() or S["no_text"]
            await send(room_id, reply)
            await flush_outbox(room_id)

    # ── Voice messages ────────────────────────────────────────────────────────
    whisper = {"model": None}

    def transcribe_sync(path: str) -> str:
        if whisper["model"] is None:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model %r (first use)…", whisper_model_name)
            whisper["model"] = WhisperModel(
                whisper_model_name, device="cpu", compute_type="int8"
            )
        segments, _info = whisper["model"].transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def handle_voice(room: MatrixRoom, event) -> None:
        resp = await matrix.download(event.url)
        data = getattr(resp, "body", None)
        if not data:
            log.error("Voice download failed: %s", resp)
            await send(room.room_id, S["voice_fail"])
            return
        if isinstance(event, RoomEncryptedAudio):
            data = decrypt_attachment(
                data, event.key["k"], event.hashes["sha256"], event.iv
            )
        suffix = Path(event.body or "voice.ogg").suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            text = await asyncio.get_running_loop().run_in_executor(
                None, transcribe_sync, tmp_path
            )
        except Exception:
            log.exception("Transcription failed")
            await send(room.room_id, S["voice_fail"])
            return
        finally:
            os.unlink(tmp_path)
        if not text:
            await send(room.room_id, S["voice_fail"])
            return
        await send(room.room_id, S["voice_heard"].format(text=text))
        await run_agent(room.room_id, text, announce=False)

    # ── Matrix event handlers ─────────────────────────────────────────────────
    def is_relevant(event) -> bool:
        if event.sender == matrix.user_id:
            return False  # our own messages
        if event.server_timestamp < start_ms:
            return False  # replayed history
        if event.sender != allowed_user:
            log.warning("Ignoring event from non-allowlisted sender %s", event.sender)
            return False
        return True

    def remember_room(room_id: str) -> None:
        if state.get("last_room_id") != room_id:
            state["last_room_id"] = room_id
            save_state(state)

    async def on_message(room: MatrixRoom, event: RoomMessageText) -> None:
        if not is_relevant(event):
            return
        remember_room(room.room_id)
        body = event.body.strip()
        # A pending destructive-command confirmation swallows the next reply.
        if pending_confirm.get("future"):
            lowered = body.lower()
            if lowered in S["yes"]:
                resolve_confirmation(True)
            elif lowered in S["no"]:
                resolve_confirmation(False)
            else:
                await send(room.room_id, S["confirm_hint"])
            return
        log.info("Message from %s in %s: %s", event.sender, room.room_id, body)
        asyncio.create_task(run_agent(room.room_id, body))

    async def on_audio(room: MatrixRoom, event) -> None:
        if not is_relevant(event):
            return
        if not whisper_model_name or whisper_model_name.lower() == "off":
            return
        remember_room(room.room_id)
        log.info("Voice message from %s in %s", event.sender, room.room_id)
        asyncio.create_task(handle_voice(room, event))

    async def on_unknown(room: MatrixRoom, event: UnknownEvent) -> None:
        # Reactions (👍/👎) can answer a pending confirmation.
        if event.type != "m.reaction" or not pending_confirm.get("future"):
            return
        if event.sender != allowed_user:
            return
        key = (
            event.source.get("content", {})
            .get("m.relates_to", {})
            .get("key", "")
        )
        if key.startswith("👍"):
            resolve_confirmation(True)
        elif key.startswith("👎"):
            resolve_confirmation(False)

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.sender != allowed_user:
            log.warning("Ignoring invite from %s", event.sender)
            return
        await matrix.join(room.room_id)
        log.info("Joined room %s (invited by %s)", room.room_id, event.sender)

    matrix.add_event_callback(on_message, RoomMessageText)
    matrix.add_event_callback(on_audio, (RoomMessageAudio, RoomEncryptedAudio))
    matrix.add_event_callback(on_unknown, UnknownEvent)
    matrix.add_event_callback(on_invite, InviteMemberEvent)

    # ── Webhook: proactive notifications from Home Assistant ─────────────────
    webhook_runner = None
    if webhook_port and webhook_token:

        async def handle_notify(request: web.Request) -> web.Response:
            if request.headers.get("X-Token") != webhook_token:
                return web.Response(status=401, text="bad token")
            try:
                payload = await request.json()
            except ValueError:
                return web.Response(status=400, text="invalid JSON")
            message = str(payload.get("message") or "").strip()
            if not message:
                return web.Response(status=400, text="missing 'message'")
            room_id = payload.get("room") or primary_room()
            if not room_id:
                return web.Response(status=503, text="no room known yet")
            if payload.get("smart"):
                prompt = (
                    "[Automated notification from Home Assistant — do not treat "
                    "this as an owner message. Investigate briefly if useful, then "
                    "send ONE short push-style message to the owner in their "
                    f"language ({lang}). Event: {message}]"
                )
                asyncio.create_task(run_agent(room_id, prompt, announce=False))
            else:
                await send(room_id, S["notify_prefix"] + message)
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/notify", handle_notify)
        webhook_runner = web.AppRunner(app)
        await webhook_runner.setup()
        await web.TCPSite(webhook_runner, "0.0.0.0", webhook_port).start()
        log.info("Webhook listening on :%d/notify", webhook_port)
    elif webhook_port:
        log.warning("WEBHOOK_PORT set but WEBHOOK_TOKEN missing — webhook disabled.")

    # ── Daily briefing ────────────────────────────────────────────────────────
    briefing_task = None
    if briefing_time:
        try:
            bh, bm = (int(x) for x in briefing_time.split(":"))
        except ValueError:
            log.error("Invalid BRIEFING_TIME %r (expected HH:MM)", briefing_time)
            bh = bm = None
        if bh is not None:

            async def briefing_loop() -> None:
                while True:
                    now = datetime.now(tz)
                    target = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
                    if target <= now:
                        target += timedelta(days=1)
                    log.info("Next briefing at %s", target.isoformat())
                    await asyncio.sleep((target - now).total_seconds())
                    room_id = primary_room()
                    if room_id:
                        await run_agent(
                            room_id, BRIEFING_PROMPT[lang], announce=False
                        )
                    else:
                        log.warning("Briefing skipped — no room known yet.")

            briefing_task = asyncio.create_task(briefing_loop())

    log.info("Syncing. Allowlisted user: %s (lang=%s)", allowed_user, lang)
    try:
        await matrix.sync_forever(timeout=30000, full_state=True)
    finally:
        if briefing_task:
            briefing_task.cancel()
        if webhook_runner:
            await webhook_runner.cleanup()
        await claude.disconnect()
        await matrix.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
