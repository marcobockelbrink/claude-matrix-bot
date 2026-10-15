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
import ctypes
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
from dataclasses import dataclass, field
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
    WhoamiResponse,
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
    ToolUseBlock,
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
<tr><td class="muted">Agent</td><td>{agent}</td></tr>
<tr><td class="muted">Matrix</td><td>{matrix}</td></tr>
<tr><td class="muted">Signal</td><td>{signal}</td></tr>
<tr><td class="muted">Voice</td><td>{voice}</td></tr>
<tr><td class="muted">Confirm destructive</td><td>{confirm}</td></tr>
<tr><td class="muted">Next briefing</td><td>{briefing}</td></tr>
<tr><td class="muted">Next restart</td><td>{restart}</td></tr>
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
# Matrix session (device id + access token) so restarts reuse one device
# instead of piling up a new "unverified device" on every start.
SESSION_FILE = DATA_DIR / "matrix_session.json"
# Matrix events tolerate large bodies, but keep chunks well under any server cap.
CHUNK_CHARS = 4000
CONFIRM_TIMEOUT_S = 180
# Window for NOTIFY_SMART_LIMIT, the cap on agent runs a (possibly looping)
# automation can trigger through smart notifications.
NOTIFY_SMART_WINDOW_S = 600
MIN_WEBHOOK_TOKEN_CHARS = 24
# A restart loop must not flood the chat with "back online" messages.
ONLINE_NOTICE_MIN_GAP_S = 600
# A wedged agent run holds the serial lock, which would silently swallow every
# later message — cap it instead.
AGENT_TIMEOUT_S = int(os.environ.get("AGENT_TIMEOUT_S") or 900)
# How long an interrupted turn gets to wind down before the session is rebuilt.
ABORT_DRAIN_TIMEOUT_S = 60

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
        "online": "🟢 Bin wieder online und einsatzbereit (Version {version}).",
        "agent_timeout": "⏱️ Der Agent hat zu lange gebraucht und wurde abgebrochen. Bitte nochmal versuchen.",
        "agent_reset": "⚠️ Der Agent hat nicht richtig geantwortet ({reason}). Die Sitzung wurde neu gestartet — bitte nochmal senden.",
        "agent_reset_partial": "⚠️ Der Agent hat nicht richtig geantwortet ({reason}). Die Sitzung wurde neu gestartet. Achtung: Ein Teil der Anfrage wurde möglicherweise schon ausgeführt — bitte erst prüfen, bevor du sie nochmal sendest.",
        "agent_unusable": "⚠️ Der Agent kann gerade nicht antworten ({reason}).",
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
        "online": "🟢 Back online and ready (version {version}).",
        "agent_timeout": "⏱️ The agent took too long and was aborted. Please try again.",
        "agent_reset": "⚠️ The agent did not answer properly ({reason}). Its session was restarted — please send that again.",
        "agent_reset_partial": "⚠️ The agent did not answer properly ({reason}). Its session was restarted. Careful: part of the request may already have been carried out — check before sending it again.",
        "agent_unusable": "⚠️ The agent cannot answer right now ({reason}).",
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


def hide_secrets_from_agent(*names: str) -> None:
    """Keep bot-only secrets out of reach of the agent's Bash tool.

    The agent's CLI inherits our environment, and running as the same user it
    could also read our original environment back from /proc.
    """
    for name in names:
        os.environ.pop(name, None)
    if sys.platform != "linux":
        return
    # PR_SET_DUMPABLE = 0 makes /proc/<pid>/environ and /proc/<pid>/mem root-only.
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
        log.warning("Could not hide the process environment from the agent.")


def parse_notify(payload: object) -> tuple[str, str | None, bool]:
    """Validate a /notify body and return (message, room, smart).

    Raises ValueError if the body is malformed.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    message = payload.get("message")
    # A bare 0 from a sensor template is a message, not a missing one.
    message = "" if message is None else str(message).strip()
    if not message:
        raise ValueError("missing 'message'")
    room = payload.get("room")
    if room is not None and not isinstance(room, str):
        raise ValueError("'room' must be a string")
    return message, room or None, bool(payload.get("smart"))


# A fresh session does not cure these; replacing it would only lose the conversation.
ERRORS_A_NEW_SESSION_CANNOT_FIX = {
    "authentication_failed",
    "billing_error",
    "rate_limit",
    "server_error",
}
# The turn stopped at a configured limit; its partial answer is still usable.
LIMIT_SUBTYPES = {"error_max_turns", "error_max_budget_usd"}


def judge_turn(result, api_errors: list[str]) -> tuple[str, bool] | None:
    """Decide whether an agent turn can be trusted.

    Returns None if it can, otherwise (reason, reset) where reset says whether
    a fresh session is worth trying. `result` is the turn's ResultMessage (None
    if the stream ended without one), `api_errors` the error codes its
    assistant messages carried. A turn can report "success" and still be an
    API error dressed up as an answer — that is what this catches.
    """
    if result is not None and result.subtype in LIMIT_SUBTYPES:
        # Wins over API errors the SDK retried on the way to the limit.
        return None
    if api_errors:
        code = api_errors[0]
        return f"API error: {code}", code not in ERRORS_A_NEW_SESSION_CANNOT_FIX
    if result is None:
        return "the turn ended without a result", True
    if result.is_error and result.subtype == "success":
        status = getattr(result, "api_error_status", None)
        if status in (401, 403, 429) or (status or 0) >= 500:
            return f"API error: HTTP {status}", False
        return "the answer is an error" + (f", HTTP {status}" if status else ""), True
    if result.subtype != "success":
        # e.g. error_during_execution, what a crashed session reports.
        return f"the turn ended with {result.subtype}", True
    return None


class SmartBudget:
    """Caps agent runs started by smart notifications to `limit` per window."""

    def __init__(
        self, limit: int, window_s: float = NOTIFY_SMART_WINDOW_S, clock=time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._taken: deque[float] = deque()

    def take(self) -> bool:
        """Claim one run; False if the window is used up."""
        now = self._clock()
        while self._taken and now - self._taken[0] >= self.window_s:
            self._taken.popleft()
        if len(self._taken) >= self.limit:
            return False
        self._taken.append(now)
        return True


def chunk(text: str, size: int = CHUNK_CHARS):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def loggable(text: str, limit: int = 200) -> str:
    """Flatten untrusted text so it can't forge extra log lines."""
    # Line breaks are stripped explicitly (not just via the control-char
    # regex) so static analysis recognises this as the sanitizer it is.
    flat = str(text).replace("\r", " ").replace("\n", " ")
    flat = re.sub(r"[\x00-\x1f\x7f]+", " ", flat).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


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


def should_announce_start(state: dict, now: float, after_planned: bool = False) -> bool:
    """Whether this start is worth a "back online" message to the owner.

    Not after the scheduled restart (nobody wants that every night) unless the
    owner opted in with after_planned, and not again within
    ONLINE_NOTICE_MIN_GAP_S of the last one (restart loops).
    """
    if state.get("planned_restart") and not after_planned:
        return False
    last = state.get("online_notice_ts")
    if not isinstance(last, (int, float)):
        return True
    return now - last >= ONLINE_NOTICE_MIN_GAP_S


def load_session() -> dict | None:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_session(client: AsyncClient) -> None:
    try:
        SESSION_FILE.write_text(
            json.dumps(
                {
                    "user_id": client.user_id,
                    "device_id": client.device_id,
                    "access_token": client.access_token,
                }
            )
        )
        SESSION_FILE.chmod(0o600)  # holds an access token
    except OSError:
        log.exception("Could not persist Matrix session")


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignalConfig:
    api: str = ""
    number: str = ""
    allowed: frozenset[str] = frozenset()
    notify: str = ""
    enabled: bool = False


@dataclass(frozen=True)
class Config:
    # Secrets stay out of repr(), so a logged or printed Config cannot leak them
    # (the log is shown on /status).
    claude_auth: dict = field(repr=False)
    ha_base_url: str
    ha_token: str = field(repr=False)
    homeserver: str
    matrix_user: str
    matrix_password: str = field(repr=False)
    allowed_users: frozenset[str]
    model: str | None
    lang: str
    webhook_port: int
    webhook_token: str = field(repr=False)
    notify_smart_limit: int
    briefing_time: str
    restart_time: str
    announce_planned_restart: bool
    tz: ZoneInfo
    whisper_model: str
    confirm_destructive: bool
    notify_room: str
    signal: SignalConfig

    @property
    def voice_enabled(self) -> bool:
        return bool(self.whisper_model) and self.whisper_model.lower() != "off"


def claude_credentials() -> dict[str, str]:
    # Either a Claude subscription OAuth token (`claude setup-token`) or a
    # pay-per-use API key from console.anthropic.com works; the Agent SDK
    # picks up whichever env var is set.
    auth = {
        name: value
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        if (value := os.environ.get(name))
    }
    if not auth:
        log.error(
            "Missing Claude credentials: set CLAUDE_CODE_OAUTH_TOKEN "
            "(from `claude setup-token`) or ANTHROPIC_API_KEY in .env"
        )
        sys.exit(1)
    return auth


def matrix_allowed_users() -> frozenset[str]:
    # MATRIX_ALLOWED_USERS (comma-separated) supersedes the singular v1 var.
    users = set(env_list("MATRIX_ALLOWED_USERS"))
    if not users and os.environ.get("MATRIX_ALLOWED_USER"):
        users = {os.environ["MATRIX_ALLOWED_USER"]}
    if not users:
        log.error("Set MATRIX_ALLOWED_USERS (or MATRIX_ALLOWED_USER) in .env")
        sys.exit(1)
    return frozenset(users)


def bot_language() -> str:
    lang = (os.environ.get("BOT_LANG") or "de").lower()
    if lang not in STRINGS:
        log.warning("Unknown BOT_LANG %r, falling back to 'en'", lang)
        return "en"
    return lang


def env_flag(name: str, default: str, accepted: tuple[str, ...]) -> bool:
    return (os.environ.get(name) or default).lower() in accepted


def signal_config() -> SignalConfig:
    api = (os.environ.get("SIGNAL_API_URL") or "").rstrip("/")
    number = os.environ.get("SIGNAL_NUMBER") or ""
    allowed = frozenset(env_list("SIGNAL_ALLOWED_NUMBERS"))
    enabled = bool(api and number)
    if enabled and not allowed:
        log.warning("Signal enabled but SIGNAL_ALLOWED_NUMBERS empty — disabling.")
        enabled = False
    return SignalConfig(
        api=api,
        number=number,
        allowed=allowed,
        notify=os.environ.get("SIGNAL_NOTIFY") or "",
        enabled=enabled,
    )


def load_config() -> Config:
    """Read and validate the environment; exits with a message if it is unusable."""
    return Config(
        claude_auth=claude_credentials(),
        ha_base_url=require_env("HA_BASE_URL").rstrip("/"),
        ha_token=require_env("HA_TOKEN"),
        homeserver=require_env("MATRIX_HOMESERVER"),
        matrix_user=require_env("MATRIX_USER"),
        matrix_password=require_env("MATRIX_PASSWORD"),
        allowed_users=matrix_allowed_users(),
        model=os.environ.get("CLAUDE_MODEL") or None,
        lang=bot_language(),
        webhook_port=int(os.environ.get("WEBHOOK_PORT") or 0),
        webhook_token=os.environ.get("WEBHOOK_TOKEN") or "",
        notify_smart_limit=int(os.environ.get("NOTIFY_SMART_LIMIT") or 6),
        briefing_time=os.environ.get("BRIEFING_TIME") or "",
        restart_time=os.environ.get("RESTART_TIME") or "03:00",
        announce_planned_restart=env_flag(
            "ANNOUNCE_SCHEDULED_RESTART", "false", ("1", "true", "yes", "on")
        ),
        tz=ZoneInfo(os.environ.get("TZ") or "Europe/Berlin"),
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        confirm_destructive=env_flag("CONFIRM_DESTRUCTIVE", "true", ("1", "true", "yes")),
        notify_room=os.environ.get("NOTIFY_ROOM") or "",
        signal=signal_config(),
    )


# ── Scheduling ───────────────────────────────────────────────────────────────


def parse_clock(value: str) -> tuple[int, int] | None:
    """"HH:MM" → (hour, minute); None if it is not a valid time of day."""
    try:
        hour, minute = (int(x) for x in value.split(":"))
    except ValueError:
        return None
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def next_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    """The next time the wall clock shows hour:minute, strictly after now."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target + timedelta(days=1) if target <= now else target


# ── Status page ──────────────────────────────────────────────────────────────


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def status_badge(up: bool, on_text="connected", off_text="disconnected") -> str:
    if up:
        return f'<span class="ok">● {on_text}</span>'
    return f'<span class="bad">● {off_text}</span>'


def render_runs(run_history) -> str:
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{} s</td><td>{}</td></tr>".format(
            html.escape(r["time"]),
            html.escape(r["via"]),
            "✅" if r["ok"] else "❌",
            r["duration_s"],
            html.escape(r["prompt"]),
        )
        for r in run_history
    )
    return rows or '<tr><td colspan="5" class="muted">none yet</td></tr>'


def render_status_page(p: dict, run_history) -> str:
    """The HTML status page for a status_payload() dict."""
    agent = status_badge(p["agent_ok"], "ok", "failing")
    if p["agent_failure"]:
        agent += " " + html.escape(p["agent_failure"])
    matrix_up = p["matrix_connected"]
    return STATUS_PAGE.format(
        version=html.escape(BOT_VERSION),
        cls="ok" if matrix_up else "bad",
        state="online" if matrix_up else "degraded",
        uptime=format_uptime(p["uptime_s"]),
        agent=agent,
        matrix=status_badge(matrix_up),
        signal=status_badge(p["signal_connected"]) if p["signal_enabled"] else "off",
        voice=html.escape(p["voice"]),
        confirm="on" if p["confirm_destructive"] else "off",
        briefing=html.escape(p["next_briefing"] or "off"),
        restart=html.escape(p["next_restart"] or "off"),
        runs=render_runs(run_history),
        logs=html.escape("\n".join(list(LOG_BUFFER)[-100:])),
    )


# ── Chat helpers ─────────────────────────────────────────────────────────────

MATRIX_MSGTYPES = {"image": "m.image", "audio": "m.audio", "video": "m.video"}


def matrix_msgtype(mime: str) -> str:
    return MATRIX_MSGTYPES.get(mime.split("/", 1)[0], "m.file")


def matrix_file_content(name: str, mime: str, size: int, uri: str, keys: dict | None) -> dict:
    """The m.room.message content for an uploaded file; keys only if encrypted."""
    content = {
        "msgtype": matrix_msgtype(mime),
        "body": name,
        "info": {"mimetype": mime, "size": size},
    }
    if keys:
        keys["url"] = uri
        content["file"] = keys
    else:
        content["url"] = uri
    return content


def signal_voice_attachment(dm: dict) -> dict | None:
    return next(
        (
            att
            for att in dm.get("attachments") or []
            if (att.get("contentType") or "").startswith("audio/")
        ),
        None,
    )


def unusable_turn_text(strings: dict, reset: bool, used_tools: bool) -> str:
    """What to tell the owner about a turn that could not be used."""
    if not reset:
        return strings["agent_unusable"]
    return strings["agent_reset_partial" if used_tools else "agent_reset"]


@dataclass
class TurnState:
    """What one agent turn produced; reset between attempts."""

    parts: list[str] = field(default_factory=list)
    result: object = None
    api_errors: list[str] = field(default_factory=list)
    used_tools: bool = False

    def reset(self) -> None:
        self.parts.clear()
        self.result = None
        self.api_errors = []
        self.used_tools = False

    def absorb(self, message) -> None:
        """Fold one message of the agent's stream into the turn."""
        if isinstance(message, AssistantMessage):
            self._absorb_assistant(message)
        elif isinstance(message, ResultMessage):
            self.result = message
            self._log_result(message)

    def _absorb_assistant(self, message: AssistantMessage) -> None:
        if getattr(message, "error", None):
            self.api_errors.append(message.error)
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                self.parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                self.used_tools = True

    def _log_result(self, message: ResultMessage) -> None:
        # One line per turn: the evidence when a turn looks fine from outside
        # but did nothing.
        log.info(
            "Agent turn: subtype=%s is_error=%s api_status=%s "
            "api_errors=%s api_ms=%s turns=%s",
            loggable(str(message.subtype), 40),
            message.is_error,
            getattr(message, "api_error_status", None),
            loggable(",".join(self.api_errors) or "-", 80),
            message.duration_api_ms,
            message.num_turns,
        )
        if message.subtype and message.subtype != "success":
            log.warning("Agent turn ended: %s", message.subtype)


# ── The bot ──────────────────────────────────────────────────────────────────


class Bot:
    """One agent session, wired to Matrix (and optionally Signal)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.S = STRINGS[cfg.lang]
        self.started_at = time.time()
        self.http: aiohttp.ClientSession | None = None
        self.claude: ClaudeSDKClient | None = None
        self.matrix: AsyncClient | None = None
        # Ignore any history the server replays on the first sync.
        self.start_ms = 0

        # Only one agent run at a time — a home-automation chat is naturally
        # serial, and it keeps the persistent session's turns from interleaving.
        self.agent_lock = asyncio.Lock()
        self.current_run: dict = {}
        # Set when the agent session is beyond repair: the process exits and the
        # container's restart policy starts a fresh one.
        self.fatal = asyncio.Event()
        # At most one pending confirmation at a time — agent runs are serial.
        self.pending_confirm: dict = {}
        # asyncio keeps only weak references to tasks: hold fire-and-forget ones
        # until they finish, or a run could be garbage-collected mid-flight.
        self.background_tasks: set[asyncio.Task] = set()
        self.whisper = None

        # Status bookkeeping (served on /status).
        self.run_history: deque = deque(maxlen=20)
        self.agent_state: dict = {"ok": True, "failure": None}
        self.last_sync = 0.0
        self.signal_connected = False
        self.briefing_next: str | None = None
        self.restart_next: str | None = None

        self.state = load_state()
        self.online_notice_due = should_announce_start(
            self.state, time.time(), cfg.announce_planned_restart
        )
        if self.state.pop("planned_restart", None):
            save_state(self.state)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.http = aiohttp.ClientSession()
        await self._connect_claude()
        await self._connect_matrix()
        self._register_matrix_handlers()

        signal_task = (
            asyncio.create_task(self._signal_loop()) if self.cfg.signal.enabled else None
        )
        webhook = await self._start_webhook()
        briefing_task = self._start_briefing()
        log.info(
            "Syncing. Matrix allowlist: %s | Signal: %s (lang=%s)",
            ", ".join(sorted(self.cfg.allowed_users)),
            ", ".join(sorted(self.cfg.signal.allowed)) if self.cfg.signal.enabled else "off",
            self.cfg.lang,
        )
        restart_task = self._start_restart_timer()

        sync_task = asyncio.create_task(
            self.matrix.sync_forever(timeout=30000, full_state=True)
        )
        fatal_task = asyncio.create_task(self.fatal.wait())
        try:
            await asyncio.wait(
                {t for t in (sync_task, restart_task, fatal_task) if t},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sync_task.done():
                sync_task.result()
        finally:
            for task in (briefing_task, signal_task, restart_task, fatal_task, sync_task):
                if task:
                    task.cancel()
            if webhook:
                await webhook.cleanup()
            await self.http.close()
            await self.claude.disconnect()
            await self.matrix.close()
        if self.fatal.is_set():
            sys.exit(1)

    def _agent_options(self) -> ClaudeAgentOptions:
        cfg = self.cfg
        if cfg.confirm_destructive:
            # Bash is deliberately NOT pre-approved: every Bash call goes through
            # can_use_tool, which auto-allows everything non-destructive.
            gate: dict = dict(
                allowed_tools=["Read", "Write", "Edit", "WebFetch", "WebSearch"],
                permission_mode="default",
                can_use_tool=self.can_use_tool,
            )
        else:
            # Original v1 behavior: no gate at all beyond the sender allowlist.
            gate = dict(
                allowed_tools=["Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"],
                permission_mode="bypassPermissions",
            )
        return ClaudeAgentOptions(
            system_prompt=Path(__file__).with_name("system_prompt.md").read_text(),
            # Relative file operations land in the persistent data volume.
            cwd=str(DATA_DIR),
            # Passed into the agent's tool environment so `curl` can auth to HA
            # without the token ever appearing in the prompt or the chat.
            env={
                "HA_BASE_URL": cfg.ha_base_url,
                "HA_TOKEN": cfg.ha_token,
                "OUTBOX": str(OUTBOX_DIR),
                **cfg.claude_auth,
            },
            model=cfg.model,
            **gate,
        )

    async def _connect_claude(self) -> None:
        # One persistent session for the life of the process.
        self.claude = ClaudeSDKClient(options=self._agent_options())
        await self.claude.connect()
        log.info(
            "Claude Agent SDK session connected (version=%s, confirm_destructive=%s, signal=%s).",
            BOT_VERSION,
            self.cfg.confirm_destructive,
            self.cfg.signal.enabled,
        )

    async def _start_webhook(self) -> "Webhook | None":
        if not self.cfg.webhook_port:
            return None
        if not self.cfg.webhook_token:
            log.warning("WEBHOOK_PORT set but WEBHOOK_TOKEN missing — webhook disabled.")
            return None
        webhook = Webhook(self)
        await webhook.start()
        return webhook

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    def _start_briefing(self) -> asyncio.Task | None:
        value = self.cfg.briefing_time
        if not value:
            return None
        clock = parse_clock(value)
        if not clock:
            log.error("Invalid BRIEFING_TIME %r (expected HH:MM)", value)
            return None
        return asyncio.create_task(self._briefing_loop(*clock))

    async def _briefing_loop(self, hour: int, minute: int) -> None:
        while True:
            now = datetime.now(self.cfg.tz)
            target_dt = next_occurrence(now, hour, minute)
            self.briefing_next = target_dt.isoformat(timespec="minutes")
            log.info("Next briefing at %s", target_dt.isoformat())
            await asyncio.sleep((target_dt - now).total_seconds())
            targets = self.notify_targets()
            if targets:
                await self.run_agent(
                    targets, BRIEFING_PROMPT[self.cfg.lang], announce=False
                )
            else:
                log.warning("Briefing skipped — no channel known yet.")

    # The process just exits; the container's restart policy brings it back.
    def _start_restart_timer(self) -> asyncio.Task | None:
        value = self.cfg.restart_time
        if value.lower() == "off":
            return None
        clock = parse_clock(value)
        if not clock:
            log.error("Invalid RESTART_TIME %r (expected HH:MM or off)", value)
            return None
        return asyncio.create_task(self._wait_for_restart(*clock))

    async def _wait_for_restart(self, hour: int, minute: int) -> None:
        now = datetime.now(self.cfg.tz)
        target_dt = next_occurrence(now, hour, minute)
        self.restart_next = target_dt.isoformat(timespec="minutes")
        log.info("Next scheduled restart at %s", target_dt.isoformat())
        await asyncio.sleep((target_dt - now).total_seconds())
        # Let a running agent turn finish; never released, so no new
        # one starts while we shut down.
        await self.agent_lock.acquire()
        # The next start reads this and skips its online message.
        self.state["planned_restart"] = True
        save_state(self.state)
        log.info("Scheduled restart — exiting.")

    # ── Confirmation plumbing (chat-gated destructive commands) ───────────────
    async def ask_confirmation(self, target: Target, cmd: str) -> bool:
        await self.deliver(target, self.S["confirm"].format(cmd=cmd))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_confirm["future"] = fut
        self.pending_confirm["target"] = target
        try:
            return await asyncio.wait_for(fut, CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.deliver(target, self.S["confirm_timeout"])
            return False
        finally:
            self.pending_confirm.clear()

    def resolve_confirmation(self, answer: bool) -> None:
        fut = self.pending_confirm.get("future")
        if fut and not fut.done():
            fut.set_result(answer)

    def maybe_answer_confirmation(self, body: str) -> str | None:
        """Returns 'handled'/'hint' if a confirmation was pending, else None."""
        if not self.pending_confirm.get("future"):
            return None
        lowered = body.strip().lower()
        if lowered in self.S["yes"]:
            self.resolve_confirmation(True)
            return "handled"
        if lowered in self.S["no"]:
            self.resolve_confirmation(False)
            return "handled"
        return "hint"

    async def can_use_tool(self, tool_name, tool_input, context):
        if self.current_run.get("aborted"):
            return PermissionResultDeny(message="This run timed out and was aborted.")
        if tool_name != "Bash":
            return PermissionResultAllow()
        cmd = (tool_input or {}).get("command", "")
        if not DESTRUCTIVE_RE.search(cmd):
            return PermissionResultAllow()
        target = self.current_run.get("targets", [None])[0] or self.default_matrix_target()
        if not target:
            return PermissionResultDeny(message="No channel available to confirm in.")
        log.info("Asking chat confirmation for command: %s", loggable(cmd, 300))
        if await self.ask_confirmation(target, cmd):
            return PermissionResultAllow()
        return PermissionResultDeny(message=self.S["denied"])

    # ── Matrix session ────────────────────────────────────────────────────────
    async def _connect_matrix(self) -> None:
        cfg = self.cfg
        os.makedirs(STORE_PATH, exist_ok=True)
        self.matrix = AsyncClient(
            cfg.homeserver,
            user=cfg.matrix_user,
            store_path=STORE_PATH,
            config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
        )
        # Reuse the stored session if it's still valid — a fresh password login
        # would register yet another device the owner has to verify in their client.
        if not await self._restore_matrix_session():
            await self._matrix_login()
        if self.matrix.should_upload_keys:
            await self.matrix.keys_upload()
        self.start_ms = int(time.time() * 1000)

    async def _restore_matrix_session(self) -> bool:
        session = load_session()
        if not session or session.get("user_id") != self.cfg.matrix_user:
            return False
        self.matrix.restore_login(**session)
        if isinstance(await self.matrix.whoami(), WhoamiResponse):
            log.info(
                "Restored Matrix session as %s (device %s).",
                self.matrix.user_id,
                self.matrix.device_id,
            )
            return True
        log.warning("Stored Matrix session rejected — logging in again.")
        self.matrix.access_token = ""
        return False

    async def _matrix_login(self) -> None:
        login = await self.matrix.login(
            self.cfg.matrix_password, device_name="ha-matrix-bot"
        )
        if not isinstance(login, LoginResponse):
            log.error("Matrix login failed: %s", login)
            await self.claude.disconnect()
            sys.exit(1)
        save_session(self.matrix)
        log.info(
            "Logged in to Matrix as %s (device %s).",
            self.matrix.user_id,
            self.matrix.device_id,
        )

    # ── Delivery ──────────────────────────────────────────────────────────────
    def default_matrix_target(self) -> Target | None:
        """Matrix room for unsolicited messages (webhook, briefing)."""
        if self.cfg.notify_room:
            return ("matrix", self.cfg.notify_room)
        rooms = self.matrix.rooms
        if self.state.get("last_room_id") in rooms:
            return ("matrix", self.state["last_room_id"])
        # Fall back to a room an allowlisted user is actually in (skips e.g.
        # the homeserver's server-notices room).
        for room_id, room in rooms.items():
            if self.cfg.allowed_users & set(room.users):
                return ("matrix", room_id)
        first = next(iter(rooms), None)
        return ("matrix", first) if first else None

    def notify_targets(self) -> list[Target]:
        targets: list[Target] = []
        matrix_target = self.default_matrix_target()
        if matrix_target:
            targets.append(matrix_target)
        if self.cfg.signal.enabled and self.cfg.signal.notify:
            targets.append(("signal", self.cfg.signal.notify))
        return targets

    async def matrix_send(self, room_id: str, text: str) -> None:
        for part in chunk(text):
            await self.matrix.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": part},
                ignore_unverified_devices=True,
            )

    async def signal_send(self, recipient: str, text: str) -> None:
        signal = self.cfg.signal
        for part in chunk(text):
            async with self.http.post(
                f"{signal.api}/v2/send",
                json={
                    "message": part,
                    "number": signal.number,
                    "recipients": [recipient],
                },
            ) as resp:
                if resp.status >= 400:
                    log.error("Signal send failed (%d): %s", resp.status, await resp.text())

    async def signal_send_file(self, recipient: str, path: Path) -> None:
        signal = self.cfg.signal
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        b64 = base64.b64encode(path.read_bytes()).decode()
        async with self.http.post(
            f"{signal.api}/v2/send",
            json={
                "message": "",
                "number": signal.number,
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

    async def deliver(self, target: Target, text: str) -> None:
        kind, dest = target
        if kind == "matrix":
            await self.matrix_send(dest, text)
        else:
            await self.signal_send(dest, text)

    # ── Outbox: files the agent wants delivered into the chat ─────────────────
    async def matrix_post_file(self, room_id: str, path: Path) -> None:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        room = self.matrix.rooms.get(room_id)
        encrypted = bool(room and room.encrypted)
        resp, keys = await self.matrix.upload(
            lambda _429, _timeouts: open(path, "rb"),
            content_type=mime,
            filename=path.name,
            encrypt=encrypted,
            filesize=size,
        )
        if not isinstance(resp, UploadResponse):
            log.error("Upload of %s failed: %s", loggable(path.name, 100), resp)
            return
        await self.matrix.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=matrix_file_content(
                path.name, mime, size, resp.content_uri, keys if encrypted else None
            ),
            ignore_unverified_devices=True,
        )
        log.info(
            "Posted %s (%s, %d bytes) to %s",
            loggable(path.name, 100),
            mime,
            size,
            loggable(room_id, 100),
        )

    async def _post_file(self, target: Target, path: Path) -> None:
        kind, dest = target
        try:
            if kind == "matrix":
                await self.matrix_post_file(dest, path)
            else:
                await self.signal_send_file(dest, path)
        except Exception:
            log.exception(
                "Failed to post outbox file %s to %s",
                loggable(path.name, 100),
                loggable(str(target), 120),
            )

    async def flush_outbox(self, targets: list[Target]) -> None:
        for path in sorted(OUTBOX_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            for target in targets:
                await self._post_file(target, path)
            try:
                path.unlink()
            except OSError:
                pass

    # ── Agent runs ────────────────────────────────────────────────────────────
    async def reconnect_session(self) -> bool:
        """Replace the agent session with a fresh one; the conversation is lost."""
        try:
            await self.claude.disconnect()
            await self.claude.connect()
        except Exception:
            log.exception("Reconnecting the agent session failed")
            return False
        log.warning("Agent session replaced with a fresh one.")
        return True

    async def _drain_turn(self) -> None:
        await self.claude.interrupt()
        async for _ in self.claude.receive_response():
            # Discard: we only need to reach the end of the turn.
            pass

    async def abort_turn(self) -> bool:
        """Stop a timed-out turn so it can't keep acting or bleed into the next.

        Returns False if the agent session could not be recovered."""
        # Cancelling our reader doesn't stop the turn itself: it would go on
        # running tools, and its leftover messages would be read as the reply
        # to the next prompt.
        self.current_run["aborted"] = True
        self.resolve_confirmation(False)
        try:
            await asyncio.wait_for(self._drain_turn(), ABORT_DRAIN_TIMEOUT_S)
        except Exception:
            # A fresh session loses the conversation so far, but a turn that
            # won't stop is worse.
            log.exception("Interrupt failed — reconnecting the agent session")
            return await self.reconnect_session()
        return True

    async def _attempt(self, prompt: str, turn: TurnState) -> tuple[str, bool] | None:
        """Run the prompt once; returns judge_turn's verdict."""
        turn.reset()
        await self.claude.query(prompt)
        async for message in self.claude.receive_response():
            turn.absorb(message)
        return judge_turn(turn.result, turn.api_errors)

    async def _attempt_with_retry(
        self, prompt: str, turn: TurnState, t0: float
    ) -> tuple[tuple[str, bool] | None, bool]:
        """Returns (verdict, whether the agent session is still usable)."""
        verdict = await asyncio.wait_for(self._attempt(prompt, turn), AGENT_TIMEOUT_S)
        if not (verdict and verdict[1]):
            return verdict, True
        log.error("Agent turn not usable (%s) — replacing the session", verdict[0])
        session_ok = await self.reconnect_session()
        # Nothing was executed, so the fresh session can redo it.
        # Both attempts share one budget; the lock is held meanwhile.
        left = AGENT_TIMEOUT_S - (time.time() - t0)
        if session_ok and not turn.used_tools and left > 30:
            verdict = await asyncio.wait_for(self._attempt(prompt, turn), left)
        return verdict, session_ok

    async def _report_unusable(
        self,
        targets: list[Target],
        turn: TurnState,
        verdict: tuple[str, bool],
        session_ok: bool,
    ) -> None:
        reason, reset = verdict
        detail = " ".join(turn.parts).strip()[:200]
        if detail:
            reason = f"{reason}: {detail}"
        log.error("Agent turn not usable: %s", loggable(reason, 300))
        self.agent_state.update(ok=False, failure=reason)
        text = unusable_turn_text(self.S, reset, turn.used_tools)
        await self.deliver(targets[0], text.format(reason=reason))
        await self.flush_outbox(targets)
        if not session_ok:
            self.fatal.set()

    async def _report_timeout(self, targets: list[Target]) -> None:
        self.agent_state.update(ok=False, failure="timed out")
        log.error("Agent run timed out after %ds — aborting", AGENT_TIMEOUT_S)
        session_ok = await self.abort_turn()
        await self.deliver(targets[0], self.S["agent_timeout"])
        # Otherwise the aborted run's files would go to the next run's chat.
        await self.flush_outbox(targets)
        if not session_ok:
            self.fatal.set()

    async def _execute_turn(
        self, targets: list[Target], prompt: str, turn: TurnState, t0: float
    ) -> bool:
        """Run one turn; True if its reply can go out. Failures are reported here."""
        try:
            verdict, session_ok = await self._attempt_with_retry(prompt, turn, t0)
            if verdict:
                await self._report_unusable(targets, turn, verdict, session_ok)
                return False
            self.agent_state.update(ok=True, failure=None)
            return True
        except asyncio.TimeoutError:
            await self._report_timeout(targets)
            return False
        except Exception:
            self.agent_state.update(ok=False, failure="failed with an exception, see the log")
            log.exception("Agent run failed")
            await self.deliver(targets[0], self.S["error"])
            return False

    def _record_run(self, targets: list[Target], prompt: str, ok: bool, t0: float) -> None:
        self.current_run.clear()
        self.run_history.appendleft(
            {
                "time": datetime.now(self.cfg.tz).isoformat(timespec="seconds"),
                "via": targets[0][0],
                "ok": ok,
                "duration_s": round(time.time() - t0, 1),
                "prompt": prompt[:80],
            }
        )

    async def run_agent(
        self, targets: list[Target], prompt: str, announce: bool = True, on_start=None
    ) -> None:
        async with self.agent_lock:
            if on_start:
                on_start()  # no longer waiting for the lock
            self.current_run["targets"] = targets
            if announce:
                await self.deliver(targets[0], self.S["thinking"])
            turn = TurnState()
            t0 = time.time()
            ok = False
            try:
                ok = await self._execute_turn(targets, prompt, turn, t0)
            finally:
                self._record_run(targets, prompt, ok, t0)
            if not ok:
                return
            reply = "\n\n".join(turn.parts).strip() or self.S["no_text"]
            for target in targets:
                await self.deliver(target, reply)
            await self.flush_outbox(targets)

    # ── Voice messages ────────────────────────────────────────────────────────
    def transcribe_sync(self, path: str) -> str:
        name = self.cfg.whisper_model
        if self.whisper is None:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model %r (first use)…", name)
            self.whisper = WhisperModel(name, device="cpu", compute_type="int8")
        segments, _info = self.whisper.transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe_and_run(self, target: Target, data: bytes, suffix: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            text = await asyncio.get_running_loop().run_in_executor(
                None, self.transcribe_sync, tmp_path
            )
        except Exception:
            log.exception("Transcription failed")
            await self.deliver(target, self.S["voice_fail"])
            return
        finally:
            os.unlink(tmp_path)
        if not text:
            await self.deliver(target, self.S["voice_fail"])
            return
        await self.deliver(target, self.S["voice_heard"].format(text=text))
        await self.run_agent([target], text, announce=False)

    async def handle_matrix_voice(self, room: MatrixRoom, event) -> None:
        resp = await self.matrix.download(event.url)
        data = getattr(resp, "body", None)
        if not data:
            log.error("Voice download failed: %s", resp)
            await self.matrix_send(room.room_id, self.S["voice_fail"])
            return
        if isinstance(event, RoomEncryptedAudio):
            data = decrypt_attachment(
                data, event.key["k"], event.hashes["sha256"], event.iv
            )
        suffix = Path(event.body or "voice.ogg").suffix or ".ogg"
        await self.transcribe_and_run(("matrix", room.room_id), data, suffix)

    # ── Matrix event handlers ─────────────────────────────────────────────────
    def _register_matrix_handlers(self) -> None:
        self.matrix.add_event_callback(self.on_message, RoomMessageText)
        self.matrix.add_event_callback(
            self.on_audio, (RoomMessageAudio, RoomEncryptedAudio)
        )
        self.matrix.add_event_callback(self.on_unknown, UnknownEvent)
        self.matrix.add_event_callback(self.on_invite, InviteMemberEvent)
        self.matrix.add_response_callback(self.on_sync, SyncResponse)

    def is_relevant(self, event) -> bool:
        if event.sender == self.matrix.user_id:
            return False  # our own messages
        if event.server_timestamp < self.start_ms:
            return False  # replayed history
        if event.sender not in self.cfg.allowed_users:
            log.warning(
                "Ignoring event from non-allowlisted sender %s",
                loggable(event.sender, 80),
            )
            return False
        return True

    def remember_room(self, room_id: str) -> None:
        if self.state.get("last_room_id") != room_id:
            self.state["last_room_id"] = room_id
            save_state(self.state)

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if not self.is_relevant(event):
            return
        self.remember_room(room.room_id)
        body = event.body.strip()
        # A pending destructive-command confirmation swallows the next reply.
        outcome = self.maybe_answer_confirmation(body)
        if outcome == "hint":
            await self.matrix_send(room.room_id, self.S["confirm_hint"])
        if outcome:
            return
        log.info(
            "Message from %s in %s: %s",
            loggable(event.sender, 80),
            loggable(room.room_id, 100),
            loggable(body),
        )
        self.spawn(self.run_agent([("matrix", room.room_id)], body))

    async def on_audio(self, room: MatrixRoom, event) -> None:
        if not self.is_relevant(event) or not self.cfg.voice_enabled:
            return
        self.remember_room(room.room_id)
        log.info(
            "Voice message from %s in %s",
            loggable(event.sender, 80),
            loggable(room.room_id, 100),
        )
        self.spawn(self.handle_matrix_voice(room, event))

    async def on_unknown(self, room: MatrixRoom, event: UnknownEvent) -> None:
        # Reactions (👍/👎) can answer a pending confirmation.
        if event.type != "m.reaction" or not self.pending_confirm.get("future"):
            return
        if event.sender not in self.cfg.allowed_users:
            return
        key = event.source.get("content", {}).get("m.relates_to", {}).get("key", "")
        if key.startswith("👍"):
            self.resolve_confirmation(True)
        elif key.startswith("👎"):
            self.resolve_confirmation(False)

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.sender not in self.cfg.allowed_users:
            log.warning("Ignoring invite from %s", loggable(event.sender, 80))
            return
        await self.matrix.join(room.room_id)
        log.info(
            "Joined room %s (invited by %s)",
            loggable(room.room_id, 100),
            loggable(event.sender, 80),
        )

    def _retry_online_notice(self) -> None:
        # Try again on the next syncs for two minutes; after that a
        # "back online" would no longer be news.
        self.online_notice_due = time.time() - self.started_at < 120

    async def announce_online(self) -> None:
        targets = self.notify_targets()
        if not targets:
            self._retry_online_notice()
            return
        sent = False
        for target in targets:
            try:
                await self.deliver(target, self.S["online"].format(version=BOT_VERSION))
                sent = True
            except Exception:
                log.exception("Could not send the online message to %s", loggable(str(target), 120))
        if sent:
            self.state["online_notice_ts"] = time.time()
            save_state(self.state)
        else:
            self._retry_online_notice()

    async def on_sync(self, _response: SyncResponse) -> None:
        self.last_sync = time.time()
        # After the first sync the rooms are known and messages can go out.
        if self.online_notice_due:
            self.online_notice_due = False
            self.spawn(self.announce_online())

    # ── Signal receive loop ───────────────────────────────────────────────────
    def _signal_sender(self, data: dict) -> tuple[dict, str] | None:
        """(data message, sender) of an envelope worth handling, else None."""
        envelope = data.get("envelope") or {}
        dm = envelope.get("dataMessage") or {}
        sender = envelope.get("sourceNumber") or envelope.get("source") or ""
        if not dm or sender == self.cfg.signal.number:
            return None
        if sender not in self.cfg.signal.allowed:
            if sender:
                log.warning(
                    "Ignoring Signal message from non-allowlisted %s",
                    loggable(sender, 40),
                )
            return None
        return dm, sender

    async def _signal_voice(self, target: Target, sender: str, voice_att: dict) -> None:
        att_id = voice_att.get("id")
        log.info("Signal voice message from %s", loggable(sender, 40))
        async with self.http.get(
            f"{self.cfg.signal.api}/v1/attachments/{quote(att_id)}"
        ) as resp:
            if resp.status >= 400:
                log.error("Signal attachment download failed (%d)", resp.status)
                await self.signal_send(target[1], self.S["voice_fail"])
                return
            audio = await resp.read()
        suffix = (
            mimetypes.guess_extension(voice_att.get("contentType") or "") or ".ogg"
        )
        self.spawn(self.transcribe_and_run(target, audio, suffix))

    async def handle_signal_envelope(self, data: dict) -> None:
        parsed = self._signal_sender(data)
        if not parsed:
            return
        dm, sender = parsed
        group_id = (dm.get("groupInfo") or {}).get("groupId")
        recipient = f"group.{group_id}" if group_id else sender
        target: Target = ("signal", recipient)
        text = (dm.get("message") or "").strip()

        outcome = self.maybe_answer_confirmation(text) if text else None
        if outcome == "hint":
            await self.signal_send(recipient, self.S["confirm_hint"])
        if outcome:
            return

        voice_att = signal_voice_attachment(dm)
        if voice_att and self.cfg.voice_enabled:
            await self._signal_voice(target, sender, voice_att)
            return

        if text:
            log.info(
                "Signal message from %s: %s", loggable(sender, 40), loggable(text)
            )
            self.spawn(self.run_agent([target], text))

    async def _signal_text(self, raw: str) -> None:
        try:
            await self.handle_signal_envelope(json.loads(raw))
        except Exception:
            log.exception("Error handling Signal envelope")

    async def _signal_session(self, url: str) -> None:
        async with self.http.ws_connect(url, heartbeat=30) as ws:
            log.info("Signal websocket connected (%s).", self.cfg.signal.number)
            self.signal_connected = True
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._signal_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

    async def _signal_loop(self) -> None:
        signal = self.cfg.signal
        ws_base = signal.api.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/v1/receive/{quote(signal.number)}"
        while True:
            try:
                await self._signal_session(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Signal connection lost (%s) — retrying in 10s", exc)
            finally:
                self.signal_connected = False
            await asyncio.sleep(10)


# ── Webhook: proactive notifications from Home Assistant, plus /status ───────


class Webhook:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.token = bot.cfg.webhook_token
        self.smart_budget = SmartBudget(bot.cfg.notify_smart_limit)
        self.smart_waiting = 0
        # Bad tokens are worth seeing, but not once per request: the log ring
        # buffer is small and shown on the status page.
        self.bad_token = {"logged_at": 0.0, "suppressed": 0}
        self.runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/notify", self.handle_notify)
        app.router.add_get("/status", self.handle_status)
        app.router.add_get("/healthz", self.handle_health)
        return app

    async def start(self) -> None:
        port = self.bot.cfg.webhook_port
        if len(self.token) < MIN_WEBHOOK_TOKEN_CHARS:
            log.warning(
                "WEBHOOK_TOKEN has only %d characters — use at least %d random ones.",
                len(self.token),
                MIN_WEBHOOK_TOKEN_CHARS,
            )
        # No access log: the status page polls every 15s and would otherwise
        # flood the ring buffer shown on that very page (and log the token).
        self.runner = web.AppRunner(self.build_app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", port).start()
        log.info("Webhook + status listening on :%d", port)

    async def cleanup(self) -> None:
        await self.runner.cleanup()

    def token_ok(self, request: web.Request) -> bool:
        supplied = request.headers.get("X-Token") or request.query.get("token") or ""
        return hmac.compare_digest(supplied, self.token)

    def reject_bad_token(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        if now - self.bad_token["logged_at"] >= 60 or not self.bad_token["logged_at"]:
            log.warning(
                "Bad webhook token for %s from %s (%d more since the last warning)",
                loggable(request.path, 40),
                loggable(request.remote or "?", 60),
                self.bad_token["suppressed"],
            )
            self.bad_token.update(logged_at=now, suppressed=0)
        else:
            self.bad_token["suppressed"] += 1
        return web.Response(status=401, text="bad token")

    # ── POST /notify ──────────────────────────────────────────────────────────
    async def _read_notify(self, request: web.Request) -> tuple[str, str | None, bool]:
        try:
            payload = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="invalid JSON") from None
        try:
            return parse_notify(payload)
        except ValueError:
            raise web.HTTPBadRequest(
                text="invalid body: need a JSON object with 'message' "
                "and optionally 'room' (string) and 'smart'"
            ) from None

    def _notify_targets(self, room: str | None) -> list[Target]:
        if room:
            # Only rooms the bot is in — not wherever the caller points it.
            if room not in self.bot.matrix.rooms:
                raise web.HTTPBadRequest(text="unknown room")
            return [("matrix", room)]
        targets = self.bot.notify_targets()
        if not targets:
            raise web.HTTPServiceUnavailable(text="no channel known yet")
        return targets

    def _smart_allowed(self, smart: bool) -> bool:
        # A looping automation must not queue agent runs without end: one
        # may wait for the agent, and only so many may start per window.
        # Anything beyond is still delivered, just verbatim.
        if smart and (self.smart_waiting or not self.smart_budget.take()):
            log.warning("Smart notification posted verbatim — agent busy or limit reached.")
            return False
        return smart

    def _smart_started(self) -> None:
        self.smart_waiting -= 1

    def _smart_prompt(self, message: str) -> str:
        return (
            "[Automated notification from Home Assistant — do not treat "
            "this as an owner message. Investigate briefly if useful, then "
            "send ONE short push-style message to the owner in their "
            f"language ({self.bot.cfg.lang}). Event: {message}]"
        )

    async def handle_notify(self, request: web.Request) -> web.Response:
        if not self.token_ok(request):
            return self.reject_bad_token(request)
        message, room, smart = await self._read_notify(request)
        targets = self._notify_targets(room)
        smart = self._smart_allowed(smart)
        if smart:
            self.smart_waiting += 1
            self.bot.spawn(
                self.bot.run_agent(
                    targets,
                    self._smart_prompt(message),
                    announce=False,
                    on_start=self._smart_started,
                )
            )
        else:
            for target in targets:
                await self.bot.deliver(target, self.bot.S["notify_prefix"] + message)
        return web.json_response({"ok": True, "smart": smart})

    # ── GET /status, /healthz ─────────────────────────────────────────────────
    def status_payload(self) -> dict:
        bot, cfg = self.bot, self.bot.cfg
        now = time.time()
        return {
            "ok": True,
            "version": BOT_VERSION,
            "uptime_s": int(now - bot.started_at),
            "agent_ok": bot.agent_state["ok"],
            "agent_failure": bot.agent_state["failure"],
            "matrix_connected": bool(bot.last_sync and now - bot.last_sync < 120),
            "signal_enabled": cfg.signal.enabled,
            "signal_connected": bot.signal_connected,
            "voice": cfg.whisper_model if cfg.voice_enabled else "off",
            "confirm_destructive": cfg.confirm_destructive,
            "next_briefing": bot.briefing_next,
            "next_restart": bot.restart_next,
            "runs_total": len(bot.run_history),
            "last_run": bot.run_history[0] if bot.run_history else None,
            "lang": cfg.lang,
        }

    async def handle_status(self, request: web.Request) -> web.Response:
        if not self.token_ok(request):
            return self.reject_bad_token(request)
        payload = self.status_payload()
        wants_html = "text/html" in request.headers.get("Accept", "")
        if request.query.get("format") == "json" or not wants_html:
            return web.json_response(payload)
        page = render_status_page(payload, self.bot.run_history)
        return web.Response(text=page, content_type="text/html")

    async def handle_health(self, _request: web.Request) -> web.Response:
        # Unauthenticated on purpose (container/k8s probes), so it must not
        # leak anything: liveness of the sync loop, nothing else.
        last = self.bot.last_sync
        alive = bool(last and time.time() - last < 300)
        return web.json_response(
            {"status": "ok" if alive else "degraded"},
            status=200 if alive else 503,
        )


async def main() -> None:
    cfg = load_config()
    # Everything is read; the agent session started below must not see these.
    hide_secrets_from_agent("MATRIX_PASSWORD", "WEBHOOK_TOKEN")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    await Bot(cfg).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
