"""The Mistral agent backend (tool-calling loop) and the runtime backend switch."""

import asyncio
import json
import tempfile
import unittest

import aiohttp
from pathlib import Path
from unittest import mock

from claude_agent_sdk import AssistantMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, ToolUseBlock

import bot
from fakes import make_bot, make_config


class FakeResp:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data
        self._text = json.dumps(data)

    async def json(self):
        return self._data

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeHttp:
    """Plays back (status, payload) pairs per POST."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResp(*self.responses.pop(0))


def completion(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return (200, {"choices": [{"message": message, "finish_reason": "stop"}]})


def tool_call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def make_backend(http, gate, **kwargs) -> bot.MistralBackend:
    tmp = kwargs.pop("tmp", None) or Path(tempfile.mkdtemp())
    (tmp / "outbox").mkdir(exist_ok=True)
    return bot.MistralBackend(
        api_key="k",
        model="mistral-medium-latest",
        gate=gate,
        system_prompt="sys prompt",
        base_env={"HA_TOKEN": "ha"},
        cwd=tmp,
        outbox=tmp / "outbox",
        http=http,
        **kwargs,
    )


async def collect(backend, prompt):
    out = []
    async for message in backend.query_stream(prompt):
        out.append(message)
    return out


class MistralLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_answer(self):
        http = FakeHttp(completion(content="Hallo Welt"))
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        messages = await collect(b, "hi")
        self.assertIsInstance(messages[-1], ResultMessage)
        self.assertEqual(messages[-1].subtype, "success")
        texts = [blk.text for m in messages if isinstance(m, AssistantMessage) for blk in m.content]
        self.assertEqual(texts, ["Hallo Welt"])
        # The request carries the system prompt, the user message and the tools.
        sent = http.calls[0]["json"]
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(sent["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(sent["tools"], bot.MISTRAL_TOOL_SPECS)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer k")
        # History keeps the assistant answer for the next turn.
        self.assertEqual(b.history[-1], {"role": "assistant", "content": "Hallo Welt"})

    async def test_tool_round_trip_and_gate(self):
        http = FakeHttp(
            completion(tool_calls=[tool_call("Bash", {"command": "echo hello-out"})]),
            completion(content="erledigt"),
        )
        gate = mock.AsyncMock(return_value=PermissionResultAllow())
        b = make_backend(http, gate)
        await b.connect()
        messages = await collect(b, "mach")
        gate.assert_awaited_once_with("Bash", {"command": "echo hello-out"}, None)
        tool_msgs = [m for m in b.history if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0]["name"], "Bash")
        self.assertIn("hello-out", tool_msgs[0]["content"])
        blocks = [blk for m in messages if isinstance(m, AssistantMessage) for blk in m.content]
        self.assertTrue(any(isinstance(blk, ToolUseBlock) for blk in blocks))
        self.assertEqual(messages[-1].subtype, "success")
        # The tool env carries the HA token for the subprocess.
        self.assertEqual(b.base_env["HA_TOKEN"], "ha")

    async def test_denied_command_is_reported_to_the_model_not_run(self):
        http = FakeHttp(
            completion(tool_calls=[tool_call("Bash", {"command": "rm -rf /"})]),
            completion(content="ok, nicht gemacht"),
        )
        gate = mock.AsyncMock(return_value=PermissionResultDeny(message="nein"))
        b = make_backend(http, gate)
        await b.connect()
        await collect(b, "weg damit")
        tool_msgs = [m for m in b.history if m["role"] == "tool"]
        self.assertIn("not allowed", tool_msgs[0]["content"])

    async def test_api_error_maps_to_sdk_error_vocabulary(self):
        http = FakeHttp((401, {"error": "bad key"}))
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        messages = await collect(b, "hi")
        errors = [m.error for m in messages if isinstance(m, AssistantMessage)]
        self.assertEqual(errors, ["authentication_failed"])
        self.assertEqual(messages[-1].subtype, "error_during_execution")
        # judge_turn: no session reset for auth errors.
        verdict = bot.judge_turn(messages[-1], errors)
        self.assertEqual(verdict, ("API error: authentication_failed", False))
        # And it was not retried (401 is not retryable).
        self.assertEqual(len(http.calls), 1)

    async def test_rate_limit_is_retried_then_succeeds(self):
        http = FakeHttp((429, {"error": "slow down"}), (429, {"error": "slow down"}), completion(content="da"))
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            messages = await collect(b, "hi")
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(messages[-1].subtype, "success")

    async def test_tool_round_limit_yields_max_turns(self):
        http = FakeHttp(*[completion(tool_calls=[tool_call("Read", {"path": "memory.md"})])] * 60)
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        b.MAX_TOOL_ROUNDS = 5
        await b.connect()
        messages = await collect(b, "loop")
        self.assertEqual(messages[-1].subtype, "error_max_turns")
        # judge_turn treats it as a usable partial answer.
        self.assertIsNone(bot.judge_turn(messages[-1], []))

    async def test_interrupt_cancels_the_request(self):
        gate = asyncio.Event()

        class HangingResp(FakeResp):
            async def __aenter__(self):
                await gate.wait()  # hangs until the request task is cancelled
                return self

        class HangingHttp(FakeHttp):
            def post(self, url, headers=None, json=None, timeout=None):
                self.calls.append({"url": url, "json": json})
                return HangingResp(200, {})

        http = HangingHttp()
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        task = asyncio.create_task(collect(b, "hi"))
        await asyncio.sleep(0)  # let the request start
        await b.interrupt()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(b._aborted)

    async def test_connect_clears_history(self):
        b = make_backend(FakeHttp(completion(content="x")), mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        await collect(b, "hi")
        self.assertEqual(len(b.history), 2)
        await b.disconnect()  # injected http: must NOT be closed
        await b.connect()
        self.assertEqual(b.history, [])

    async def test_cancel_during_bash_kills_the_child_and_rolls_back_history(self):
        http = FakeHttp(completion(tool_calls=[tool_call("Bash", {"command": "sleep 30"})]))
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        task = asyncio.create_task(collect(b, "hi"))
        await asyncio.sleep(0.3)  # let the tool call start
        self.assertTrue(any("sleep 30" in line for line in _ps()))
        await b.interrupt()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        self.assertFalse(any("sleep 30" in line for line in _ps()))
        # No dangling assistant tool_calls left in the history.
        self.assertEqual(b.history, [{"role": "user", "content": "hi"}])

    async def test_api_error_mid_tool_loop_rolls_back_history(self):
        http = FakeHttp(
            completion(tool_calls=[tool_call("Bash", {"command": "echo x"})]),
            (500, {"error": "boom"}), (500, {"error": "boom"}), (500, {"error": "boom"}),
        )
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            messages = await collect(b, "hi")
        self.assertEqual(messages[-1].subtype, "error_during_execution")
        # The answered tool round stays (call + response are consistent);
        # nothing dangling remains for the next turn.
        self.assertEqual([m["role"] for m in b.history], ["user", "assistant", "tool"])
        self.assertIn("tool_calls", b.history[1])

    async def test_client_error_maps_to_resettable_invalid_request(self):
        http = FakeHttp((400, {"error": "bad request"}))
        b = make_backend(http, mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        messages = await collect(b, "hi")
        errors = [m.error for m in messages if isinstance(m, AssistantMessage)]
        self.assertEqual(errors, ["invalid_request"])
        # invalid_request is NOT in the cannot-fix set → judge wants a fresh session
        verdict = bot.judge_turn(messages[-1], errors)
        self.assertEqual(verdict[1], True)


    async def test_bash_env_strips_claude_credentials(self):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured.update(kwargs)
            proc = mock.Mock()
            proc.communicate = mock.AsyncMock(return_value=(b"ok", None))
            proc.kill = mock.Mock()
            return proc

        with mock.patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "sekret", "PATH": "/usr/bin"}, clear=False):
            with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                b = make_backend(FakeHttp(), mock.AsyncMock(return_value=PermissionResultAllow()))
                b._abort_event = asyncio.Event()
                out = await b._bash("echo hi")
        self.assertEqual(out.strip(), "ok")
        env = captured["env"]
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["HA_TOKEN"], "ha")  # base_env still applied

    async def test_network_error_is_retried_as_server_error(self):
        class DownHttp(FakeHttp):
            def post(self, url, headers=None, json=None, timeout=None):
                self.calls.append({"url": url, "json": json})
                raise aiohttp.ClientConnectionError("down")

        b = make_backend(DownHttp(), mock.AsyncMock(return_value=PermissionResultAllow()))
        await b.connect()
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            messages = await collect(b, "hi")
        self.assertEqual(len(b._http.calls), 3)  # retried
        errors = [m.error for m in messages if isinstance(m, AssistantMessage)]
        self.assertEqual(errors, ["server_error"])


class TrimHistoryTest(unittest.TestCase):
    def test_never_leaves_a_dangling_tool_message_at_the_head(self):
        b = make_backend(FakeHttp(), mock.AsyncMock())
        b.MAX_HISTORY_MESSAGES = 2
        b.history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "out", "tool_call_id": "1"},
            {"role": "assistant", "content": "done"},
        ]
        b._trim_history()
        self.assertEqual(b.history[0]["role"], "assistant")
        self.assertNotIn("tool_calls", b.history[0])


def _ps():
    import subprocess
    return subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout.splitlines()


class ToolJailTest(unittest.TestCase):
    def test_paths_outside_data_dir_and_outbox_are_rejected(self):
        b = make_backend(FakeHttp(), mock.AsyncMock())
        self.assertIn("outside", b._write("../escape.txt", "x"))
        self.assertIn("outside", b._read("/etc/passwd"))
        self.assertIn("wrote", b._write("memory.md", "hallo"))
        self.assertEqual(b._read("memory.md"), "hallo")
        self.assertIn("wrote", b._write("outbox/snap.jpg", "b"))
        self.assertIn("edited", b._edit("memory.md", "hallo", "tschüss"))
        self.assertIn("not found", b._edit("memory.md", "nope", "x"))


class BackendSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_overrides_the_env_default(self):
        with mock.patch("bot.load_state", return_value={"agent_backend": "mistral", "mistral_model": "devstral-small-latest"}), mock.patch("bot.save_state"):
            b = bot.Bot(make_config())
        self.assertEqual((b.backend_name, b.backend_model), ("mistral", "devstral-small-latest"))

    async def test_unknown_state_value_falls_back_to_config(self):
        with mock.patch("bot.load_state", return_value={"agent_backend": "nope"}), mock.patch("bot.save_state"):
            b = bot.Bot(make_config())
        self.assertEqual(b.backend_name, "claude")

    async def test_switch_requires_a_configured_key(self):
        b2 = make_bot(mistral_api_key="")
        with self.assertRaises(ValueError):
            await b2.switch_backend("mistral")

    async def test_switch_replaces_backend_and_persists_choice(self):
        b = make_bot()
        old = mock.Mock()
        old.disconnect = mock.AsyncMock()
        b.backend = old
        new = mock.Mock()
        new.connect = mock.AsyncMock()
        new.disconnect = mock.AsyncMock()
        b._make_backend = mock.Mock(return_value=new)
        with mock.patch("bot.save_state") as save:
            await b.switch_backend("mistral", "devstral-small-latest")
        new.connect.assert_awaited_once()
        old.disconnect.assert_awaited_once()
        self.assertIs(b.backend, new)
        self.assertEqual(b.backend_name, "mistral")
        self.assertEqual(b.state["agent_backend"], "mistral")
        self.assertEqual(b.state["mistral_model"], "devstral-small-latest")
        save.assert_called_once()

    async def test_switch_to_claude_needs_credentials(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            b = make_bot(claude_auth={}, mistral_api_key="m")
            with self.assertRaises(ValueError):
                await b.switch_backend("claude")

    async def test_unknown_backend_rejected(self):
        b = make_bot()
        with self.assertRaises(ValueError):
            await b.switch_backend("gemini")
