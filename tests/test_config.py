"""Configuration must never start the bot with unrestricted access."""
import unittest
from unittest.mock import patch

from config import Config


class AccessConfigTests(unittest.TestCase):
    def test_allowed_ids_support_spaces_duplicates_and_multiple_users(self):
        with patch.dict("os.environ", {
            "BOT_TOKEN": "123:test", "ALLOWED_USER_IDS": " 101, 202,101 ",
        }, clear=True):
            self.assertEqual(Config.from_env().allowed_user_ids, frozenset({101, 202}))

    def test_missing_allowed_ids_prevent_startup(self):
        with patch.dict("os.environ", {"BOT_TOKEN": "123:test"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ALLOWED_USER_IDS"):
                Config.from_env()

    def test_invalid_or_empty_allowed_ids_prevent_startup(self):
        for value in ("", " ", "@username", "101,,202", "101,", "0", "-100123", "101,0"):
            with self.subTest(value=value), patch.dict("os.environ", {
                "BOT_TOKEN": "123:test", "ALLOWED_USER_IDS": value,
            }, clear=True):
                with self.assertRaisesRegex(ValueError, "ALLOWED_USER_IDS"):
                    Config.from_env()


if __name__ == "__main__":
    unittest.main()
