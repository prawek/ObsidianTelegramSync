"""
SQLite persistence layer.

Tables
------
topics      — folder path  → Telegram forum topic ID
known_topics — forum topics observed by the bot (including manually created ones)
messages    — note path    → Telegram message ID + topic ID + content hash
sync_state  — per-group metadata (last sync time, vault root name, etc.)
"""
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL,
                    folder_path TEXT    NOT NULL,
                    topic_id    INTEGER NOT NULL,
                    topic_name  TEXT    NOT NULL,
                    created_at  TEXT    DEFAULT (datetime('now')),
                    UNIQUE(chat_id, folder_path)
                );

                CREATE TABLE IF NOT EXISTS known_topics (
                    chat_id     INTEGER NOT NULL,
                    topic_id    INTEGER NOT NULL,
                    topic_name  TEXT    NOT NULL,
                    PRIMARY KEY (chat_id, topic_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      INTEGER NOT NULL,
                    note_path    TEXT    NOT NULL,
                    topic_id     INTEGER NOT NULL,
                    message_id   INTEGER NOT NULL,
                    content_hash TEXT    NOT NULL,
                    title        TEXT    NOT NULL,
                    updated_at   TEXT    DEFAULT (datetime('now')),
                    UNIQUE(chat_id, note_path)
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    chat_id      INTEGER PRIMARY KEY,
                    vault_name   TEXT,
                    last_sync_at TEXT,
                    note_count   INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS navigation (
                    chat_id          INTEGER PRIMARY KEY,
                    topic_id         INTEGER NOT NULL,
                    tree_message_id  INTEGER,
                    graph_message_id INTEGER,
                    updated_at       TEXT DEFAULT (datetime('now'))
                );
                """
            )
            # Keep compatibility with databases created before known_topics
            # existed. A topic already mapped to a folder is also a known topic.
            conn.execute(
                """INSERT OR IGNORE INTO known_topics (chat_id, topic_id, topic_name)
                   SELECT chat_id, topic_id, topic_name FROM topics"""
            )

    # ------------------------------------------------------------------ #
    # Topics
    # ------------------------------------------------------------------ #

    def get_topic(self, chat_id: int, folder_path: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT topic_id FROM topics WHERE chat_id=? AND folder_path=?",
                (chat_id, folder_path),
            ).fetchone()
        return row["topic_id"] if row else None

    def save_topic(
        self, chat_id: int, folder_path: str, topic_id: int, topic_name: str
    ) -> None:
        with self._conn() as conn:
            # One Telegram topic must not silently be mapped to two folders.
            conn.execute(
                """DELETE FROM topics WHERE chat_id=? AND topic_id=?
                   AND folder_path<>?""",
                (chat_id, topic_id, folder_path),
            )
            conn.execute(
                """
                INSERT INTO topics (chat_id, folder_path, topic_id, topic_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, folder_path)
                DO UPDATE SET topic_id=excluded.topic_id, topic_name=excluded.topic_name
                """,
                (chat_id, folder_path, topic_id, topic_name),
            )
            conn.execute(
                """INSERT INTO known_topics (chat_id, topic_id, topic_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id, topic_id)
                   DO UPDATE SET topic_name=excluded.topic_name""",
                (chat_id, topic_id, topic_name),
            )

    def observe_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        """Remember a topic announced by Telegram, without choosing a folder."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO known_topics (chat_id, topic_id, topic_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id, topic_id)
                   DO UPDATE SET topic_name=excluded.topic_name""",
                (chat_id, topic_id, name),
            )

    def list_known_topics(self, chat_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT k.topic_id, k.topic_name, t.folder_path
                   FROM known_topics AS k LEFT JOIN topics AS t
                   ON t.chat_id=k.chat_id AND t.topic_id=k.topic_id
                   WHERE k.chat_id=? ORDER BY k.topic_id""",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_topics(self, chat_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT folder_path, topic_id, topic_name FROM topics WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    def get_message(self, chat_id: int, note_path: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT topic_id, message_id, content_hash, title
                   FROM messages WHERE chat_id=? AND note_path=?""",
                (chat_id, note_path),
            ).fetchone()
        return dict(row) if row else None

    def save_message(
        self,
        chat_id: int,
        note_path: str,
        topic_id: int,
        message_id: int,
        content: str,
        title: str,
    ) -> None:
        h = content_hash(content)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages (chat_id, note_path, topic_id, message_id, content_hash, title)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, note_path)
                DO UPDATE SET
                    topic_id=excluded.topic_id,
                    message_id=excluded.message_id,
                    content_hash=excluded.content_hash,
                    title=excluded.title,
                    updated_at=datetime('now')
                """,
                (chat_id, note_path, topic_id, message_id, h, title),
            )

    def get_message_by_title(self, chat_id: int, title: str) -> dict | None:
        """Look up a message by note title (for resolving [[wikilinks]])."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT note_path, topic_id, message_id
                   FROM messages WHERE chat_id=? AND title=?
                   LIMIT 1""",
                (chat_id, title),
            ).fetchone()
        return dict(row) if row else None

    def get_all_messages(self, chat_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note_path, topic_id, message_id, title FROM messages WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def is_content_changed(self, chat_id: int, note_path: str, content: str) -> bool:
        existing = self.get_message(chat_id, note_path)
        if not existing:
            return True
        return existing["content_hash"] != content_hash(content)

    # ------------------------------------------------------------------ #
    # Navigation topic
    # ------------------------------------------------------------------ #

    def get_navigation(self, chat_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT topic_id, tree_message_id, graph_message_id
                   FROM navigation WHERE chat_id=?""",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_navigation(
        self,
        chat_id: int,
        topic_id: int,
        tree_message_id: int | None = None,
        graph_message_id: int | None = None,
    ) -> None:
        """Persist the navigation topic while retaining omitted message IDs."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO navigation (
                    chat_id, topic_id, tree_message_id, graph_message_id, updated_at
                ) VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(chat_id) DO UPDATE SET
                    topic_id=excluded.topic_id,
                    tree_message_id=COALESCE(excluded.tree_message_id,
                                             navigation.tree_message_id),
                    graph_message_id=COALESCE(excluded.graph_message_id,
                                              navigation.graph_message_id),
                    updated_at=datetime('now')
                """,
                (chat_id, topic_id, tree_message_id, graph_message_id),
            )

    # ------------------------------------------------------------------ #
    # Sync state
    # ------------------------------------------------------------------ #

    def update_sync_state(
        self, chat_id: int, vault_name: str, note_count: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (chat_id, vault_name, last_sync_at, note_count)
                VALUES (?, ?, datetime('now'), ?)
                ON CONFLICT(chat_id)
                DO UPDATE SET
                    vault_name=excluded.vault_name,
                    last_sync_at=datetime('now'),
                    note_count=excluded.note_count
                """,
                (chat_id, vault_name, note_count),
            )

    def get_sync_state(self, chat_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------------ #
# Utility
# ------------------------------------------------------------------ #


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
