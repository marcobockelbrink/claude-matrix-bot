"""The status page and the small chat helpers around it."""

import unittest

import bot

PAYLOAD = {
    "agent_ok": True,
    "agent_failure": None,
    "matrix_connected": True,
    "signal_enabled": False,
    "signal_connected": False,
    "uptime_s": 8 * 3600 + 56 * 60,
    "voice": "small",
    "confirm_destructive": True,
    "next_briefing": "2026-10-16T07:00+02:00",
    "next_restart": None,
}
RUN = {"time": "2026-10-15T07:00:12+02:00", "via": "matrix", "ok": True, "duration_s": 12.6, "prompt": "Morgen-Briefing"}


class FormatUptimeTest(unittest.TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(bot.format_uptime(8 * 3600 + 56 * 60), "8h 56m")
        self.assertEqual(bot.format_uptime(59), "0h 0m")

    def test_days_only_when_there_are_some(self):
        self.assertEqual(bot.format_uptime(86400 + 2 * 3600 + 3 * 60), "1d 2h 3m")


class RenderStatusPageTest(unittest.TestCase):
    def test_healthy_page(self):
        page = bot.render_status_page(PAYLOAD, [RUN])
        self.assertIn("online", page)
        self.assertIn("8h 56m", page)
        self.assertIn("2026-10-16T07:00+02:00", page)
        self.assertIn("Morgen-Briefing", page)
        self.assertIn("✅", page)

    def test_unset_schedule_and_disabled_signal_read_off(self):
        page = bot.render_status_page(PAYLOAD, [])
        self.assertIn("none yet", page)
        # Signal (disabled) and Next restart (unset); the briefing is set.
        self.assertEqual(page.count("<td>off</td>"), 2)

    def test_degraded_when_matrix_is_not_connected(self):
        page = bot.render_status_page({**PAYLOAD, "matrix_connected": False}, [])
        self.assertIn("degraded", page)

    def test_agent_failure_is_shown_and_escaped(self):
        page = bot.render_status_page({**PAYLOAD, "agent_ok": False, "agent_failure": "<b>boom</b>"}, [])
        self.assertIn("failing", page)
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", page)
        self.assertNotIn("<b>boom</b>", page)

    def test_run_prompts_are_escaped(self):
        page = bot.render_runs([{**RUN, "prompt": "<script>alert(1)</script>"}])
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_failed_runs_are_marked(self):
        self.assertIn("❌", bot.render_runs([{**RUN, "ok": False}]))


class ChatHelpersTest(unittest.TestCase):
    def test_message_type_follows_the_mime_type(self):
        self.assertEqual(bot.matrix_msgtype("image/png"), "m.image")
        self.assertEqual(bot.matrix_msgtype("audio/ogg"), "m.audio")
        self.assertEqual(bot.matrix_msgtype("video/mp4"), "m.video")
        self.assertEqual(bot.matrix_msgtype("application/pdf"), "m.file")
        self.assertEqual(bot.matrix_msgtype("application/octet-stream"), "m.file")

    def test_plain_file_content_carries_the_url(self):
        content = bot.matrix_file_content("a.png", "image/png", 3, "mxc://x/y", None)
        self.assertEqual(
            content,
            {"msgtype": "m.image", "body": "a.png", "info": {"mimetype": "image/png", "size": 3}, "url": "mxc://x/y"},
        )

    def test_encrypted_file_content_carries_the_keys_not_a_plain_url(self):
        content = bot.matrix_file_content("a.png", "image/png", 3, "mxc://x/y", {"k": "key"})
        self.assertEqual(content["file"], {"k": "key", "url": "mxc://x/y"})
        self.assertNotIn("url", content)

    def test_voice_attachment_is_the_first_audio_one(self):
        dm = {"attachments": [{"contentType": "image/png", "id": "1"}, {"contentType": "audio/aac", "id": "2"}]}
        self.assertEqual(bot.signal_voice_attachment(dm)["id"], "2")
        self.assertIsNone(bot.signal_voice_attachment({"attachments": [{"contentType": None}]}))
        self.assertIsNone(bot.signal_voice_attachment({}))

    def test_unusable_turn_text(self):
        s = bot.STRINGS["en"]
        self.assertEqual(bot.unusable_turn_text(s, False, True), s["agent_unusable"])
        self.assertEqual(bot.unusable_turn_text(s, True, False), s["agent_reset"])
        self.assertEqual(bot.unusable_turn_text(s, True, True), s["agent_reset_partial"])


if __name__ == "__main__":
    unittest.main()
