"""Offline unit tests for topic adoption and note upserts."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# The telegram library is installed in production. Provide a minimal shim so
# these isolated sync tests can also run without downloading dependencies.
if importlib.util.find_spec("telegram") is None:
    telegram = types.ModuleType("telegram")
    telegram.Bot = type("Bot", (), {})
    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = SimpleNamespace(HTML="HTML")
    errors = types.ModuleType("telegram.error")
    errors.TelegramError = type("TelegramError", (Exception,), {})
    errors.RetryAfter = type("RetryAfter", (errors.TelegramError,), {})
    sys.modules.update({
        "telegram": telegram,
        "telegram.constants": constants,
        "telegram.error": errors,
    })

from database import Database
from markdown_parser import parse_note
from syncer import ObsidianSyncer
from vault_parser import VaultNote, VaultStructure


def note(raw="Initial"):
    return VaultNote(
        parsed=parse_note("Alpha", raw), raw_content=raw,
        relative_path="Projects/Alpha.md", folder_path="Projects", stem="Alpha",
    )


class TopicReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "db.sqlite"))
        self.chat_id = -100123456
        self.bot = SimpleNamespace(
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=888)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321)),
            edit_message_text=AsyncMock(),
        )
        self.syncer = ObsidianSyncer(
            self.bot, self.db, SimpleNamespace(topic_icon_color=123)
        )
        self.db.save_topic(self.chat_id, "", 1, "📚 Vault")

    async def test_existing_topic_is_adopted_and_note_is_upserted(self):
        self.db.observe_topic(self.chat_id, 77, "Projects")
        vault = VaultStructure("Vault", notes=[note()], folders=["Projects"])
        await self.syncer._ensure_topics(self.chat_id, vault)
        self.assertEqual(self.db.get_topic(self.chat_id, "Projects"), 77)
        self.bot.create_forum_topic.assert_not_called()

        self.assertEqual(await self.syncer._sync_note(self.chat_id, note(), {}), "created")
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["message_thread_id"], 77
        )
        self.assertEqual(await self.syncer._sync_note(self.chat_id, note(), {}), "skipped")
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_not_awaited()

        self.assertEqual(
            await self.syncer._sync_note(self.chat_id, note("Changed"), {}), "updated"
        )
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 321)
        self.bot.send_message.assert_awaited_once()

    async def test_ambiguous_observed_topics_do_not_silently_create_duplicate(self):
        self.db.observe_topic(self.chat_id, 77, "Projects")
        self.db.observe_topic(self.chat_id, 78, "projects")
        vault = VaultStructure("Vault", folders=["Projects"])
        with self.assertRaisesRegex(ValueError, "/bindtopic"):
            await self.syncer._ensure_topics(self.chat_id, vault)
        self.bot.create_forum_topic.assert_not_called()

    async def test_existing_navigation_topic_is_reused(self):
        self.db.observe_topic(self.chat_id, 404, "🗂 Навигация")
        await self.syncer._sync_navigation(
            self.chat_id, VaultStructure("Vault"), link_map={}
        )
        self.assertEqual(self.db.get_navigation(self.chat_id)["topic_id"], 404)
        self.bot.create_forum_topic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
