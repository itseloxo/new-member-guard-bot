import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_completed_message_task_stops_counting_and_is_not_repeated(self):
        bot.set_group_settings(-1005, {"message_limit": 2})

        self.assertEqual(bot.record_text_message(-1005, 9, "member", "Member"), 1)
        self.assertFalse(bot.is_task_completed(-1005, 9, bot.MESSAGE_UNLOCK_TASK))
        self.assertEqual(bot.record_text_message(-1005, 9, "member", "Member"), 2)
        self.assertTrue(bot.is_task_completed(-1005, 9, bot.MESSAGE_UNLOCK_TASK))
        self.assertEqual(bot.record_text_message(-1005, 9, "member", "Member"), 2)
        self.assertEqual(bot.get_task_progress(-1005, 9)["count"], 2)

    def test_lowering_limit_completes_members_who_reached_it(self):
        bot.set_group_settings(-1006, {"message_limit": 50})
        bot.record_text_message(-1006, 10, "member", "Member")
        bot.record_text_message(-1006, 10, "member", "Member")

        bot.set_group_settings(-1006, {"message_limit": 2})

        self.assertTrue(bot.is_task_completed(-1006, 10, bot.MESSAGE_UNLOCK_TASK))

    def test_existing_member_progress_is_backfilled_on_restart(self):
        bot.set_group_settings(-1009, {"message_limit": 2})
        bot.cache_member_identity(-1009, 11, "member", "Member")
        with closing(bot.connect_database()) as connection, connection:
            connection.execute(
                "UPDATE members SET message_count = 2 WHERE chat_id = ? AND user_id = ?",
                (-1009, 11),
            )

        bot.init_database()

        self.assertTrue(bot.is_task_completed(-1009, 11, bot.MESSAGE_UNLOCK_TASK))
        self.assertEqual(bot.get_task_progress(-1009, 11)["count"], 2)

    def test_trusted_members_do_not_accumulate_violations(self):
        bot.set_member_trusted(-1003, 8, True)
        self.assertIsNone(bot.record_violation(-1003, 8, "trusted", "Trusted"))

    def test_message_deletion_schedule_is_persistent(self):
        bot.save_pending_deletion(-1004, 31, 12345.0)

        self.assertEqual(
            bot.get_pending_deletions(),
            [{"chat_id": -1004, "message_id": 31, "delete_at": 12345.0}],
        )
        bot.clear_pending_deletion(-1004, 31)
        self.assertEqual(bot.get_pending_deletions(), [])

    def test_old_warning_schedules_are_migrated(self):
        with closing(bot.connect_database()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE pending_warnings (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    delete_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO pending_warnings VALUES (?, ?, ?)",
                (-1010, 32, 12346.0),
            )

        bot.init_database()

        self.assertEqual(
            bot.get_pending_deletions(),
            [{"chat_id": -1010, "message_id": 32, "delete_at": 12346.0}],
        )


class AutoDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_helper_schedules_default_lifetime(self):
        sender = AsyncMock(return_value=SimpleNamespace(chat_id=-1007, message_id=44))
        job_queue = MagicMock()
        context = SimpleNamespace(
            application=SimpleNamespace(bot=SimpleNamespace(send_message=sender)),
            job_queue=job_queue,
        )

        with patch.object(bot, "time") as mock_time, patch.object(
            bot, "save_pending_deletion"
        ) as save_schedule:
            mock_time.time.return_value = 1000.0
            sent = await bot.send_message_with_auto_delete(context, -1007, "hello")

        self.assertEqual(sent.message_id, 44)
        sender.assert_awaited_once_with(chat_id=-1007, text="hello")
        job_queue.run_once.assert_called_once()
        self.assertEqual(job_queue.run_once.call_args.kwargs["data"]["message_id"], 44)
        self.assertEqual(save_schedule.call_args.args[2], 1060.0)
        self.assertEqual(job_queue.run_once.call_args.kwargs["when"], 60.0)


class CommandCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._INTERACTION_COOLDOWNS.clear()

    async def test_commands_share_one_minute_user_cooldown(self):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
            effective_user=SimpleNamespace(id=42),
        )
        context = SimpleNamespace()
        first_command = AsyncMock()
        second_command = AsyncMock()
        wrapped_first = bot.with_command_cooldown(first_command)
        wrapped_second = bot.with_command_cooldown(second_command)

        with patch.object(bot, "is_admin", new=AsyncMock(return_value=False)), patch.object(
            bot.time, "monotonic", side_effect=(100.0, 101.0, 160.0)
        ):
            await wrapped_first(update, context)
            await wrapped_second(update, context)
            await wrapped_second(update, context)

        first_command.assert_awaited_once_with(update, context)
        second_command.assert_awaited_once_with(update, context)

    async def test_admin_commands_bypass_the_cooldown(self):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
            effective_user=SimpleNamespace(id=7),
        )
        context = SimpleNamespace()
        command = AsyncMock()
        wrapped = bot.with_command_cooldown(command)

        with patch.object(bot, "is_admin", new=AsyncMock(return_value=True)):
            await wrapped(update, context)
            await wrapped(update, context)

        self.assertEqual(command.await_count, 2)

    def test_rules_button_has_a_separate_cooldown_scope(self):
        with patch.object(bot.time, "monotonic", side_effect=(100.0, 100.0, 101.0)):
            self.assertTrue(bot.claim_interaction_cooldown(-1002, 43))
            self.assertTrue(
                bot.claim_interaction_cooldown(-1002, 43, "rules_button")
            )
            self.assertFalse(
                bot.claim_interaction_cooldown(-1002, 43, "rules_button")
            )


class MessageFormattingTests(unittest.TestCase):
    def test_task_status_has_html_styling_and_progress_bar(self):
        status = bot.format_task_message(25, 50, False)
        self.assertIn("<b>YOUR STICKER &amp; GIF PASS</b>", status)
        self.assertIn("🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜", status)
        self.assertIn("<b>50%</b>", status)

    def test_rules_include_the_group_settings_in_styled_sections(self):
        rules = bot.format_rules_message(
            {"message_limit": 50, "violation_limit": 3, "violation_window": 300, "mute_duration": 600}
        )
        self.assertIn("<b>GROUP RULES</b>", rules)
        self.assertIn("<b>50</b> text messages", rules)
        self.assertIn("<b>10m</b> mute", rules)


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
