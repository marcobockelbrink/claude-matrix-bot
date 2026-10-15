"""Reading the environment, and the clock times the scheduled jobs run at."""

import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import bot

BERLIN = ZoneInfo("Europe/Berlin")

MINIMAL_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
    "HA_BASE_URL": "http://ha.local:8123/",
    "HA_TOKEN": "ha",
    "MATRIX_HOMESERVER": "https://matrix.example",
    "MATRIX_USER": "@bot:example",
    "MATRIX_PASSWORD": "pw",
    "MATRIX_ALLOWED_USERS": "@a:example, @b:example",
}


def load(**extra):
    env = {**MINIMAL_ENV, **extra}
    with mock.patch.dict(os.environ, env, clear=True):
        return bot.load_config()


class LoadConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = load()
        self.assertEqual(cfg.claude_auth, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth"})
        self.assertEqual(cfg.ha_base_url, "http://ha.local:8123")  # no trailing slash
        self.assertEqual(cfg.allowed_users, {"@a:example", "@b:example"})
        self.assertEqual(
            (cfg.lang, cfg.restart_time, cfg.briefing_time, cfg.webhook_port, cfg.notify_smart_limit),
            ("de", "03:00", "", 0, 6),
        )
        self.assertEqual((cfg.whisper_model, cfg.voice_enabled), ("small", True))
        self.assertTrue(cfg.confirm_destructive)
        self.assertFalse(cfg.announce_planned_restart)
        self.assertEqual(str(cfg.tz), "Europe/Berlin")
        self.assertFalse(cfg.signal.enabled)
        self.assertIsNone(cfg.model)

    def test_secrets_stay_out_of_the_repr(self):
        text = repr(load(WEBHOOK_TOKEN="w" * 32))
        for secret in ("oauth", "pw", "w" * 32, "'ha'"):
            self.assertNotIn(secret, text)
        self.assertIn("matrix.example", text)

    def test_missing_credentials_or_settings_stop_the_start(self):
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "HA_TOKEN", "MATRIX_PASSWORD", "MATRIX_ALLOWED_USERS"):
            with self.subTest(missing=name):
                env = {k: v for k, v in MINIMAL_ENV.items() if k != name}
                with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
                    bot.load_config()

    def test_api_key_works_instead_of_the_oauth_token(self):
        env = {k: v for k, v in MINIMAL_ENV.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        env["ANTHROPIC_API_KEY"] = "key"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(bot.load_config().claude_auth, {"ANTHROPIC_API_KEY": "key"})

    def test_single_allowed_user_from_the_v1_variable(self):
        env = {k: v for k, v in MINIMAL_ENV.items() if k != "MATRIX_ALLOWED_USERS"}
        env["MATRIX_ALLOWED_USER"] = "@old:example"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(bot.load_config().allowed_users, {"@old:example"})

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(load(BOT_LANG="xx").lang, "en")
        self.assertEqual(load(BOT_LANG="EN").lang, "en")

    def test_flags(self):
        self.assertTrue(load(ANNOUNCE_SCHEDULED_RESTART="on").announce_planned_restart)
        self.assertTrue(load(ANNOUNCE_SCHEDULED_RESTART="TRUE").announce_planned_restart)
        self.assertFalse(load(ANNOUNCE_SCHEDULED_RESTART="nope").announce_planned_restart)
        self.assertFalse(load(CONFIRM_DESTRUCTIVE="false").confirm_destructive)
        self.assertTrue(load(CONFIRM_DESTRUCTIVE="yes").confirm_destructive)

    def test_voice_can_be_switched_off(self):
        for value in ("off", "OFF", ""):
            with self.subTest(value=value):
                self.assertFalse(load(WHISPER_MODEL=value).voice_enabled)

    def test_signal_needs_an_allowlist(self):
        both = {"SIGNAL_API_URL": "http://signal:8080/", "SIGNAL_NUMBER": "+491"}
        self.assertFalse(load(**both).signal.enabled)
        cfg = load(**both, SIGNAL_ALLOWED_NUMBERS="+492, +493", SIGNAL_NOTIFY="+492")
        self.assertTrue(cfg.signal.enabled)
        self.assertEqual(cfg.signal.api, "http://signal:8080")
        self.assertEqual(cfg.signal.allowed, {"+492", "+493"})
        self.assertFalse(load(SIGNAL_API_URL="http://signal:8080").signal.enabled)


class ParseClockTest(unittest.TestCase):
    def test_valid_times(self):
        self.assertEqual(bot.parse_clock("03:00"), (3, 0))
        self.assertEqual(bot.parse_clock("7:5"), (7, 5))
        self.assertEqual(bot.parse_clock("23:59"), (23, 59))

    def test_everything_else_is_none(self):
        for value in ("", "off", "3", "24:00", "12:60", "-1:00", "1:2:3", "ab:cd"):
            with self.subTest(value=value):
                self.assertIsNone(bot.parse_clock(value))


class NextOccurrenceTest(unittest.TestCase):
    NOW = datetime(2026, 10, 15, 13, 0, 30, tzinfo=BERLIN)

    def test_later_today(self):
        self.assertEqual(
            bot.next_occurrence(self.NOW, 14, 0), datetime(2026, 10, 15, 14, 0, tzinfo=BERLIN)
        )

    def test_already_past_means_tomorrow(self):
        self.assertEqual(
            bot.next_occurrence(self.NOW, 3, 0), datetime(2026, 10, 16, 3, 0, tzinfo=BERLIN)
        )

    def test_the_current_minute_is_already_past(self):
        # 13:00:30 is after 13:00:00, so the 13:00 job is tomorrow's.
        self.assertEqual(
            bot.next_occurrence(self.NOW, 13, 0), datetime(2026, 10, 16, 13, 0, tzinfo=BERLIN)
        )

    def test_exactly_on_the_dot_is_not_in_the_future(self):
        now = datetime(2026, 10, 15, 3, 0, tzinfo=BERLIN)
        self.assertEqual(
            bot.next_occurrence(now, 3, 0), datetime(2026, 10, 16, 3, 0, tzinfo=BERLIN)
        )

    def test_month_end(self):
        now = datetime(2026, 10, 31, 23, 30, tzinfo=BERLIN)
        self.assertEqual(
            bot.next_occurrence(now, 3, 0), datetime(2026, 11, 1, 3, 0, tzinfo=BERLIN)
        )


if __name__ == "__main__":
    unittest.main()
