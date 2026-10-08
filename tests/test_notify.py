"""Validation of the /notify webhook body.

Run inside the image, which has the bot's dependencies:
    python -m unittest discover -s tests -v
"""

import unittest

from bot import parse_notify


class ParseNotifyTest(unittest.TestCase):
    def test_plain_message(self):
        self.assertEqual(parse_notify({"message": " Tür offen "}), ("Tür offen", None, False))

    def test_smart_and_room(self):
        self.assertEqual(
            parse_notify({"message": "x", "smart": True, "room": "!abc:matrix.org"}),
            ("x", "!abc:matrix.org", True),
        )

    def test_non_string_message_is_stringified(self):
        # HA templates may render a bare number.
        self.assertEqual(parse_notify({"message": 21.5}), ("21.5", None, False))

    def test_body_must_be_an_object(self):
        for body in ([], ["message"], "text", 5, None, True):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_notify(body)

    def test_message_is_required(self):
        for body in ({}, {"message": ""}, {"message": "   "}, {"message": None}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_notify(body)

    def test_room_must_be_a_string(self):
        for room in (5, 0, False, ["!abc:matrix.org"], {"id": "!abc:matrix.org"}):
            with self.subTest(room=room), self.assertRaises(ValueError):
                parse_notify({"message": "x", "room": room})

    def test_empty_room_means_default(self):
        self.assertEqual(parse_notify({"message": "x", "room": ""}), ("x", None, False))


if __name__ == "__main__":
    unittest.main()
