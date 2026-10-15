"""POST /notify, GET /status and /healthz, against the real aiohttp routes."""

import asyncio
import time
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import bot
from fakes import make_bot, make_config

TARGET = ("matrix", "!room:example")
TOKEN = make_config().webhook_token
AUTH = {"X-Token": TOKEN}


class WebhookTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.bot.matrix.rooms = {"!room:example": object()}
        self.bot.notify_targets = lambda: [TARGET]
        self.bot.run_agent = mock.AsyncMock()
        self.hook = bot.Webhook(self.bot)
        self.client = TestClient(TestServer(self.hook.build_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def notify(self, payload, headers=AUTH):
        return self.client.post("/notify", json=payload, headers=headers)


class NotifyTest(WebhookTestCase):
    async def test_wrong_or_missing_token_is_rejected(self):
        for headers in ({}, {"X-Token": "nope"}):
            with self.subTest(headers=headers):
                resp = await self.notify({"message": "hi"}, headers=headers)
                self.assertEqual(resp.status, 401)
        self.bot.deliver.assert_not_awaited()

    async def test_token_in_the_query_string_works_too(self):
        resp = await self.client.post(f"/notify?token={TOKEN}", json={"message": "hi"})
        self.assertEqual(resp.status, 200)

    async def test_plain_notification_is_delivered_verbatim_with_prefix(self):
        resp = await self.notify({"message": "Tür offen"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"ok": True, "smart": False})
        self.bot.deliver.assert_awaited_once_with(TARGET, bot.STRINGS["en"]["notify_prefix"] + "Tür offen")
        self.bot.run_agent.assert_not_called()

    async def test_bad_bodies_are_400(self):
        resp = await self.client.post("/notify", data="not json", headers=AUTH)
        self.assertEqual((resp.status, await resp.text()), (400, "invalid JSON"))
        for payload in ([], {"nope": 1}, {"message": ""}):
            with self.subTest(payload=payload):
                resp = await self.notify(payload)
                self.assertEqual(resp.status, 400)
                self.assertIn("invalid body", await resp.text())

    async def test_only_rooms_the_bot_is_in_are_allowed(self):
        resp = await self.notify({"message": "hi", "room": "!elsewhere:example"})
        self.assertEqual((resp.status, await resp.text()), (400, "unknown room"))
        resp = await self.notify({"message": "hi", "room": "!room:example"})
        self.assertEqual(resp.status, 200)
        self.bot.deliver.assert_awaited_once()

    async def test_no_channel_yet_is_503(self):
        self.bot.notify_targets = lambda: []
        resp = await self.notify({"message": "hi"})
        self.assertEqual(resp.status, 503)


class SmartNotifyTest(WebhookTestCase):
    async def test_smart_notification_goes_through_the_agent(self):
        resp = await self.notify({"message": "Rauchmelder", "smart": True})
        self.assertEqual(await resp.json(), {"ok": True, "smart": True})
        await asyncio.sleep(0)
        self.bot.run_agent.assert_awaited_once()
        (targets, prompt), kwargs = self.bot.run_agent.await_args
        self.assertEqual(targets, [TARGET])
        self.assertIn("Rauchmelder", prompt)
        self.assertFalse(kwargs["announce"])
        self.bot.deliver.assert_not_awaited()

    async def test_a_second_smart_one_waits_no_longer_than_one_in_the_queue(self):
        await self.notify({"message": "eins", "smart": True})
        resp = await self.notify({"message": "zwei", "smart": True})
        self.assertEqual(await resp.json(), {"ok": True, "smart": False})
        self.assertEqual(self.hook.smart_waiting, 1)
        self.bot.deliver.assert_awaited_once()

    async def test_waiting_count_drops_when_the_run_starts(self):
        await self.notify({"message": "eins", "smart": True})
        await asyncio.sleep(0)
        self.bot.run_agent.await_args.kwargs["on_start"]()
        self.assertEqual(self.hook.smart_waiting, 0)

    async def test_exhausted_budget_falls_back_to_verbatim(self):
        with mock.patch.object(self.hook.smart_budget, "take", return_value=False):
            resp = await self.notify({"message": "hi", "smart": True})
        self.assertEqual(await resp.json(), {"ok": True, "smart": False})
        self.assertEqual(self.hook.smart_waiting, 0)


class StatusTest(WebhookTestCase):
    async def test_status_needs_the_token(self):
        resp = await self.client.get("/status")
        self.assertEqual(resp.status, 401)

    async def test_json_by_default(self):
        self.bot.last_sync = time.time()
        resp = await self.client.get("/status", headers=AUTH)
        body = await resp.json()
        self.assertEqual(body["version"], bot.BOT_VERSION)
        self.assertTrue(body["matrix_connected"])
        self.assertEqual(body["voice"], "off")
        self.assertIsNone(body["last_run"])

    async def test_html_for_browsers_and_json_on_request(self):
        resp = await self.client.get("/status", headers={**AUTH, "Accept": "text/html"})
        self.assertEqual(resp.content_type, "text/html")
        self.assertIn(bot.BOT_VERSION, await resp.text())
        resp = await self.client.get("/status?format=json", headers={**AUTH, "Accept": "text/html"})
        self.assertEqual(resp.content_type, "application/json")

    async def test_reports_the_schedule_and_the_agent_state(self):
        self.bot.restart_next = "2026-10-16T03:00+02:00"
        self.bot.agent_state.update(ok=False, failure="timed out")
        body = await (await self.client.get("/status", headers=AUTH)).json()
        self.assertEqual(body["next_restart"], "2026-10-16T03:00+02:00")
        self.assertEqual((body["agent_ok"], body["agent_failure"]), (False, "timed out"))


class HealthTest(WebhookTestCase):
    async def test_degraded_until_the_first_sync(self):
        resp = await self.client.get("/healthz")
        self.assertEqual((resp.status, await resp.json()), (503, {"status": "degraded"}))

    async def test_ok_while_syncing_and_without_a_token(self):
        self.bot.last_sync = time.time()
        resp = await self.client.get("/healthz")
        self.assertEqual((resp.status, await resp.json()), (200, {"status": "ok"}))

    async def test_degraded_when_the_sync_loop_went_quiet(self):
        self.bot.last_sync = time.time() - 301
        resp = await self.client.get("/healthz")
        self.assertEqual(resp.status, 503)


class BadTokenLogTest(WebhookTestCase):
    async def test_repeated_bad_tokens_are_logged_once_per_minute(self):
        with self.assertLogs("ha-matrix-bot", "WARNING") as logs:
            for _ in range(5):
                await self.notify({"message": "x"}, headers={"X-Token": "bad"})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.hook.bad_token["suppressed"], 4)


if __name__ == "__main__":
    unittest.main()
