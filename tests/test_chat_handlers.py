"""Where messages go, and what the bot does with incoming Matrix and Signal ones."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bot
from fakes import make_bot

OWNER = "@owner:example"
S = bot.STRINGS["en"]


def room(users=(), encrypted=False):
    return SimpleNamespace(users=list(users), encrypted=encrypted)


class TargetsTest(unittest.TestCase):
    def test_configured_notify_room_wins(self):
        b = make_bot(notify_room="!fixed:example")
        b.matrix.rooms = {"!other:example": room([OWNER])}
        self.assertEqual(b.default_matrix_target(), ("matrix", "!fixed:example"))

    def test_last_room_is_used_while_the_bot_is_still_in_it(self):
        b = make_bot()
        b.matrix.rooms = {"!a:example": room(), "!b:example": room()}
        b.state["last_room_id"] = "!b:example"
        self.assertEqual(b.default_matrix_target(), ("matrix", "!b:example"))
        b.state["last_room_id"] = "!gone:example"
        self.assertEqual(b.default_matrix_target(), ("matrix", "!a:example"))

    def test_prefers_a_room_with_an_allowlisted_user(self):
        b = make_bot()
        b.matrix.rooms = {"!notices:example": room(["@server:example"]), "!chat:example": room([OWNER])}
        self.assertEqual(b.default_matrix_target(), ("matrix", "!chat:example"))

    def test_any_room_is_better_than_none(self):
        b = make_bot()
        b.matrix.rooms = {"!only:example": room(["@server:example"])}
        self.assertEqual(b.default_matrix_target(), ("matrix", "!only:example"))
        b.matrix.rooms = {}
        self.assertIsNone(b.default_matrix_target())

    def test_signal_is_added_only_when_enabled_and_a_recipient_is_set(self):
        b = make_bot(signal=bot.SignalConfig(api="http://s", number="+1", allowed=frozenset({"+2"}), notify="+2", enabled=True))
        b.matrix.rooms = {"!r:example": room([OWNER])}
        self.assertEqual(b.notify_targets(), [("matrix", "!r:example"), ("signal", "+2")])
        b = make_bot(signal=bot.SignalConfig(api="http://s", number="+1", allowed=frozenset({"+2"}), enabled=True))
        b.matrix.rooms = {"!r:example": room([OWNER])}
        self.assertEqual(b.notify_targets(), [("matrix", "!r:example")])


class SignalEnvelopeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = make_bot(
            signal=bot.SignalConfig(api="http://s", number="+49000", allowed=frozenset({"+491"}), enabled=True)
        )
        self.b.run_agent = mock.AsyncMock()
        self.b.signal_send = mock.AsyncMock()

    def envelope(self, source="+491", **data_message):
        return {"envelope": {"sourceNumber": source, "dataMessage": data_message}}

    async def handle(self, data):
        await self.b.handle_signal_envelope(data)
        await asyncio.sleep(0)

    async def test_text_from_an_allowed_number_runs_the_agent(self):
        await self.handle(self.envelope(message="  Licht an "))
        self.b.run_agent.assert_awaited_once_with([("signal", "+491")], "Licht an")

    async def test_group_messages_are_answered_in_the_group(self):
        await self.handle(self.envelope(message="hi", groupInfo={"groupId": "abc"}))
        self.b.run_agent.assert_awaited_once_with([("signal", "group.abc")], "hi")

    async def test_strangers_our_own_messages_and_empty_ones_are_ignored(self):
        for data in (
            self.envelope(source="+499", message="hi"),
            self.envelope(source="+49000", message="hi"),
            self.envelope(message=""),
            {"envelope": {"sourceNumber": "+491"}},
            {},
        ):
            with self.subTest(data=data):
                await self.handle(data)
        self.b.run_agent.assert_not_called()

    async def test_a_pending_confirmation_swallows_the_reply(self):
        self.b.pending_confirm["future"] = asyncio.get_running_loop().create_future()
        await self.handle(self.envelope(message="yes"))
        self.assertTrue(self.b.pending_confirm["future"].result())
        self.b.run_agent.assert_not_called()

    async def test_unclear_answer_to_a_confirmation_gets_a_hint(self):
        self.b.pending_confirm["future"] = asyncio.get_running_loop().create_future()
        await self.handle(self.envelope(message="maybe"))
        self.b.signal_send.assert_awaited_once_with("+491", S["confirm_hint"])
        self.b.run_agent.assert_not_called()

    async def test_voice_is_ignored_when_it_is_switched_off(self):
        voice = {"attachments": [{"contentType": "audio/aac", "id": "7"}]}
        await self.handle(self.envelope(**voice))
        self.b.run_agent.assert_not_called()


class MatrixEventTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = make_bot()
        self.b.matrix.user_id = "@bot:example"
        self.b.matrix_send = mock.AsyncMock()
        self.b.run_agent = mock.AsyncMock()

    def event(self, sender=OWNER, body="Licht an", ts=2_000):
        return SimpleNamespace(sender=sender, body=body, server_timestamp=ts)

    async def message(self, event, room_id="!r:example"):
        with mock.patch("bot.save_state"):
            await self.b.on_message(SimpleNamespace(room_id=room_id), event)
        await asyncio.sleep(0)

    async def test_message_from_the_owner_runs_the_agent_and_remembers_the_room(self):
        self.b.start_ms = 1_000
        await self.message(self.event())
        self.b.run_agent.assert_awaited_once_with([("matrix", "!r:example")], "Licht an")
        self.assertEqual(self.b.state["last_room_id"], "!r:example")

    async def test_own_old_and_foreign_messages_are_ignored(self):
        self.b.start_ms = 1_000
        for event in (
            self.event(sender="@bot:example"),
            self.event(ts=500),
            self.event(sender="@stranger:example"),
        ):
            with self.subTest(sender=event.sender, ts=event.server_timestamp):
                await self.message(event)
        self.b.run_agent.assert_not_called()
        self.assertNotIn("last_room_id", self.b.state)

    async def test_confirmation_answer_is_not_a_new_request(self):
        self.b.start_ms = 0
        self.b.pending_confirm["future"] = asyncio.get_running_loop().create_future()
        await self.message(self.event(body="no"))
        self.assertFalse(self.b.pending_confirm["future"].result())
        self.b.run_agent.assert_not_called()

    async def test_reaction_answers_a_confirmation(self):
        fut = asyncio.get_running_loop().create_future()
        self.b.pending_confirm["future"] = fut
        reaction = SimpleNamespace(
            type="m.reaction", sender=OWNER, source={"content": {"m.relates_to": {"key": "👍️"}}}
        )
        await self.b.on_unknown(None, reaction)
        self.assertTrue(fut.result())

    async def test_reaction_from_a_stranger_is_ignored(self):
        fut = asyncio.get_running_loop().create_future()
        self.b.pending_confirm["future"] = fut
        reaction = SimpleNamespace(
            type="m.reaction", sender="@stranger:example", source={"content": {"m.relates_to": {"key": "👍"}}}
        )
        await self.b.on_unknown(None, reaction)
        self.assertFalse(fut.done())

    async def test_invites_are_accepted_only_from_the_allowlist(self):
        self.b.matrix.join = mock.AsyncMock()
        await self.b.on_invite(SimpleNamespace(room_id="!new:example"), SimpleNamespace(sender="@stranger:example"))
        self.b.matrix.join.assert_not_awaited()
        await self.b.on_invite(SimpleNamespace(room_id="!new:example"), SimpleNamespace(sender=OWNER))
        self.b.matrix.join.assert_awaited_once_with("!new:example")


class OnlineNoticeTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_sync_announces_once(self):
        b = make_bot()
        b.online_notice_due = True
        b.announce_online = mock.AsyncMock()
        await b.on_sync(None)
        await b.on_sync(None)
        await asyncio.sleep(0)
        b.announce_online.assert_awaited_once()
        self.assertGreater(b.last_sync, 0)

    async def test_nothing_due_means_no_message(self):
        b = make_bot()
        b.online_notice_due = False
        b.announce_online = mock.AsyncMock()
        await b.on_sync(None)
        await asyncio.sleep(0)
        b.announce_online.assert_not_called()

    async def test_sent_notice_is_remembered(self):
        b = make_bot()
        b.online_notice_due = False
        b.notify_targets = lambda: [("matrix", "!r:example")]
        with mock.patch("bot.save_state") as save:
            await b.announce_online()
        b.deliver.assert_awaited_once_with(("matrix", "!r:example"), S["online"].format(version=bot.BOT_VERSION))
        self.assertIn("online_notice_ts", b.state)
        save.assert_called_once()
        self.assertFalse(b.online_notice_due)

    async def test_without_a_room_it_tries_again_for_two_minutes(self):
        b = make_bot()
        b.notify_targets = lambda: []
        await b.announce_online()
        self.assertTrue(b.online_notice_due)
        b.started_at = time.time() - 121
        await b.announce_online()
        self.assertFalse(b.online_notice_due)

    async def test_failed_delivery_tries_again_too(self):
        b = make_bot()
        b.notify_targets = lambda: [("matrix", "!r:example")]
        b.deliver.side_effect = RuntimeError("down")
        await b.announce_online()
        self.assertTrue(b.online_notice_due)
        self.assertNotIn("online_notice_ts", b.state)

    async def test_scheduled_restart_marker_is_consumed_at_start(self):
        with mock.patch("bot.load_state", return_value={"planned_restart": True}), mock.patch("bot.save_state") as save:
            quiet = bot.Bot(make_bot().cfg)
        self.assertFalse(quiet.online_notice_due)
        self.assertNotIn("planned_restart", quiet.state)
        save.assert_called_once()


class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_files_are_posted_to_every_target_and_removed(self):
        b = make_bot()
        b.flush_outbox = bot.Bot.flush_outbox.__get__(b)  # the real one, not the mock
        b.matrix_post_file = mock.AsyncMock()
        b.signal_send_file = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp)
            (outbox / "snap.jpg").write_bytes(b"x")
            (outbox / ".hidden").write_bytes(b"x")
            (outbox / "subdir").mkdir()
            with mock.patch.object(bot, "OUTBOX_DIR", outbox):
                await b.flush_outbox([("matrix", "!r:example"), ("signal", "+491")])
            self.assertEqual(sorted(p.name for p in outbox.iterdir()), [".hidden", "subdir"])
        b.matrix_post_file.assert_awaited_once_with("!r:example", outbox / "snap.jpg")
        b.signal_send_file.assert_awaited_once_with("+491", outbox / "snap.jpg")

    async def test_one_failing_target_does_not_stop_the_others_or_the_cleanup(self):
        b = make_bot()
        b.flush_outbox = bot.Bot.flush_outbox.__get__(b)
        b.matrix_post_file = mock.AsyncMock(side_effect=RuntimeError("upload failed"))
        b.signal_send_file = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp)
            (outbox / "snap.jpg").write_bytes(b"x")
            with mock.patch.object(bot, "OUTBOX_DIR", outbox):
                await b.flush_outbox([("matrix", "!r:example"), ("signal", "+491")])
            self.assertEqual(list(outbox.iterdir()), [])
        b.signal_send_file.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
