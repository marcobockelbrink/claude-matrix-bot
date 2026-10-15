"""One agent run end to end: what the owner is told, and what the bot remembers."""

import asyncio
import unittest
from unittest import mock

import bot
from fakes import FakeClaude, assistant, finished, make_bot

TARGET = ("matrix", "!room:example")
S = bot.STRINGS["en"]


def delivered(b) -> list[str]:
    return [call.args[1] for call in b.deliver.await_args_list]


class RunAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_goes_to_every_target_and_the_outbox_is_flushed(self):
        claude = FakeClaude([assistant("Licht ist an"), finished()])
        b = make_bot(claude)
        targets = [TARGET, ("signal", "+491234")]
        await b.run_agent(targets, "Licht an", announce=False)
        self.assertEqual(
            [(c.args[0], c.args[1]) for c in b.deliver.await_args_list],
            [(TARGET, "Licht ist an"), (("signal", "+491234"), "Licht ist an")],
        )
        b.flush_outbox.assert_awaited_once_with(targets)
        self.assertEqual(b.agent_state, {"ok": True, "failure": None})

    async def test_announces_thinking_first_unless_told_not_to(self):
        b = make_bot(FakeClaude([assistant("x"), finished()]))
        await b.run_agent([TARGET], "hi")
        self.assertEqual(delivered(b), [S["thinking"], "x"])

    async def test_text_parts_are_joined_and_empty_reply_gets_a_placeholder(self):
        b = make_bot(FakeClaude([assistant("eins"), assistant("zwei"), finished()]))
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertEqual(delivered(b), ["eins\n\nzwei"])

        b = make_bot(FakeClaude([finished()]))
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertEqual(delivered(b), [S["no_text"]])

    async def test_history_records_the_run_and_clears_the_current_run(self):
        b = make_bot(FakeClaude([assistant("x"), finished()]))
        await b.run_agent([TARGET], "p" * 200, announce=False)
        (entry,) = b.run_history
        self.assertEqual((entry["via"], entry["ok"], len(entry["prompt"])), ("matrix", True, 80))
        self.assertEqual(b.current_run, {})

    async def test_on_start_runs_once_the_lock_is_held(self):
        b = make_bot(FakeClaude([finished()]))
        seen = []
        await b.run_agent(
            [TARGET], "hi", announce=False, on_start=lambda: seen.append(b.agent_lock.locked())
        )
        self.assertEqual(seen, [True])

    async def test_concurrent_runs_are_serial(self):
        b = make_bot(FakeClaude([assistant("a"), finished()], [assistant("b"), finished()]))
        await asyncio.gather(
            b.run_agent([TARGET], "1", announce=False),
            b.run_agent([TARGET], "2", announce=False),
        )
        self.assertEqual(delivered(b), ["a", "b"])


class FailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_api_error_the_session_cannot_fix_is_reported_without_a_reset(self):
        claude = FakeClaude([assistant("x", error="rate_limit"), finished()])
        b = make_bot(claude)
        await b.run_agent([TARGET], "hi", announce=False)
        (message,) = delivered(b)
        self.assertIn("rate_limit", message)
        self.assertEqual(claude.connects, 0)  # no new session
        self.assertFalse(b.agent_state["ok"])
        self.assertFalse(b.run_history[0]["ok"])
        self.assertFalse(b.fatal.is_set())

    async def test_broken_session_is_replaced_and_the_request_retried(self):
        claude = FakeClaude(
            [finished(subtype="error_during_execution", is_error=True)],
            [assistant("jetzt klappt es"), finished()],
        )
        b = make_bot(claude)
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertEqual(claude.prompts, ["hi", "hi"])
        self.assertEqual((claude.disconnects, claude.connects), (1, 1))
        self.assertEqual(delivered(b), ["jetzt klappt es"])
        self.assertTrue(b.agent_state["ok"])
        self.assertTrue(b.run_history[0]["ok"])

    async def test_no_retry_once_tools_ran_and_the_owner_is_warned(self):
        claude = FakeClaude(
            [assistant(tool=True), finished(subtype="error_during_execution", is_error=True)]
        )
        b = make_bot(claude)
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertEqual(claude.prompts, ["hi"])
        (message,) = delivered(b)
        self.assertIn("part of the request may already have been carried out", message)
        self.assertFalse(b.run_history[0]["ok"])

    async def test_session_that_cannot_be_replaced_ends_the_process(self):
        claude = FakeClaude(
            [finished(subtype="error_during_execution", is_error=True)],
            connect_fails_after=0,
        )
        b = make_bot(claude)
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertTrue(b.fatal.is_set())
        self.assertEqual(len(delivered(b)), 1)
        b.flush_outbox.assert_awaited_once()

    async def test_exception_is_reported_and_recorded(self):
        b = make_bot(FakeClaude(RuntimeError("boom")))
        await b.run_agent([TARGET], "hi", announce=False)
        self.assertEqual(delivered(b), [S["error"]])
        self.assertEqual(b.agent_state["failure"], "failed with an exception, see the log")
        self.assertFalse(b.run_history[0]["ok"])
        self.assertEqual(b.current_run, {})

    async def test_timeout_aborts_the_turn_and_tells_the_owner(self):
        claude = FakeClaude(FakeClaude.HANG)
        b = make_bot(claude)
        with mock.patch.object(bot, "AGENT_TIMEOUT_S", 0.05):
            await b.run_agent([TARGET], "hi", announce=False)
        self.assertTrue(claude.interrupted)
        self.assertEqual(delivered(b), [S["agent_timeout"]])
        self.assertEqual(b.agent_state["failure"], "timed out")
        self.assertFalse(b.run_history[0]["ok"])
        b.flush_outbox.assert_awaited_once()
        self.assertFalse(b.fatal.is_set())

    async def test_a_failed_run_does_not_block_the_next_one(self):
        b = make_bot(FakeClaude(RuntimeError("boom"), [assistant("ok"), finished()]))
        await b.run_agent([TARGET], "eins", announce=False)
        await b.run_agent([TARGET], "zwei", announce=False)
        self.assertEqual(delivered(b)[-1], "ok")
        self.assertTrue(b.agent_state["ok"])
        self.assertEqual([r["ok"] for r in b.run_history], [True, False])


class ConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def ask(self, b, command):
        b.current_run["targets"] = [TARGET]
        pending = asyncio.ensure_future(b.can_use_tool("Bash", {"command": command}, None))
        for _ in range(3):
            await asyncio.sleep(0)
        return pending

    async def test_non_destructive_commands_run_unattended(self):
        result = await make_bot().can_use_tool("Bash", {"command": "ls /app"}, None)
        self.assertEqual(type(result).__name__, "PermissionResultAllow")

    async def test_destructive_command_waits_for_the_owner(self):
        b = make_bot()
        pending = await self.ask(b, "rm -rf /x")
        self.assertFalse(pending.done())
        self.assertEqual(b.maybe_answer_confirmation("nonsense"), "hint")
        self.assertEqual(b.maybe_answer_confirmation("yes"), "handled")
        self.assertEqual(type((await pending)).__name__, "PermissionResultAllow")

    async def test_refusal_denies(self):
        b = make_bot()
        pending = await self.ask(b, "rm -rf /x")
        self.assertEqual(b.maybe_answer_confirmation("no"), "handled")
        self.assertEqual(type((await pending)).__name__, "PermissionResultDeny")

    async def test_aborted_run_cannot_use_tools(self):
        b = make_bot()
        b.current_run["aborted"] = True
        result = await b.can_use_tool("Read", {}, None)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")

    async def test_no_pending_confirmation_means_the_message_is_a_normal_one(self):
        self.assertIsNone(make_bot().maybe_answer_confirmation("yes"))


if __name__ == "__main__":
    unittest.main()
