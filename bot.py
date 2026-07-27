#!/usr/bin/env python3
"""Matrix/Signal ↔ Claude Agent SDK bridge for operating a Home Assistant instance.

Chat surfaces:
- A Matrix room (E2E-encrypted) — the primary channel.
- Optionally Signal, via a signal-cli-rest-api sidecar container (enable by
  setting SIGNAL_API_URL + SIGNAL_NUMBER; see docker-compose profile "signal").

Messages (text or voice) from allowlisted users are handed to one persistent
Claude Agent SDK session that has Bash/file/web tools and a system prompt
describing how to reach Home Assistant over its HTTP API. Everyone else is
ignored.

Beyond chat, the bot can also speak first:
- POST /notify (token-protected webhook) lets Home Assistant automations push
  messages to all channels — verbatim, or rephrased with context by the agent.
- An optional daily briefing runs the agent on a schedule.
Files the agent drops into the outbox directory (e.g. camera snapshots) are
delivered to the requesting channel after each turn. Destructive shell
commands can be gated behind a chat confirmation instead of running
unattended.
"""

import asyncio
import base64
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
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
    SyncResponse,
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

BOT_VERSION = os.environ.get("BOT_VERSION", "dev")

# Recent log lines, served on the /status page.
LOG_BUFFER: deque = deque(maxlen=200)


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:  # never let logging take the bot down
            pass


_ring = _RingBufferHandler()
_ring.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
)
logging.getLogger().addHandler(_ring)

STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>ha-matrix-bot</title>
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; background: #14161a;
         color: #d8dee4; max-width: 780px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.2rem; }} h2 {{ font-size: 1rem; margin-top: 1.6rem; }}
  .ok {{ color: #7ee787; }} .bad {{ color: #ff7b72; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ text-align: left; padding: .25rem .6rem .25rem 0; vertical-align: top; }}
  pre {{ background: #0d1117; padding: .8rem; border-radius: 8px; overflow-x: auto;
        font-size: 12px; line-height: 1.4; }}
  .muted {{ color: #8b949e; }}
</style></head><body>
<h1>🤖 ha-matrix-bot <span class="muted">{version}</span> <span class="{cls}">{state}</span></h1>
<table>
<tr><td class="muted">Version</td><td>{version}</td></tr>
<tr><td class="muted">Uptime</td><td>{uptime}</td></tr>
<tr><td class="muted">Matrix</td><td>{matrix}</td></tr>
<tr><td class="muted">Signal</td><td>{signal}</td></tr>
<tr><td class="muted">Voice</td><td>{voice}</td></tr>
<tr><td class="muted">Confirm destructive</td><td>{confirm}</td></tr>
<tr><td class="muted">Next briefing</td><td>{briefing}</td></tr>
</table>
<h2>Agent runs</h2>
<table><tr><th>Time</th><th>Via</th><th></th><th>Duration</th><th>Prompt</th></tr>{runs}</table>
<h2>Log</h2>
<pre>{logs}</pre>
<p class="muted">auto-refreshes every 15 s</p>
</body></html>"""

STORE_PATH = "./store"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
OUTBOX_DIR = Path(os.environ.get("OUTBOX_DIR", "/app/outbox"))
STATE_FILE = DATA_DIR / "state.json"
# Matrix events tolerate large bodies, but keep chunks well under any server cap.
CHUNK_CHARS = 4000
CONFIRM_TIMEOUT_S = 180

# A delivery target is ("matrix", room_id) or ("signal", recipient) where a
# Signal recipient is a phone number or "group.<base64 id>".
Target = tuple[str, str]

# Shell commands matching any of these need an owner confirmation in chat
# (when CONFIRM_DESTRUCTIVE is on). Everything else runs unattended.
# NOTE: this is an accident guard, not a security boundary — a sufficiently
# creative command (or a script written first, executed second) can slip past
# any pattern list. See SECURITY.md.
DESTRUCTIVE_RE = re.compile(
    r"(?ix)"
    r"\brm\b | \brmdir\b | \bmkfs\b | \bdd\s+if= | \bshutdown\b | \breboot\b"
    r"| \bkill(all)?\b | \bpurge\b | \btruncate\b | \bshred\b"
    r"| \bfind\b[^|;]*-delete"
    r"| (-X|--request)\s*['\"]?DELETE"
    r"| /api/services/homeassistant/(restart|stop)"
    r"| homeassistant[./](restart|stop)"
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


def env_list(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


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
    # MATRIX_ALLOWED_USERS (comma-separated) supersedes the singular v1 var.
    allowed_users = set(env_list("MATRIX_ALLOWED_USERS"))
    if not allowed_users and os.environ.get("MATRIX_ALLOWED_USER"):
        allowed_users = {os.environ["MATRIX_ALLOWED_USER"]}
    if not allowed_users:
        log.error("Set MATRIX_ALLOWED_USERS (or MATRIX_ALLOWED_USER) in .env")
        sys.exit(1)
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

    # ── Signal (optional second channel) ─────────────────────────────────────
    signal_api = (os.environ.get("SIGNAL_API_URL") or "").rstrip("/")
    signal_number = os.environ.get("SIGNAL_NUMBER") or ""
    signal_allowed = set(env_list("SIGNAL_ALLOWED_NUMBERS"))
    signal_notify = os.environ.get("SIGNAL_NOTIFY") or ""
    signal_enabled = bool(signal_api and signal_number)
    if signal_enabled and not signal_allowed:
        log.warning("Signal enabled but SIGNAL_ALLOWED_NUMBERS empty — disabling.")
        signal_enabled = False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    # ── Status bookkeeping (served on /status) ───────────────────────────────
    started_at = time.time()
    run_history: deque = deque(maxlen=20)
    last_sync = {"ts": 0.0}
    signal_state = {"connected": False}
    briefing_next = {"iso": None}

    system_prompt = Path(__file__).with_name("system_prompt.md").read_text()

    http = aiohttp.ClientSession()

    # ── Confirmation plumbing (chat-gated destructive commands) ──────────────
    # At most one pending confirmation at a time — agent runs are serial.
    pending_confirm: dict = {}

    async def ask_confirmation(target: Target, cmd: str) -> bool:
        await deliver(target, S["confirm"].format(cmd=cmd))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending_confirm["future"] = fut
        pending_confirm["target"] = target
        try:
            return await asyncio.wait_for(fut, CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            await deliver(target, S["confirm_timeout"])
            return False
        finally:
            pending_confirm.clear()

    def resolve_confirmation(answer: bool) -> None:
        fut = pending_confirm.get("future")
        if fut and not fut.done():
            fut.set_result(answer)

    def maybe_answer_confirmation(body: str) -> str | None:
        """Returns 'handled'/'hint' if a confirmation was pending, else None."""
        if not pending_confirm.get("future"):
            return None
        lowered = body.strip().lower()
        if lowered in S["yes"]:
            resolve_confirmation(True)
            return "handled"
        if lowered in S["no"]:
            resolve_confirmation(False)
            return "handled"
        return "hint"

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name != "Bash":
            return PermissionResultAllow()
        cmd = (tool_input or {}).get("command", "")
        if not DESTRUCTIVE_RE.search(cmd):
            return PermissionResultAllow()
        target = current_run.get("targets", [None])[0] or default_matrix_target()
        if not target:
            return PermissionResultDeny(message="No channel available to confirm in.")
        log.info("Asking chat confirmation for command: %s", cmd)
        if await ask_confirmation(target, cmd):
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
        "Claude Agent SDK session connected (version=%s, confirm_destructive=%s, signal=%s).",
        BOT_VERSION,
        confirm_destructive,
        signal_enabled,
    )

    # Only one agent run at a time — a home-automation chat is naturally serial,
    # and it keeps the persistent session's turns from interleaving.
    agent_lock = asyncio.Lock()
    current_run: dict = {}

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

    def default_matrix_target() -> Target | None:
        """Matrix room for unsolicited messages (webhook, briefing)."""
        if notify_room_env:
            return ("matrix", notify_room_env)
        if state.get("last_room_id") in matrix.rooms:
            return ("matrix", state["last_room_id"])
        # Fall back to a room an allowlisted user is actually in (skips e.g.
        # the homeserver's server-notices room).
        for room_id, room in matrix.rooms.items():
            if allowed_users & set(room.users):
                return ("matrix", room_id)
        first = next(iter(matrix.rooms), None)
        return ("matrix", first) if first else None

    def notify_targets() -> list[Target]:
        targets: list[Target] = []
        matrix_target = default_matrix_target()
        if matrix_target:
            targets.append(matrix_target)
        if signal_enabled and signal_notify:
            targets.append(("signal", signal_notify))
        return targets

    async def matrix_send(room_id: str, text: str) -> None:
        for part in chunk(text):
            await matrix.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": part},
                ignore_unverified_devices=True,
            )

    # ── Signal send/receive ───────────────────────────────────────────────────
    async def signal_send(recipient: str, text: str) -> None:
        for part in chunk(text):
            async with http.post(
                f"{signal_api}/v2/send",
                json={
                    "message": part,
                    "number": signal_number,
                    "recipients": [recipient],
                },
            ) as resp:
                if resp.status >= 400:
                    log.error("Signal send failed (%d): %s", resp.status, await resp.text())

    async def signal_send_file(recipient: str, path: Path) -> None:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        b64 = base64.b64encode(path.read_bytes()).decode()
        async with http.post(
            f"{signal_api}/v2/send",
            json={
                "message": "",
                "number": signal_number,
                "recipients": [recipient],
                "base64_attachments": [
                    f"data:{mime};filename={path.name};base64,{b64}"
                ],
            },
        ) as resp:
            if resp.status >= 400:
                log.error(
                    "Signal attachment send failed (%d): %s",
                    resp.status,
                    await resp.text(),
                )

    async def deliver(target: Target, text: str) -> None:
        kind, dest = target
        if kind == "matrix":
            await matrix_send(dest, text)
        else:
            await signal_send(dest, text)

    # ── Outbox: files the agent wants delivered into the chat ─────────────────
    async def matrix_post_file(room_id: str, path: Path) -> None:
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

    async def flush_outbox(targets: list[Target]) -> None:
        for path in sorted(OUTBOX_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            for target in targets:
                kind, dest = target
                try:
                    if kind == "matrix":
                        await matrix_post_file(dest, path)
                    else:
                        await signal_send_file(dest, path)
                except Exception:
                    log.exception("Failed to post outbox file %s to %s", path.name, target)
            try:
                path.unlink()
            except OSError:
                pass

    # ── Agent runs ────────────────────────────────────────────────────────────
    async def run_agent(
        targets: list[Target], prompt: str, announce: bool = True
    ) -> None:
        async with agent_lock:
            current_run["targets"] = targets
            if announce:
                await deliver(targets[0], S["thinking"])
            parts: list[str] = []
            t0 = time.time()
            ok = True
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
                ok = False
                log.exception("Agent run failed")
                await deliver(targets[0], S["error"])
                return
            finally:
                current_run.clear()
                run_history.appendleft(
                    {
                        "time": datetime.now(tz).isoformat(timespec="seconds"),
                        "via": targets[0][0],
                        "ok": ok,
                        "duration_s": round(time.time() - t0, 1),
                        "prompt": prompt[:80],
                    }
                )

            reply = "\n\n".join(parts).strip() or S["no_text"]
            for target in targets:
                await deliver(target, reply)
            await flush_outbox(targets)

    # ── Voice messages ────────────────────────────────────────────────────────
    whisper = {"model": None}
    voice_enabled = bool(whisper_model_name) and whisper_model_name.lower() != "off"

    def transcribe_sync(path: str) -> str:
        if whisper["model"] is None:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model %r (first use)…", whisper_model_name)
            whisper["model"] = WhisperModel(
                whisper_model_name, device="cpu", compute_type="int8"
            )
        segments, _info = whisper["model"].transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe_and_run(target: Target, data: bytes, suffix: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            text = await asyncio.get_running_loop().run_in_executor(
                None, transcribe_sync, tmp_path
            )
        except Exception:
            log.exception("Transcription failed")
            await deliver(target, S["voice_fail"])
            return
        finally:
            os.unlink(tmp_path)
        if not text:
            await deliver(target, S["voice_fail"])
            return
        await deliver(target, S["voice_heard"].format(text=text))
        await run_agent([target], text, announce=False)

    async def handle_matrix_voice(room: MatrixRoom, event) -> None:
        resp = await matrix.download(event.url)
        data = getattr(resp, "body", None)
        if not data:
            log.error("Voice download failed: %s", resp)
            await matrix_send(room.room_id, S["voice_fail"])
            return
        if isinstance(event, RoomEncryptedAudio):
            data = decrypt_attachment(
                data, event.key["k"], event.hashes["sha256"], event.iv
            )
        suffix = Path(event.body or "voice.ogg").suffix or ".ogg"
        await transcribe_and_run(("matrix", room.room_id), data, suffix)

    # ── Matrix event handlers ─────────────────────────────────────────────────
    def is_relevant(event) -> bool:
        if event.sender == matrix.user_id:
            return False  # our own messages
        if event.server_timestamp < start_ms:
            return False  # replayed history
        if event.sender not in allowed_users:
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
        outcome = maybe_answer_confirmation(body)
        if outcome == "hint":
            await matrix_send(room.room_id, S["confirm_hint"])
        if outcome:
            return
        log.info("Message from %s in %s: %s", event.sender, room.room_id, body)
        asyncio.create_task(run_agent([("matrix", room.room_id)], body))

    async def on_audio(room: MatrixRoom, event) -> None:
        if not is_relevant(event) or not voice_enabled:
            return
        remember_room(room.room_id)
        log.info("Voice message from %s in %s", event.sender, room.room_id)
        asyncio.create_task(handle_matrix_voice(room, event))

    async def on_unknown(room: MatrixRoom, event: UnknownEvent) -> None:
        # Reactions (👍/👎) can answer a pending confirmation.
        if event.type != "m.reaction" or not pending_confirm.get("future"):
            return
        if event.sender not in allowed_users:
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
        if event.sender not in allowed_users:
            log.warning("Ignoring invite from %s", event.sender)
            return
        await matrix.join(room.room_id)
        log.info("Joined room %s (invited by %s)", room.room_id, event.sender)

    async def on_sync(_response: SyncResponse) -> None:
        last_sync["ts"] = time.time()

    matrix.add_event_callback(on_message, RoomMessageText)
    matrix.add_event_callback(on_audio, (RoomMessageAudio, RoomEncryptedAudio))
    matrix.add_event_callback(on_unknown, UnknownEvent)
    matrix.add_event_callback(on_invite, InviteMemberEvent)
    matrix.add_response_callback(on_sync, SyncResponse)

    # ── Signal receive loop ───────────────────────────────────────────────────
    async def handle_signal_envelope(data: dict) -> None:
        envelope = data.get("envelope") or {}
        dm = envelope.get("dataMessage") or {}
        sender = envelope.get("sourceNumber") or envelope.get("source") or ""
        if not dm or sender == signal_number:
            return
        if sender not in signal_allowed:
            if sender:
                log.warning("Ignoring Signal message from non-allowlisted %s", sender)
            return
        group_id = (dm.get("groupInfo") or {}).get("groupId")
        recipient = f"group.{group_id}" if group_id else sender
        target: Target = ("signal", recipient)
        text = (dm.get("message") or "").strip()

        outcome = maybe_answer_confirmation(text) if text else None
        if outcome == "hint":
            await signal_send(recipient, S["confirm_hint"])
        if outcome:
            return

        voice_att = next(
            (
                att
                for att in dm.get("attachments") or []
                if (att.get("contentType") or "").startswith("audio/")
            ),
            None,
        )
        if voice_att and voice_enabled:
            att_id = voice_att.get("id")
            log.info("Signal voice message from %s", sender)
            async with http.get(f"{signal_api}/v1/attachments/{quote(att_id)}") as resp:
                if resp.status >= 400:
                    log.error("Signal attachment download failed (%d)", resp.status)
                    await signal_send(recipient, S["voice_fail"])
                    return
                audio = await resp.read()
            suffix = (
                mimetypes.guess_extension(voice_att.get("contentType") or "")
                or ".ogg"
            )
            asyncio.create_task(transcribe_and_run(target, audio, suffix))
            return

        if text:
            log.info("Signal message from %s: %s", sender, text)
            asyncio.create_task(run_agent([target], text))

    async def signal_loop() -> None:
        ws_base = signal_api.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/v1/receive/{quote(signal_number)}"
        while True:
            try:
                async with http.ws_connect(url, heartbeat=30) as ws:
                    log.info("Signal websocket connected (%s).", signal_number)
                    signal_state["connected"] = True
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await handle_signal_envelope(json.loads(msg.data))
                            except Exception:
                                log.exception("Error handling Signal envelope")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Signal connection lost (%s) — retrying in 10s", exc)
            finally:
                signal_state["connected"] = False
            await asyncio.sleep(10)

    signal_task = asyncio.create_task(signal_loop()) if signal_enabled else None

    # ── Webhook: proactive notifications from Home Assistant ─────────────────
    webhook_runner = None
    if webhook_port and webhook_token:

        def token_ok(request: web.Request) -> bool:
            supplied = request.headers.get("X-Token") or request.query.get("token") or ""
            return hmac.compare_digest(supplied, webhook_token)

        async def handle_notify(request: web.Request) -> web.Response:
            if not token_ok(request):
                return web.Response(status=401, text="bad token")
            try:
                payload = await request.json()
            except ValueError:
                return web.Response(status=400, text="invalid JSON")
            message = str(payload.get("message") or "").strip()
            if not message:
                return web.Response(status=400, text="missing 'message'")
            if payload.get("room"):
                targets: list[Target] = [("matrix", payload["room"])]
            else:
                targets = notify_targets()
            if not targets:
                return web.Response(status=503, text="no channel known yet")
            if payload.get("smart"):
                prompt = (
                    "[Automated notification from Home Assistant — do not treat "
                    "this as an owner message. Investigate briefly if useful, then "
                    "send ONE short push-style message to the owner in their "
                    f"language ({lang}). Event: {message}]"
                )
                asyncio.create_task(run_agent(targets, prompt, announce=False))
            else:
                for target in targets:
                    await deliver(target, S["notify_prefix"] + message)
            return web.json_response({"ok": True})

        def status_payload() -> dict:
            now = time.time()
            return {
                "ok": True,
                "version": BOT_VERSION,
                "uptime_s": int(now - started_at),
                "matrix_connected": bool(
                    last_sync["ts"] and now - last_sync["ts"] < 120
                ),
                "signal_enabled": signal_enabled,
                "signal_connected": signal_state["connected"],
                "voice": whisper_model_name if voice_enabled else "off",
                "confirm_destructive": confirm_destructive,
                "next_briefing": briefing_next["iso"],
                "runs_total": len(run_history),
                "last_run": run_history[0] if run_history else None,
                "lang": lang,
            }

        async def handle_status(request: web.Request) -> web.Response:
            if not token_ok(request):
                return web.Response(status=401, text="bad token")
            wants_html = "text/html" in request.headers.get("Accept", "")
            if request.query.get("format") == "json" or not wants_html:
                return web.json_response(status_payload())
            p = status_payload()

            def fmt_uptime(s: int) -> str:
                d, rem = divmod(s, 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"

            def badge(up: bool, on_text="connected", off_text="disconnected") -> str:
                return (
                    f'<span class="ok">● {on_text}</span>'
                    if up
                    else f'<span class="bad">● {off_text}</span>'
                )

            runs_html = "".join(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{} s</td><td>{}</td></tr>".format(
                    html.escape(r["time"]),
                    html.escape(r["via"]),
                    "✅" if r["ok"] else "❌",
                    r["duration_s"],
                    html.escape(r["prompt"]),
                )
                for r in run_history
            ) or '<tr><td colspan="5" class="muted">none yet</td></tr>'
            page = STATUS_PAGE.format(
                version=html.escape(BOT_VERSION),
                cls="ok" if p["matrix_connected"] else "bad",
                state="online" if p["matrix_connected"] else "degraded",
                uptime=fmt_uptime(p["uptime_s"]),
                matrix=badge(p["matrix_connected"]),
                signal=badge(p["signal_connected"]) if signal_enabled else "off",
                voice=html.escape(p["voice"]),
                confirm="on" if confirm_destructive else "off",
                briefing=html.escape(p["next_briefing"] or "off"),
                runs=runs_html,
                logs=html.escape("\n".join(list(LOG_BUFFER)[-100:])),
            )
            return web.Response(text=page, content_type="text/html")

        app = web.Application()
        app.router.add_post("/notify", handle_notify)
        app.router.add_get("/status", handle_status)
        webhook_runner = web.AppRunner(app)
        await webhook_runner.setup()
        await web.TCPSite(webhook_runner, "0.0.0.0", webhook_port).start()
        log.info("Webhook + status listening on :%d", webhook_port)
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
                    target_dt = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
                    if target_dt <= now:
                        target_dt += timedelta(days=1)
                    briefing_next["iso"] = target_dt.isoformat(timespec="minutes")
                    log.info("Next briefing at %s", target_dt.isoformat())
                    await asyncio.sleep((target_dt - now).total_seconds())
                    targets = notify_targets()
                    if targets:
                        await run_agent(targets, BRIEFING_PROMPT[lang], announce=False)
                    else:
                        log.warning("Briefing skipped — no channel known yet.")

            briefing_task = asyncio.create_task(briefing_loop())

    log.info(
        "Syncing. Matrix allowlist: %s | Signal: %s (lang=%s)",
        ", ".join(sorted(allowed_users)),
        ", ".join(sorted(signal_allowed)) if signal_enabled else "off",
        lang,
    )
    try:
        await matrix.sync_forever(timeout=30000, full_state=True)
    finally:
        for task in (briefing_task, signal_task):
            if task:
                task.cancel()
        if webhook_runner:
            await webhook_runner.cleanup()
        await http.close()
        await claude.disconnect()
        await matrix.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
