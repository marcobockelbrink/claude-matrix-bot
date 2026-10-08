"""When the bot tells the owner that it is back online."""

import unittest

from bot import ONLINE_NOTICE_MIN_GAP_S, should_announce_start

NOW = 1_800_000_000.0


class ShouldAnnounceStartTest(unittest.TestCase):
    def test_first_start_ever(self):
        self.assertTrue(should_announce_start({}, NOW))

    def test_start_after_a_crash_or_manual_restart(self):
        state = {"last_room_id": "!abc:matrix.org", "online_notice_ts": NOW - 86400}
        self.assertTrue(should_announce_start(state, NOW))

    def test_scheduled_restart_stays_quiet(self):
        # Nobody wants a message at 03:00 every night.
        self.assertFalse(should_announce_start({"planned_restart": True}, NOW))

    def test_restart_loop_does_not_spam(self):
        state = {"online_notice_ts": NOW - 30}
        self.assertFalse(should_announce_start(state, NOW))

    def test_gap_boundary(self):
        self.assertFalse(
            should_announce_start({"online_notice_ts": NOW - ONLINE_NOTICE_MIN_GAP_S + 1}, NOW)
        )
        self.assertTrue(
            should_announce_start({"online_notice_ts": NOW - ONLINE_NOTICE_MIN_GAP_S}, NOW)
        )

    def test_garbage_in_the_state_file_does_not_break_the_start(self):
        self.assertTrue(should_announce_start({"online_notice_ts": "yesterday"}, NOW))


if __name__ == "__main__":
    unittest.main()
