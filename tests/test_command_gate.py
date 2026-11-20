"""The destructive-command gate, the Signal-API guard, token checks and
background-task error visibility."""

import asyncio
import unittest
from unittest import mock

import bot
from fakes import make_config


class DestructiveRegexTest(unittest.TestCase):
    def test_needs_confirmation(self):
        cases = [
            "rm -rf /tmp/x",
            "rmdir dir",
            "mkfs.ext4 /dev/sda",
            "dd if=/dev/zero of=/tmp/x",
            "shutdown -h now",
            "reboot",
            "kill -9 1234",
            "killall python",
            "apt purge some-package",
            "truncate -s 0 movie.mkv",
            "shred secret.txt",
            "find . -delete",
            "find . -name '*.log' -print -delete | tee list",
            "curl -X DELETE $HA_BASE_URL/api/states/light.x",
            "curl --request 'DELETE' $HA_BASE_URL/api/states/light.x",
            "curl -XPOST $HA_BASE_URL/api/services/homeassistant/restart",
            "homeassistant.stop service call",
            "curl -X POST $HA_BASE_URL/api/services/backup/delete",
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                self.assertTrue(bot.DESTRUCTIVE_RE.search(cmd))

    def test_runs_without_confirmation(self):
        cases = [
            "ls -la",
            "curl -s -X POST $HA_BASE_URL/api/services/switch/turn_off -d '{}'",
            "curl -s $HA_BASE_URL/api/states",
            "sensor.rms_wave/state",  # entity id containing 'rm'
            "skill foo",  # contains 'kill'
            "echo killpill",
            "grep -r DELETE .",  # the word alone in a read must not trigger
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                self.assertFalse(bot.DESTRUCTIVE_RE.search(cmd))


class SignalApiGuardTest(unittest.TestCase):
    def test_flagged_and_clean(self):
        cfg = make_config(
            signal=bot.SignalConfig(
                api="http://signal:8080",
                number="+15550100",
                allowed=frozenset({"+15550100"}),
                notify="+15550100",
                enabled=True,
            )
        )
        with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
            b = bot.Bot(cfg)
        self.assertTrue(b.touches_signal_api("curl http://signal:8080/v2/send -d x"))
        self.assertTrue(b.touches_signal_api("curl signal:8080/v2/send"))
        # Not inside an entity id …
        self.assertFalse(b.touches_signal_api(
            "curl -s $HA_BASE_URL/api/states/sensor.wifi_signal/state"))
        # … not a longer word …
        self.assertFalse(b.touches_signal_api("echo signals and smoke"))
        # … and ignored when Signal is off.
        with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
            b_off = bot.Bot(make_config())
        self.assertFalse(b_off.touches_signal_api("curl http://signal:8080/v2/send"))


class TokenComparisonTest(unittest.TestCase):
    async def test_non_ascii_token_is_a_plain_401_not_a_500(self):
        with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
            b = bot.Bot(make_config())
        hook = bot.Webhook(b)
        request = mock.MagicMock()
        request.headers = {"X-Token": "ä"}
        request.query = {}
        self.assertFalse(hook.token_ok(request))


class SpawnFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_exception_in_spawned_task_is_logged(self):
        with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
            b = bot.Bot(make_config())
        failure = RuntimeError("boom in delivery")

        async def fail():
            raise failure

        with self.assertLogs("ha-matrix-bot", level="ERROR") as captured:
            b.spawn(fail())
            await asyncio.sleep(0.05)
        self.assertTrue(any("boom in delivery" in line for line in captured.output))
        self.assertEqual(b.background_tasks, set())

    async def test_exception_in_a_run_agent_task_is_logged(self):
        with mock.patch("bot.load_state", return_value={}), mock.patch("bot.save_state"):
            b = bot.Bot(make_config())
        b.deliver = mock.AsyncMock(side_effect=RuntimeError("transport down"))

        with self.assertLogs("ha-matrix-bot", level="ERROR") as captured:
            b.spawn(b.run_agent([("matrix", "!r:example")], "hi", announce=False))
            await asyncio.sleep(0.05)
        self.assertTrue(any("transport down" in line for line in captured.output))
