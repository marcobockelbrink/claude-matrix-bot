"""Stand-ins for the agent session and the chat, so Bot logic can run without I/O."""

import asyncio
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

import bot


def make_config(**overrides) -> bot.Config:
    fields = dict(
        claude_auth={},
        ha_base_url="http://ha.local:8123",
        ha_token="ha-token",
        homeserver="https://matrix.example",
        matrix_user="@bot:example",
        matrix_password="pw",
        allowed_users=frozenset({"@owner:example"}),
        model=None,
        lang="en",
        webhook_port=0,
        webhook_token="t" * 32,
        notify_smart_limit=6,
        briefing_time="",
        restart_time="off",
        announce_planned_restart=False,
        tz=ZoneInfo("Europe/Berlin"),
        whisper_model="off",
        confirm_destructive=True,
        notify_room="",
        signal=bot.SignalConfig(),
    )
    fields.update(overrides)
    return bot.Config(**fields)


def assistant(text=None, tool=False, error=None) -> AssistantMessage:
    content = []
    if text:
        content.append(TextBlock(text=text))
    if tool:
        content.append(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}))
    return AssistantMessage(content=content, model="m", error=error)


def finished(subtype="success", is_error=False, api_error_status=None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        api_error_status=api_error_status,
    )


class FakeClaude:
    """Plays back one scripted turn per query().

    A turn is a list of messages, an Exception to raise from query(), or
    HANG to never finish (until interrupt()).
    """

    HANG = "hang"

    def __init__(self, *turns, connect_fails_after=None):
        self.turns = list(turns)
        self.current: object = []
        self.prompts: list[str] = []
        self.connects = 0
        self.disconnects = 0
        self.interrupted = False
        self.connect_fails_after = connect_fails_after

    async def connect(self):
        self.connects += 1
        if self.connect_fails_after is not None and self.connects > self.connect_fails_after:
            raise RuntimeError("cannot connect")

    async def disconnect(self):
        self.disconnects += 1

    async def query(self, prompt):
        self.prompts.append(prompt)
        self.current = self.turns.pop(0)
        if isinstance(self.current, Exception):
            raise self.current

    async def interrupt(self):
        self.interrupted = True
        self.current = []

    async def receive_response(self):
        if self.current == self.HANG:
            await asyncio.sleep(3600)
        for message in self.current:
            yield message


def make_bot(claude=None, **config_overrides) -> bot.Bot:
    """A Bot with no network: delivery and the outbox are mocks."""
    with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
        b = bot.Bot(make_config(**config_overrides))
    b.claude = claude
    b.deliver = mock.AsyncMock()
    b.flush_outbox = mock.AsyncMock()
    b.matrix = SimpleNamespace(rooms={})
    return b
