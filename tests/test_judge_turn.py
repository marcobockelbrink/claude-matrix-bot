"""Deciding whether an agent turn can be trusted, and whether a new session helps."""

import unittest
from types import SimpleNamespace

from bot import judge_turn


def result(**overrides):
    fields = {"subtype": "success", "is_error": False, "api_error_status": None}
    fields.update(overrides)
    return SimpleNamespace(**fields)


class JudgeTurnTest(unittest.TestCase):
    def test_normal_turn_is_fine(self):
        self.assertIsNone(judge_turn(result(), []))

    def test_turn_that_hit_a_limit_is_not_a_broken_session(self):
        # error_max_turns etc. still carry a usable partial answer.
        self.assertIsNone(judge_turn(result(subtype="error_max_turns", is_error=True), []))

    def test_crashed_session_means_new_session(self):
        for subtype in ("error_during_execution", "error_max_structured_output_retries"):
            with self.subTest(subtype=subtype):
                reason, reset = judge_turn(result(subtype=subtype, is_error=True), [])
                self.assertTrue(reset)
                self.assertIn(subtype, reason)

    def test_budget_limit_is_not_a_broken_session(self):
        self.assertIsNone(judge_turn(result(subtype="error_max_budget_usd", is_error=True), []))

    def test_limit_wins_over_api_errors_seen_on_the_way(self):
        self.assertIsNone(
            judge_turn(result(subtype="error_max_turns", is_error=True), ["rate_limit"])
        )

    def test_missing_result_means_new_session(self):
        reason, reset = judge_turn(None, [])
        self.assertTrue(reset)
        self.assertIn("without a result", reason)

    def test_error_flag_on_a_successful_result_means_new_session(self):
        # How the CLI reports e.g. "Prompt is too long".
        reason, reset = judge_turn(result(is_error=True, api_error_status=400), [])
        self.assertTrue(reset)
        self.assertIn("400", reason)

    def test_error_flag_without_status(self):
        _reason, reset = judge_turn(result(is_error=True), [])
        self.assertTrue(reset)

    def test_errors_a_new_session_cannot_fix(self):
        for code in ("authentication_failed", "billing_error", "rate_limit", "server_error"):
            with self.subTest(code=code):
                reason, reset = judge_turn(result(is_error=True), [code])
                self.assertFalse(reset)
                self.assertIn(code, reason)

    def test_http_statuses_a_new_session_cannot_fix(self):
        for status in (401, 403, 429, 500, 529):
            with self.subTest(status=status):
                reason, reset = judge_turn(result(is_error=True, api_error_status=status), [])
                self.assertFalse(reset)
                self.assertIn(str(status), reason)

    def test_invalid_request_and_unknown_get_a_new_session(self):
        for code in ("invalid_request", "unknown"):
            with self.subTest(code=code):
                _reason, reset = judge_turn(result(is_error=True), [code])
                self.assertTrue(reset)

    def test_assistant_error_counts_even_if_the_result_looks_fine(self):
        reason, reset = judge_turn(result(), ["rate_limit"])
        self.assertFalse(reset)
        self.assertIn("rate_limit", reason)

    def test_result_from_an_older_sdk_without_status_field(self):
        old = SimpleNamespace(subtype="success", is_error=True)
        _reason, reset = judge_turn(old, [])
        self.assertTrue(reset)


if __name__ == "__main__":
    unittest.main()
