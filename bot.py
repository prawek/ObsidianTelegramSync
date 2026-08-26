"""
Telegram bot handlers.

Commands
--------
/start          — greeting & instructions
/help           — same as start
/setsupergroup  — (sent in supergroup) register THIS chat as the sync target
/sync           — re-sync the last uploaded vault
/upload         — instructions for uploading a vault ZIP
/status         — show last sync info
/topics         — list all created topics
/bindtopic      — bind the current Telegram topic to an Obsidian folder
/bindnote       — bind an existing bot message to an Obsidian note

The user uploads their Obsidian vault as a .zip file to the bot (in DM or in
the supergroup).  The bot stores the ZIP on disk, parses it, and syncs it.
"""
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import Config
from database import Database
from syncer import ObsidianSyncer, SyncProgress
from vault_parser import parse_vault_zip_bytes

logger = logging.getLogger(__name__)

# Key used to store per-chat data in bot_data
VAULT_ZIP_KEY = "vault_zip_{chat_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_application(cfg: Config, db: Database) -> Application:
    app = Application.builder().token(cfg.bot_token).build()

    # Store shared deps
    app.bot_data["cfg"] = cfg
    app.bot_data["db"] = db

    # Check every update before commands, uploads, or topic observations run.
    app.add_handler(TypeHandler(Update, check_access, block=True), group=-1)

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("setsupergroup", cmd_set_supergroup))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("upload", cmd_upload_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("bindtopic", cmd_bind_topic))
    app.add_handler(CommandHandler("bindnote", cmd_bind_note))
    app.add_handler(CommandHandler("tree", cmd_tree))
    app.add_handler(CommandHandler("graph", cmd_graph))

    # Telegram sends these service messages for topics created or renamed
    # while the bot is present. This is the only automatic name-discovery
    # available via Bot API; it cannot list older forum topics/history.
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED
            | filters.StatusUpdate.FORUM_TOPIC_EDITED,
            observe_forum_topic,
        )
    )

    # File upload handler (ZIP documents)
    app.add_handler(
        MessageHandler(filters.Document.MimeType("application/zip"), handle_zip_upload)
    )
    app.add_handler(
        MessageHandler(
            filters.Document.FileExtension("zip"),
            handle_zip_upload,
        )
    )

    return app


async def check_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow only configured users speaking under their own Telegram identity."""
    cfg: Config = ctx.bot_data["cfg"]
    user = update.effective_user
    message = update.effective_message
    if (user is not None and user.id in cfg.allowed_user_ids
            and not (message and message.sender_chat)):
        return

    # Ignore unrelated group traffic without replying to every message.
    if message and (message.document or (message.text or "").startswith("/")):
        try:
            await message.reply_text("⛔ Доступ к боту ограничен владельцем.")
        finally:
            # A failed denial reply must never let subsequent handlers run.
            raise ApplicationHandlerStop
    raise ApplicationHandlerStop


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>Obsidian → Telegram Sync Bot</b>\n\n"
        "Я синхронизирую ваш Obsidian vault в форум-темы супергруппы.\n\n"
        "<b>Быстрый старт:</b>\n"
        "1️⃣ Добавьте меня в вашу супергруппу как администратора\n"
        "   (нужно: управление темами, отправка сообщений)\n"
        "2️⃣ В супергруппе отправьте /setsupergroup\n"
        "3️⃣ Сюда (в личку) отправьте ZIP-архив вашего vault\n"
        "4️⃣ Синхронизация запустится автоматически\n\n"
        "<b>Команды:</b>\n"
        "/setsupergroup — зарегистрировать супергруппу\n"
        "/sync — повторная синхронизация\n"
        "/status — статус последней синхронизации\n"
        "/topics — список созданных тем\n"
        "/bindtopic путь/к/папке — связать текущую тему с папкой\n"
        "/bindnote путь/к/заметке.md — ответом на старое сообщение бота\n"
        "/tree — открыть дерево папок и файлов\n"
        "/graph — открыть карту связей заметок\n"
        "/upload — как загрузить vault\n"
    )
    await update.message.reply_html(text)


async def cmd_set_supergroup(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Register the current chat as the sync target supergroup."""
    chat = update.effective_chat
    if chat.type not in ("supergroup", "group"):
        await update.message.reply_text(
            "❌ Эту команду нужно отправить прямо в супергруппе."
        )
        return

    # Verify the bot can create topics
    try:
        chat_info = await ctx.bot.get_chat(chat.id)
        if not getattr(chat_info, "is_forum", False):
            await update.message.reply_html(
                "⚠️ Эта группа не является форумом.\n"
                "Включите <b>Темы</b> в настройках группы и попробуйте снова."
            )
            return
    except Exception:
        pass

    cfg: Config = ctx.bot_data["cfg"]
    # Persist supergroup ID to environment-like storage (bot_data for this run
    # + write back to a simple file for persistence across restarts)
    ctx.bot_data["supergroup_id"] = chat.id
    _persist_supergroup_id(chat.id)

    await update.message.reply_html(
        f"✅ Супергруппа зарегистрирована!\n"
        f"Chat ID: <code>{chat.id}</code>\n\n"
        f"Теперь отправьте мне ZIP-архив вашего Obsidian vault в личные сообщения."
    )
    logger.info("Supergroup set to %d", chat.id)


async def cmd_upload_help(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_html(
        "📦 <b>Как загрузить vault:</b>\n\n"
        "1. Найдите папку вашего Obsidian vault на компьютере\n"
        "2. Сожмите её в ZIP-архив\n"
        "   • macOS: ПКМ → Сжать\n"
        "   • Windows: ПКМ → Отправить → Сжатая папка\n"
        "3. Отправьте этот файл сюда как документ (не фото!)\n\n"
        "Синхронизация запустится автоматически."
    )


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run sync using the last uploaded vault ZIP."""
    chat_id = update.effective_chat.id
    supergroup_id = _get_supergroup_id(ctx)

    if not supergroup_id:
        await update.message.reply_html(
            "❌ Супергруппа не зарегистрирована.\n"
            "Добавьте бота в супергруппу и отправьте там /setsupergroup"
        )
        return

    zip_path = _vault_zip_path(chat_id)
    if not zip_path.exists():
        await update.message.reply_html(
            "❌ Vault не найден. Сначала загрузите ZIP-архив вашего vault."
        )
        return

    await _run_sync(update, ctx, zip_path, supergroup_id)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    supergroup_id = _get_supergroup_id(ctx)
    if not supergroup_id:
        await update.message.reply_text("Супергруппа не зарегистрирована.")
        return

    state = db.get_sync_state(supergroup_id)
    if not state:
        await update.message.reply_text("Синхронизация ещё не выполнялась.")
        return

    await update.message.reply_html(
        f"📊 <b>Статус синхронизации</b>\n\n"
        f"Vault: <code>{state['vault_name']}</code>\n"
        f"Заметок: <b>{state['note_count']}</b>\n"
        f"Последняя синхронизация: <code>{state['last_sync_at']}</code>"
    )


async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    supergroup_id = _get_supergroup_id(ctx)
    if not supergroup_id:
        await update.message.reply_text("Супергруппа не зарегистрирована.")
        return

    topics = db.list_topics(supergroup_id)
    if not topics:
        await update.message.reply_text("Темы ещё не созданы.")
        return

    lines = ["📋 <b>Созданные темы:</b>\n"]
    for t in topics[:50]:  # cap at 50 for readability
        folder = t["folder_path"] or "(корень)"
        lines.append(f"• {folder} → тема #{t['topic_id']}")

    if len(topics) > 50:
        lines.append(f"\n…и ещё {len(topics) - 50} тем")

    await update.message.reply_html("\n".join(lines))


async def _can_bind_in_forum(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Only group admins may change mappings, from within the target forum."""
    chat = update.effective_chat
    message = update.effective_message
    if (not chat or chat.id != _get_supergroup_id(ctx)
            or chat.type != "supergroup"
            or not message or not message.is_topic_message
            or message.message_thread_id is None):
        if message:
            await message.reply_text(
                "❌ Выполните команду внутри нужной темы зарегистрированной супергруппы."
            )
        return False

    if update.effective_user is None:
        await message.reply_text(
            "❌ Не могу проверить анонимного администратора. Отправьте команду от своего имени."
        )
        return False

    try:
        member = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            await message.reply_text("❌ Привязывать темы и заметки может только администратор.")
            return False
    except Exception:
        await message.reply_text("❌ Не удалось проверить права администратора.")
        return False
    return True


async def cmd_bind_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Associate the current pre-existing topic with an Obsidian folder."""
    if not await _can_bind_in_forum(update, ctx):
        return
    folder = " ".join(ctx.args).strip().replace("\\", "/")
    if not folder:
        await update.message.reply_text(
            "Использование: /bindtopic Проекты/Идеи (или /bindtopic . для корня)."
        )
        return
    folder = "" if folder == "." else folder.strip("/")
    topic_id = update.message.message_thread_id
    name = folder.rsplit("/", 1)[-1] if folder else "📚 Корень"
    db: Database = ctx.bot_data["db"]
    db.save_topic(update.effective_chat.id, folder, topic_id, name)
    await update.message.reply_text(
        f"✅ Эта тема #{topic_id} привязана к папке {folder or '(корень)'}. "
        "Новые заметки попадут сюда при следующей загрузке ZIP."
    )


async def cmd_bind_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Adopt a previous bot message without replacing it with a duplicate."""
    if not await _can_bind_in_forum(update, ctx):
        return
    path = " ".join(ctx.args).strip().replace("\\", "/").strip("/")
    original = update.message.reply_to_message
    if not path or not path.lower().endswith(".md") or not original:
        await update.message.reply_text(
            "Использование: ответьте командой /bindnote Папка/Заметка.md "
            "на сообщение, ранее опубликованное этим ботом."
        )
        return
    bot_user = await ctx.bot.get_me()
    if not original.from_user or original.from_user.id != bot_user.id:
        await update.message.reply_text(
            "❌ Могу редактировать только сообщения, опубликованные этим ботом."
        )
        return

    db: Database = ctx.bot_data["db"]
    chat_id = update.effective_chat.id
    existing = db.get_message(chat_id, path)
    if existing and existing["message_id"] != original.message_id:
        await update.message.reply_text(
            "❌ У этой заметки уже есть другое сообщение. Привязка не изменена."
        )
        return
    # Empty content hash intentionally forces an in-place edit on next sync.
    db.save_message(
        chat_id, path, update.message.message_thread_id,
        original.message_id, "", path.rsplit("/", 1)[-1][:-3],
    )
    await update.message.reply_text(
        f"✅ Заметка {path} привязана к сообщению #{original.message_id}. "
        "При следующем импорте оно будет отредактировано, а не продублировано."
    )


async def observe_forum_topic(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record service events about topics so sync can reuse them by name."""
    chat = update.effective_chat
    message = update.effective_message
    if (not chat or chat.type != "supergroup" or not message
            or message.message_thread_id is None):
        return
    event = message.forum_topic_created or message.forum_topic_edited
    name = getattr(event, "name", None)
    if name:
        db: Database = ctx.bot_data["db"]
        db.observe_topic(chat.id, message.message_thread_id, name)


async def cmd_tree(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Link the user to the automatically maintained navigation topic."""
    await _reply_with_navigation_link(update, ctx, "🗂 Дерево папок и файлов")


async def cmd_graph(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Link the user to the automatically maintained link map."""
    await _reply_with_navigation_link(update, ctx, "🕸 Карта связей заметок")


# ─────────────────────────────────────────────────────────────────────────────
# File upload handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_zip_upload(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle a ZIP file upload — save it and trigger sync."""
    supergroup_id = _get_supergroup_id(ctx)
    if not supergroup_id:
        await update.message.reply_html(
            "❌ Сначала зарегистрируйте супергруппу.\n"
            "Добавьте меня туда и отправьте /setsupergroup"
        )
        return

    msg = await update.message.reply_html("⏳ Загружаю архив…")
    doc = update.message.document
    user_chat_id = update.effective_chat.id

    try:
        file = await ctx.bot.get_file(doc.file_id)
        zip_data = await file.download_as_bytearray()
    except Exception as exc:
        logger.exception("Failed to download vault zip")
        await msg.edit_text(f"❌ Ошибка загрузки: {exc}")
        return

    # Save the ZIP for future /sync calls
    zip_path = _vault_zip_path(user_chat_id)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(bytes(zip_data))
    await msg.edit_text("✅ Архив получен. Начинаю синхронизацию…")

    await _run_sync(update, ctx, zip_path, supergroup_id, status_msg=msg)


# ─────────────────────────────────────────────────────────────────────────────
# Core sync runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_sync(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    zip_path: Path,
    supergroup_id: int,
    status_msg=None,
) -> None:
    db: Database = ctx.bot_data["db"]
    cfg: Config = ctx.bot_data["cfg"]

    async def _edit(text: str):
        if status_msg:
            try:
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    await _edit("📂 Разбираю структуру vault…")

    try:
        zip_data = zip_path.read_bytes()
        vault = parse_vault_zip_bytes(zip_data, zip_path.stem)
    except Exception as exc:
        logger.exception("Failed to parse vault")
        await _edit(f"❌ Ошибка чтения vault: {exc}")
        return

    await _edit(
        f"📚 Vault: <b>{vault.vault_name}</b>\n"
        f"Заметок: {vault.note_count} | Папок: {len(vault.folders)}\n\n"
        f"⏳ Синхронизирую…"
    )

    syncer = ObsidianSyncer(ctx.bot, db, cfg)

    last_update_count = 0

    async def progress_cb(prog: SyncProgress):
        nonlocal last_update_count
        done = prog.created + prog.updated + prog.skipped
        if done - last_update_count >= 10:
            last_update_count = done
            await _edit(
                f"⏳ Синхронизация…\n"
                f"✅ Создано: {prog.created} | 🔄 Обновлено: {prog.updated} "
                f"| ⏭ Без изменений: {prog.skipped}\n"
                f"Из {prog.total} заметок"
            )

    try:
        result = await syncer.sync(supergroup_id, vault, progress_callback=progress_cb)
    except Exception as exc:
        logger.exception("Sync failed")
        await _edit(f"❌ Ошибка синхронизации: {exc}")
        return

    p = result.progress
    summary_lines = [
        f"🎉 <b>Синхронизация завершена!</b>\n",
        f"Vault: <code>{result.vault_name}</code>",
        f"✅ Создано: {p.created}",
        f"🔄 Обновлено: {p.updated}",
        f"⏭ Без изменений: {p.skipped}",
    ]
    if p.errors:
        summary_lines.append(f"❌ Ошибок: {p.errors}")
    if result.error_notes:
        errs = "\n".join(result.error_notes[:5])
        summary_lines.append(f"\nПроблемные заметки:\n<code>{errs}</code>")

    await _edit("\n".join(summary_lines))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

SUPERGROUP_FILE = Path("data/supergroup_id.txt")


async def _reply_with_navigation_link(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, label: str
) -> None:
    supergroup_id = _get_supergroup_id(ctx)
    if not supergroup_id:
        await update.message.reply_text("Супергруппа не зарегистрирована.")
        return

    db: Database = ctx.bot_data["db"]
    navigation = db.get_navigation(supergroup_id)
    if not navigation:
        await update.message.reply_html(
            "Навигация появится после первой успешной синхронизации."
        )
        return

    pure_id = str(supergroup_id).replace("-100", "")
    url = f"https://t.me/c/{pure_id}/{navigation['topic_id']}"
    await update.message.reply_html(f'<a href="{url}">{label}</a>')


def _persist_supergroup_id(chat_id: int) -> None:
    SUPERGROUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUPERGROUP_FILE.write_text(str(chat_id))


def _get_supergroup_id(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    # Check bot_data first (in-memory)
    sid = ctx.bot_data.get("supergroup_id")
    if sid:
        return int(sid)
    # Check config
    cfg: Config = ctx.bot_data.get("cfg")
    if cfg and cfg.supergroup_id:
        return cfg.supergroup_id
    # Check persisted file
    if SUPERGROUP_FILE.exists():
        try:
            return int(SUPERGROUP_FILE.read_text().strip())
        except ValueError:
            pass
    return None


def _vault_zip_path(user_chat_id: int) -> Path:
    return Path(f"data/vaults/{user_chat_id}.zip")
