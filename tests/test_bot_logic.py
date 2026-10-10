import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from bot import JsonDatabase, chunks, format_mention


class JsonDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "bot_database.json"
        self.db = JsonDatabase(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reset_clears_admins_but_preserves_target_users(self):
        chat_id = -1001234567890
        self.db.ensure_group(chat_id, "گروه تست")
        self.db.upsert_user(
            chat_id,
            {
                "user_id": 321,
                "first_name": "آرمان",
                "last_name": None,
                "username": "arman",
            },
        )
        self.db.set_allowed_admin(chat_id, 321, True)

        preserved = self.db.reset_group_settings(chat_id, "گروه تست")
        group = self.db.get_group(chat_id)

        self.assertEqual(preserved, 1)
        self.assertEqual(group["allowed_admins"], [])
        self.assertIn("321", group["target_users"])
        self.assertEqual(group["target_users"]["321"]["username"], "arman")

    def test_json_is_written_and_can_be_reloaded(self):
        self.db.ensure_group(-10022, "Reload test")
        self.db.upsert_user(-10022, {"id": 42, "first_name": "Test"})

        with self.path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        restored = JsonDatabase(self.path)

        self.assertEqual(
            payload["groups"]["-10022"]["target_users"]["42"]["first_name"], "Test"
        )
        self.assertEqual(restored.get_group(-10022)["title"], "Reload test")

    def test_member_exit_removes_member_and_allowlist_grant(self):
        chat_id = -10033
        self.db.ensure_group(chat_id, "Exit test")
        self.db.upsert_user(chat_id, {"user_id": 77, "first_name": "Admin"})
        self.db.set_allowed_admin(chat_id, 77, True)

        self.assertTrue(self.db.remove_user(chat_id, 77))
        group = self.db.get_group(chat_id)
        self.assertNotIn("77", group["target_users"])
        self.assertNotIn(77, group["allowed_admins"])


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = JsonDatabase(Path(self.temp_dir.name) / "permissions.json")
        self.chat = SimpleNamespace(id=-100987, type="supergroup")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_owner_is_authorized_without_admin_lookup(self):
        telegram_bot = SimpleNamespace(get_chat_member=AsyncMock())
        with patch("bot.DB", self.db):
            allowed = await bot.is_authorized_tagger(
                bot.OWNER_ID, self.chat, telegram_bot
            )

        self.assertTrue(allowed)
        telegram_bot.get_chat_member.assert_not_awaited()

    async def test_allowlisted_user_must_still_be_a_group_admin(self):
        user_id = 456
        self.db.ensure_group(self.chat.id, "Permissions")
        self.db.set_allowed_admin(self.chat.id, user_id, True)
        telegram_bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="administrator")
            )
        )
        with patch("bot.DB", self.db):
            allowed = await bot.is_authorized_tagger(user_id, self.chat, telegram_bot)
            telegram_bot.get_chat_member.return_value = SimpleNamespace(status="member")
            demoted = await bot.is_authorized_tagger(user_id, self.chat, telegram_bot)

        self.assertTrue(allowed)
        self.assertFalse(demoted)

    async def test_non_allowlisted_user_is_denied_without_api_lookup(self):
        telegram_bot = SimpleNamespace(get_chat_member=AsyncMock())
        with patch("bot.DB", self.db):
            allowed = await bot.is_authorized_tagger(789, self.chat, telegram_bot)

        self.assertFalse(allowed)
        telegram_bot.get_chat_member.assert_not_awaited()


class PanelCloseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chat = SimpleNamespace(id=-100654, type="supergroup")

    async def test_manager_cannot_close_another_managers_panel(self):
        owner_id = 1001
        query = SimpleNamespace(
            data=f"close_panel_{owner_id}",
            from_user=SimpleNamespace(id=2002),
            message=SimpleNamespace(
                chat=self.chat, message_id=55, edit_text=AsyncMock()
            ),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"tag_panels": {}}),
            bot=SimpleNamespace(get_chat_member=AsyncMock()),
        )

        await bot.handle_close_callback(query, context)

        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.edit_message_text.assert_not_awaited()

    async def test_owner_can_close_another_users_panel(self):
        owner_id = 1001
        key = bot._panel_key(self.chat.id, 55)
        query = SimpleNamespace(
            data=f"close_panel_{owner_id}",
            from_user=SimpleNamespace(id=bot.OWNER_ID),
            message=SimpleNamespace(chat=self.chat, message_id=55),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"tag_panels": {key: {"creator_id": owner_id}}}
            ),
            bot=SimpleNamespace(),
        )

        await bot.handle_close_callback(query, context)

        query.edit_message_text.assert_awaited_once_with(
            "پنل بسته شد.", reply_markup=None
        )
        self.assertNotIn(key, context.application.bot_data["tag_panels"])


class HelperTests(unittest.TestCase):
    def test_mentions_use_username_or_user_link(self):
        self.assertEqual(
            format_mention({"user_id": 10, "first_name": "Ali", "username": "ali"}),
            "@ali",
        )
        mention = format_mention(
            {"user_id": 11, "first_name": "A [B]", "username": None}
        )
        self.assertIn("tg://user?id=11", mention)
        self.assertTrue(mention.startswith("["))

    def test_chunks_are_at_most_five(self):
        result = chunks(list(range(12)), 5)
        self.assertEqual([len(batch) for batch in result], [5, 5, 2])
        self.assertEqual([item for batch in result for item in batch], list(range(12)))

    def test_chunks_reject_non_positive_size(self):
        with self.assertRaises(ValueError):
            chunks([1, 2], 0)


if __name__ == "__main__":
    unittest.main()
