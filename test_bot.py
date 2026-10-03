import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


class BotStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        directory = Path(self.temp_dir.name)
        self.database_path = directory / "database.sqlite3"
        self.legacy_path = directory / "database.json"
        self.database_patch = patch.object(bot, "DATABASE_PATH", self.database_path)
        self.legacy_patch = patch.object(bot, "LEGACY_DATABASE_PATH", self.legacy_path)
        self.database_patch.start()
        self.legacy_patch.start()
        bot.init_database()

    def tearDown(self):
        self.database_patch.stop()
        self.legacy_patch.stop()
        self.temp_dir.cleanup()

    def test_legacy_settings_and_member_counts_are_migrated(self):
        self.legacy_path.write_text(
            json.dumps(
                {
                    "groups": {
                        "-1001": {
                            "restriction_mode": "message",
                            "restriction_value": 250,
                            "violation_limit": 4,
                            "violation_window": 30,
                            "mute_duration": 600,
                            "trusted": ["42"],
                            "members": {
                                "42": {
                                    "join_time": "2026-01-01T00:00:00",
                                    "message_count": 20,
                                    "violations": [],
                                    "is_unrestricted": True,
                                }
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bot.migrate_legacy_database()

        self.assertEqual(bot.get_group_settings(-1001)["message_limit"], 250)
        self.assertEqual(bot.get_group_settings(-1001)["violation_limit"], 4)
        self.assertEqual(bot.get_member_record(-1001, 42)["message_count"], 20)
        self.assertEqual(bot.get_member_record(-1001, 42)["is_trusted"], 1)

    def test_text_counts_and_violation_threshold_are_saved(self):
        bot.set_group_settings(
            -1002,
            {"message_limit": 50, "violation_limit": 2, "violation_window": 30},
        )
        self.assertEqual(bot.record_text_message(-1002, 7, "member", "Member"), 1)
        self.assertEqual(bot.record_text_message(-1002, 7, "member", "Member"), 2)

        first = bot.record_violation(-1002, 7, "member", "Member")
        second = bot.record_violation(-1002, 7, "member", "Member")

        self.assertEqual(first[1], 1)
        self.assertEqual(second[1], 2)

    def test_trusted_members_do_not_accumulate_violations(self):
        bot.set_member_trusted(-1003, 8, True)
        self.assertIsNone(bot.record_violation(-1003, 8, "trusted", "Trusted"))

    def test_warning_deletion_schedule_is_persistent(self):
        bot.save_pending_warning(-1004, 31, 12345.0)

        self.assertEqual(
            bot.get_pending_warnings(),
            [{"chat_id": -1004, "message_id": 31, "delete_at": 12345.0}],
        )
        bot.clear_pending_warning(-1004, 31)
        self.assertEqual(bot.get_pending_warnings(), [])


class DurationTests(unittest.TestCase):
    def test_parses_supported_duration_formats(self):
        self.assertEqual(bot.parse_duration("30s"), 30)
        self.assertEqual(bot.parse_duration("5m"), 300)
        self.assertEqual(bot.parse_duration("1d12h"), 129600)
        self.assertEqual(bot.parse_duration("1w"), 604800)

    def test_rejects_invalid_duration_formats(self):
        for value in ("", "0s", "2x", "1hgarbage", "1.5h", "-2m"):
            with self.subTest(value=value):
                self.assertIsNone(bot.parse_duration(value))


class ContentPolicyTests(unittest.TestCase):
    def test_only_stickers_and_gifs_are_restricted(self):
        sticker = SimpleNamespace(sticker=object(), animation=None, document=None)
        animation = SimpleNamespace(sticker=None, animation=object(), document=None)
        gif_file = SimpleNamespace(
            sticker=None,
            animation=None,
            document=SimpleNamespace(mime_type="image/gif"),
        )
        photo = SimpleNamespace(sticker=None, animation=None, document=None, photo=[object()])

        self.assertTrue(bot.is_restricted_content(sticker))
        self.assertTrue(bot.is_restricted_content(animation))
        self.assertTrue(bot.is_restricted_content(gif_file))
        self.assertFalse(bot.is_restricted_content(photo))


if __name__ == "__main__":
    unittest.main()
