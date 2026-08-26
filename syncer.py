"""
Core sync engine.

Orchestrates:
  1. Building the forum topic tree (one topic per folder)
  2. Building a note→message_link map for wikilink resolution
  3. Converting each note to Telegram HTML
  4. Sending new messages / editing changed ones
  5. Persisting the mapping in the database
"""
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from config import Config
from database import Database
from markdown_parser import (
    MAX_PART,
    ConversionContext,
    convert_to_telegram_html,
    resolve_wikilink,
)
from vault_parser import VaultNote, VaultStructure

logger = logging.getLogger(__name__)

# Telegram rate-limit: ~20 messages/second per chat, be conservative
SEND_DELAY = 0.35   # seconds between API calls
MAX_RETRIES = 3


@dataclass
class SyncProgress:
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class SyncResult:
    progress: SyncProgress
    vault_name: str
    error_notes: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Syncer
# ─────────────────────────────────────────────────────────────────────────────

class ObsidianSyncer:
    def __init__(self, bot: Bot, db: Database, cfg: Config) -> None:
        self.bot = bot
        self.db = db
        self.cfg = cfg

    # ── Public entry point ──────────────────────────────────────────────────

    async def sync(
        self,
        chat_id: int,
        vault: VaultStructure,
        progress_callback=None,   # async callable(SyncProgress) for live updates
    ) -> SyncResult:
        prog = SyncProgress(total=vault.note_count)
        errors: list[str] = []

        # 1. Ensure topics exist for every folder (+ root)
        await self._ensure_topics(chat_id, vault)

        # 2. First pass: send/update all notes using mappings already known
        #    from earlier syncs.  New notes may still have unresolved links;
        #    the second pass below fills those once their message IDs exist.
        logger.info("Pass 1: sending notes")
        initial_link_map = self._build_link_map(chat_id, vault)
        initial_results: dict[str, str] = {}
        for note in vault.notes:
            try:
                initial_results[note.relative_path] = await self._sync_note(
                    chat_id, note, link_map=initial_link_map
                )
            except Exception as exc:
                logger.exception("Error syncing note %s (pass 1)", note.relative_path)
                errors.append(note.relative_path)
                prog.errors += 1
            await asyncio.sleep(SEND_DELAY)

        # 3. Build wikilink map from the now-populated DB
        link_map = self._build_link_map(chat_id, vault)

        # 4. Second pass: re-send/edit notes that contain wikilinks
        notes_with_links = [n for n in vault.notes if n.parsed.wikilinks]
        logger.info(
            "Pass 2: resolving wikilinks for %d notes", len(notes_with_links)
        )
        final_results = dict(initial_results)
        for note in notes_with_links:
            try:
                result = await self._sync_note(chat_id, note, link_map=link_map)
                previous = initial_results.get(note.relative_path)
                # A freshly-created message remains "created" even if the
                # linking pass edits it once to add its final links.
                if previous == "created":
                    final_results[note.relative_path] = "created"
                elif result == "updated" or previous == "updated":
                    final_results[note.relative_path] = "updated"
                else:
                    final_results[note.relative_path] = "skipped"
            except Exception as exc:
                logger.exception("Error syncing note %s (pass 2)", note.relative_path)
                if note.relative_path not in errors:
                    errors.append(note.relative_path)
                prog.errors += 1

            await asyncio.sleep(SEND_DELAY)

        prog.created = sum(result == "created" for result in final_results.values())
        prog.updated = sum(result == "updated" for result in final_results.values())
        prog.skipped = sum(result == "skipped" for result in final_results.values())

        if progress_callback:
            await progress_callback(prog)

        # 5. Keep a dedicated navigation topic current.  It provides a
        # clickable vault tree and a compact text-based knowledge graph.
        try:
            await self._sync_navigation(chat_id, vault, link_map)
        except Exception:
            logger.exception("Failed to update navigation for chat %s", chat_id)

        self.db.update_sync_state(chat_id, vault.vault_name, vault.note_count)

        return SyncResult(progress=prog, vault_name=vault.vault_name, error_notes=errors)

    # ── Topics ──────────────────────────────────────────────────────────────

    async def _ensure_topics(self, chat_id: int, vault: VaultStructure) -> None:
        """Reuse mapped/observed topics before creating missing ones.

        The Bot API cannot enumerate all pre-existing forum topics. Topics
        observed through service updates or registered with /bindtopic can be
        matched by name, while mappings from previous syncs are authoritative.
        """
        all_folders = [""] + vault.folders
        names = {
            folder: _truncate(
                folder.split("/")[-1] if folder else f"📚 {vault.vault_name}",
                128,
            )
            for folder in all_folders
        }
        name_counts = Counter(name.casefold() for name in names.values())
        for folder in all_folders:
            existing = self.db.get_topic(chat_id, folder)
            if existing is not None:
                continue
            name = names[folder]

            # Topic service messages report the topic name/ID; unlike the
            # folders table, these entries do not necessarily have a path.
            candidates = [
                row for row in self.db.list_known_topics(chat_id)
                if row["folder_path"] is None
                and row["topic_name"].casefold() == name.casefold()
            ]
            if candidates:
                if len(candidates) > 1 or name_counts[name.casefold()] > 1:
                    raise ValueError(
                        f"Неоднозначная тема «{name}». Привяжите нужную "
                        f"тему командой /bindtopic {folder or '.'} внутри неё."
                    )
                found = candidates[0]
                self.db.save_topic(
                    chat_id, folder, found["topic_id"], found["topic_name"]
                )
                logger.info(
                    "Reusing observed topic '%s' (id=%d) for %s",
                    name, found["topic_id"], folder,
                )
                continue

            try:
                topic = await _retry(
                    self.bot.create_forum_topic,
                    chat_id=chat_id,
                    name=name,
                    icon_color=self.cfg.topic_icon_color,
                )
                self.db.save_topic(chat_id, folder, topic.message_thread_id, name)
                logger.info("Created topic '%s' (id=%d)", name, topic.message_thread_id)
            except TelegramError as exc:
                logger.error("Failed to create topic '%s': %s", name, exc)
            await asyncio.sleep(SEND_DELAY)

    # ── Notes ───────────────────────────────────────────────────────────────

    async def _sync_note(
        self, chat_id: int, note: VaultNote, link_map: dict[str, str]
    ) -> str:
        """Send or edit a single note. Returns 'created' | 'updated' | 'skipped'."""
        topic_id = self.db.get_topic(chat_id, note.folder_path)
        if topic_id is None:
            # Fallback: use root topic
            topic_id = self.db.get_topic(chat_id, "")

        ctx = ConversionContext(
            note_link_map=link_map,
            source_note_path=note.relative_path,
        )
        parts = convert_to_telegram_html(note.parsed, ctx)
        if not parts:
            parts = [f"<i>(пустая заметка)</i>"]

        # Header prepended to first part
        header = f"<b>📄 {_escape(note.parsed.title)}</b>\n\n"
        parts[0] = header + parts[0]

        existing = self.db.get_message(chat_id, note.relative_path)
        full_content = "\n\n".join(parts)

        if existing and not self.db.is_content_changed(chat_id, note.relative_path, full_content):
            return "skipped"

        if existing:
            # Edit the first message and resend continuation parts
            try:
                await _retry(
                    self.bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=existing["message_id"],
                    text=parts[0],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    return "skipped"
                raise
            self.db.save_message(
                chat_id, note.relative_path, topic_id,
                existing["message_id"], full_content, note.parsed.title,
            )
            return "updated"
        else:
            # Send first part and record message_id
            msg = await _retry(
                self.bot.send_message,
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=parts[0],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            self.db.save_message(
                chat_id, note.relative_path, topic_id,
                msg.message_id, full_content, note.parsed.title,
            )
            # Send continuation parts as replies
            for part in parts[1:]:
                await asyncio.sleep(SEND_DELAY)
                await _retry(
                    self.bot.send_message,
                    chat_id=chat_id,
                    message_thread_id=topic_id,
                    reply_to_message_id=msg.message_id,
                    text=part,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            return "created"

    # ── Navigation ─────────────────────────────────────────────────────────

    async def _sync_navigation(
        self,
        chat_id: int,
        vault: VaultStructure,
        link_map: dict[str, str],
    ) -> None:
        """Update the dedicated topic containing the vault tree and graph."""
        navigation = self.db.get_navigation(chat_id)
        if navigation:
            topic_id = navigation["topic_id"]
        else:
            candidates = [
                t for t in self.db.list_known_topics(chat_id)
                if t["topic_name"].casefold() == "🗂 навигация"
            ]
            if len(candidates) > 1:
                raise ValueError(
                    "Найдено несколько тем «🗂 Навигация»; "
                    "не создаю ещё одну автоматически."
                )
            if candidates:
                topic_id = candidates[0]["topic_id"]
            else:
                topic = await _retry(
                    self.bot.create_forum_topic,
                    chat_id=chat_id,
                    name="🗂 Навигация",
                    icon_color=self.cfg.topic_icon_color,
                )
                topic_id = topic.message_thread_id
                self.db.observe_topic(chat_id, topic_id, "🗂 Навигация")
                await asyncio.sleep(SEND_DELAY)
            self.db.save_navigation(chat_id, topic_id)
            navigation = self.db.get_navigation(chat_id) or {}

        tree_message_id = await self._upsert_navigation_message(
            chat_id=chat_id,
            topic_id=topic_id,
            message_id=navigation.get("tree_message_id"),
            text=_render_vault_tree(vault, link_map),
        )
        self.db.save_navigation(
            chat_id, topic_id, tree_message_id=tree_message_id
        )
        await asyncio.sleep(SEND_DELAY)

        graph_message_id = await self._upsert_navigation_message(
            chat_id=chat_id,
            topic_id=topic_id,
            message_id=navigation.get("graph_message_id"),
            text=_render_link_graph(vault, link_map),
        )
        self.db.save_navigation(
            chat_id, topic_id, graph_message_id=graph_message_id
        )

    async def _upsert_navigation_message(
        self,
        *,
        chat_id: int,
        topic_id: int,
        message_id: int | None,
        text: str,
    ) -> int:
        if message_id:
            try:
                await _retry(
                    self.bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return message_id
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    return message_id
                logger.warning("Recreating navigation message: %s", exc)

        message = await _retry(
            self.bot.send_message,
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return message.message_id

    # ── Link map ────────────────────────────────────────────────────────────

    def _build_link_map(
        self, chat_id: int, vault: VaultStructure | None = None
    ) -> dict[str, str]:
        """
        Build an index of note titles and paths to Telegram message links.

        Paths are indexed first because they are unambiguous.  Titles and
        filename stems are convenience fallbacks, mirroring how Obsidian
        resolves ``[[Note]]`` when there is no explicit folder in the link.
        """
        link_map: dict[str, str] = {}
        rows = self.db.get_all_messages(chat_id)
        rows_by_path = {row["note_path"]: row for row in rows}

        def add(key: str, url: str, *, overwrite: bool = False) -> None:
            normalized = key.strip().replace("\\", "/").casefold()
            if normalized and (overwrite or normalized not in link_map):
                link_map[normalized] = url

        # Prefer exact paths, including all entries still recorded from older
        # syncs, before adding the convenient (but potentially ambiguous)
        # title and stem aliases.
        for row in rows:
            # Telegram message link format for supergroups (no public username):
            # https://t.me/c/{chat_id_without_minus100}/{thread_id}/{message_id}
            # For topics: https://t.me/c/CHATID/TOPICID/MSGID
            pure_id = str(chat_id).replace("-100", "")
            url = (
                f"https://t.me/c/{pure_id}/{row['topic_id']}/{row['message_id']}"
            )
            note_path = row["note_path"]
            add(note_path, url, overwrite=True)
            if note_path.lower().endswith(".md"):
                add(note_path[:-3], url, overwrite=True)

        for row in rows:
            pure_id = str(chat_id).replace("-100", "")
            url = f"https://t.me/c/{pure_id}/{row['topic_id']}/{row['message_id']}"
            add(row["title"], url)
            stem = row["note_path"].rsplit("/", 1)[-1].removesuffix(".md")
            add(stem, url)

        # A freshly parsed vault gives us aliases and path spellings even
        # when a frontmatter title differs from the filename.
        if vault:
            for note in vault.notes:
                row = rows_by_path.get(note.relative_path)
                if not row:
                    continue
                pure_id = str(chat_id).replace("-100", "")
                url = f"https://t.me/c/{pure_id}/{row['topic_id']}/{row['message_id']}"
                add(note.relative_path, url, overwrite=True)
                add(note.relative_path.removesuffix(".md"), url, overwrite=True)
                add(note.parsed.title, url)
                aliases = note.parsed.frontmatter.get("aliases", "")
                for alias in aliases.strip("[]").split(","):
                    add(alias.strip().strip('"\''), url)
        return link_map


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _retry(func, *args, **kwargs):
    """Call a Telegram API function with exponential back-off on rate limits."""
    for attempt in range(MAX_RETRIES):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            wait = exc.retry_after + 1
            logger.warning("Rate limited, sleeping %ds", wait)
            await asyncio.sleep(wait)
        except TelegramError:
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Max retries exceeded")


def _truncate(text: str, max_len: int) -> str:
    return text[:max_len - 1] + "…" if len(text) > max_len else text


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# Keep room below Telegram's 4096-character limit for HTML entities.  A
# navigation message stays as one editable message; very large vaults receive
# the beginning of the tree plus an explicit overflow notice.
NAVIGATION_LIMIT = MAX_PART - 120


def _render_vault_tree(vault: VaultStructure, link_map: dict[str, str]) -> str:
    """Return a clickable, indented folder-and-note index for Telegram."""
    notes_by_folder: dict[str, list[VaultNote]] = {}
    for note in vault.notes:
        notes_by_folder.setdefault(note.folder_path, []).append(note)

    children: dict[str, list[str]] = {"": []}
    for folder in vault.folders:
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
        children.setdefault(parent, []).append(folder)
        children.setdefault(folder, [])

    lines = [
        f"<b>🗂 Дерево vault: {_escape(vault.vault_name)}</b>",
        "Нажмите на файл, чтобы перейти к сообщению.",
        "",
    ]

    def add_note(note: VaultNote, depth: int) -> None:
        context = ConversionContext(note_link_map=link_map)
        url = resolve_wikilink(note.relative_path, context)
        title = _escape(_truncate(note.parsed.title, 120))
        prefix = "  " * depth + "📄 "
        if url:
            lines.append(f'{prefix}<a href="{url}">{title}</a>')
        else:
            lines.append(f"{prefix}{title}")

    def render_folder(folder: str, depth: int) -> None:
        if folder:
            name = _escape(_truncate(folder.rsplit("/", 1)[-1], 100))
            lines.append(f'{"  " * depth}📁 <b>{name}</b>')
        note_depth = depth + 1 if folder else depth
        for note in sorted(notes_by_folder.get(folder, []), key=lambda n: n.parsed.title.casefold()):
            add_note(note, note_depth)
        child_depth = depth + 1 if folder else depth
        for child in sorted(children.get(folder, []), key=str.casefold):
            render_folder(child, child_depth)

    render_folder("", 0)
    return _limit_navigation_message(lines, "дерево")


def _render_link_graph(vault: VaultStructure, link_map: dict[str, str]) -> str:
    """Render the vault's links as a compact, clickable graph adjacency list."""
    lines = [
        "<b>🕸 Карта связей</b>",
        "Строка показывает исходящую связь: заметка → связанные заметки.",
        "",
    ]
    connection_count = 0
    for note in sorted(vault.notes, key=lambda n: n.parsed.title.casefold()):
        source_ctx = ConversionContext(
            note_link_map=link_map,
            source_note_path=note.relative_path,
        )
        source_url = resolve_wikilink(note.relative_path, source_ctx)
        targets: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for target in note.parsed.wikilinks:
            target_url = resolve_wikilink(target, source_ctx)
            if target_url and target_url not in seen_urls:
                seen_urls.add(target_url)
                label = target.rsplit("/", 1)[-1].removesuffix(".md")
                targets.append((_escape(_truncate(label, 80)), target_url))

        if not targets:
            continue
        connection_count += len(targets)
        source_label = _escape(_truncate(note.parsed.title, 80))
        source = (
            f'<a href="{source_url}">{source_label}</a>'
            if source_url else source_label
        )
        target_html = ", ".join(
            f'<a href="{url}">{label}</a>' for label, url in targets[:8]
        )
        if len(targets) > 8:
            target_html += f" +{len(targets) - 8}"
        lines.append(f"• {source} → {target_html}")

    if connection_count == 0:
        lines.append("Пока нет ссылок между заметками.")
    else:
        lines.insert(2, f"Связей: <b>{connection_count}</b>")
    return _limit_navigation_message(lines, "карта связей")


def _limit_navigation_message(lines: list[str], label: str) -> str:
    """Bound a generated index without cutting an HTML tag or a link."""
    kept: list[str] = []
    current_length = 0
    overflow = f"\n<i>… {label} сокращён: откройте нужную тему для полного списка.</i>"
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if current_length + addition + len(overflow) > NAVIGATION_LIMIT:
            return "\n".join(kept) + overflow
        kept.append(line)
        current_length += addition
    return "\n".join(kept)
