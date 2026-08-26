"""Exercise access checks through the real Telegram handler dispatcher, offline."""
import importlib.util
import unittest
from unittest.mock import AsyncMock, Mock, patch

if importlib.util.find_spec("telegram") is None:
    raise unittest.SkipTest("python-telegram-bot is required for dispatcher tests")

from telegram import Update, User
from telegram.ext import ExtBot

import bot
from config import Config


COMMANDS = {
    "start": "cmd_start", "help": "cmd_start",
    "setsupergroup": "cmd_set_supergroup", "sync": "cmd_sync",
    "upload": "cmd_upload_help", "status": "cmd_status", "topics": "cmd_topics",
    "bindtopic": "cmd_bind_topic", "bindnote": "cmd_bind_note",
    "tree": "cmd_tree", "graph": "cmd_graph",
}
GROUP_ID = -1001234567890


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict("os.environ", {
            "BOT_TOKEN": "123:test", "ALLOWED_USER_IDS": "101,303",
        }, clear=True):
            cfg = Config.from_env()

        names = set(COMMANDS.values()) | {"handle_zip_upload", "observe_forum_topic"}
        self.callbacks = {name: AsyncMock() for name in names}
        with patch.multiple(bot, **self.callbacks):
            self.app = bot.build_application(cfg, Mock())

        async def fake_get_me(instance, **kwargs):
            instance._bot_user = User(999, "Test bot", True, username="test_sync_bot")
            return instance._bot_user

        # Initialize the actual dispatcher without making Telegram API requests.
        with patch.object(ExtBot, "get_me", fake_get_me):
            await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        send_patch = patch.object(ExtBot, "send_message", new_callable=AsyncMock)
        self.send_message = send_patch.start()
        self.addCleanup(send_patch.stop)
        self.errors = AsyncMock()
        self.app.add_error_handler(self.errors)

    def make_update(self, user_id=202, group=False, command=None, document=False,
                    sender_chat=False, topic_event=False, text=None, zip_mime=True):
        message = {
            "message_id": 1, "date": 1700000000,
            "chat": {"id": GROUP_ID if group else user_id or 202,
                     "type": "supergroup" if group else "private"},
        }
        if user_id is not None:
            message["from"] = {"id": user_id, "is_bot": False, "first_name": "Test"}
        if sender_chat:
            message["sender_chat"] = {"id": GROUP_ID, "type": "supergroup", "title": "Forum"}
        if command:
            message["text"] = f"/{command}"
            message["entities"] = [{"type": "bot_command", "offset": 0,
                                    "length": len(command) + 1}]
        if document:
            message["document"] = {
                "file_id": "fake", "file_unique_id": "unique", "file_name": "Vault.zip",
                "mime_type": "application/zip" if zip_mime else "application/octet-stream",
            }
        if topic_event:
            message["message_thread_id"] = 77
            message["forum_topic_created"] = {"name": "Private", "icon_color": 123}
        if text:
            message["text"] = text
        return Update.de_json({"update_id": 1, "message": message}, self.app.bot)

    def assert_no_callbacks(self):
        for name, callback in self.callbacks.items():
            self.assertEqual(callback.await_count, 0, name)
        self.errors.assert_not_awaited()

    async def test_outsider_cannot_run_any_command_in_dm_or_group(self):
        for group in (False, True):
            for command in COMMANDS:
                with self.subTest(group=group, command=command):
                    await self.app.process_update(self.make_update(group=group, command=command))
                    self.assert_no_callbacks()

    async def test_outsider_zip_is_blocked_for_both_upload_filters(self):
        for group in (False, True):
            for zip_mime in (False, True):
                with self.subTest(group=group, zip_mime=zip_mime):
                    await self.app.process_update(self.make_update(
                        group=group, document=True, zip_mime=zip_mime,
                    ))
                    self.assert_no_callbacks()

    async def test_allowed_users_can_run_all_commands_and_upload(self):
        for user_id in (101, 303):
            for group in (False, True):
                for command, name in COMMANDS.items():
                    with self.subTest(user_id=user_id, group=group, command=command):
                        callback = self.callbacks[name]
                        callback.reset_mock()
                        await self.app.process_update(self.make_update(
                            user_id=user_id, group=group, command=command,
                        ))
                        callback.assert_awaited_once()
                for zip_mime in (False, True):
                    callback = self.callbacks["handle_zip_upload"]
                    callback.reset_mock()
                    await self.app.process_update(self.make_update(
                        user_id=user_id, group=group, document=True, zip_mime=zip_mime,
                    ))
                    callback.assert_awaited_once()
        self.send_message.assert_not_awaited()
        self.errors.assert_not_awaited()

    async def test_anonymous_and_sender_chat_commands_are_blocked(self):
        for user_id in (None, 101):
            with self.subTest(user_id=user_id):
                await self.app.process_update(self.make_update(
                    user_id=user_id, group=True, sender_chat=True, command="setsupergroup",
                ))
                self.assert_no_callbacks()

    async def test_topic_events_require_an_allowed_user(self):
        for user_id in (None, 202):
            await self.app.process_update(self.make_update(
                user_id=user_id, group=True, topic_event=True,
            ))
            self.assert_no_callbacks()
        self.send_message.assert_not_awaited()
        await self.app.process_update(self.make_update(user_id=101, group=True, topic_event=True))
        self.callbacks["observe_forum_topic"].assert_awaited_once()

    async def test_reply_failure_does_not_allow_an_unauthorized_command(self):
        self.send_message.side_effect = RuntimeError("Cannot send denial")
        await self.app.process_update(self.make_update(command="sync"))
        self.assert_no_callbacks()

    async def test_normal_outsider_messages_are_ignored_silently(self):
        await self.app.process_update(self.make_update(group=True, text="Hello"))
        self.assert_no_callbacks()
        self.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
