"""Sliding-window budget for agent runs triggered by smart notifications."""

import unittest

from bot import SmartBudget


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SmartBudgetTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_allows_up_to_the_limit(self):
        budget = SmartBudget(3, window_s=600, clock=self.clock)
        self.assertEqual([budget.take() for _ in range(5)], [True, True, True, False, False])

    def test_slot_frees_up_when_it_leaves_the_window(self):
        budget = SmartBudget(2, window_s=600, clock=self.clock)
        self.assertTrue(budget.take())
        self.clock.now += 300
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())
        # The first run is now exactly one window old, the second is not.
        self.clock.now += 300
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

    def test_refused_attempts_do_not_extend_the_block(self):
        budget = SmartBudget(1, window_s=600, clock=self.clock)
        self.assertTrue(budget.take())
        for _ in range(50):
            self.clock.now += 10
            self.assertFalse(budget.take())
        self.clock.now += 100
        self.assertTrue(budget.take())

    def test_zero_or_negative_limit_refuses_everything(self):
        for limit in (0, -3):
            with self.subTest(limit=limit):
                self.assertFalse(SmartBudget(limit, clock=self.clock).take())


if __name__ == "__main__":
    unittest.main()
