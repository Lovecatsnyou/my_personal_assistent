"""SQLite storage. Every task, note, reminder and weather city operation is scoped to its owner's Telegram ID."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent / "assistant.db"
MAX_TITLE_LENGTH = 300
PAGE_SIZE = 5
MAX_ID = 2**63 - 1
MAX_NOTE_TITLE_LENGTH = 200
MAX_NOTE_CONTENT_LENGTH = 4000
MAX_SEARCH_LENGTH = 200
NOTE_COLUMNS = frozenset({"id", "user_id", "title", "content", "created_at", "updated_at"})
REMINDER_COLUMNS = frozenset({
    "id", "user_id", "text", "timezone", "local_date", "local_time", "fold", "repeat", "repeat_days",
    "next_run_utc", "status", "version", "created_at", "updated_at", "last_attempt_at", "last_sent_at",
    "error_count", "last_error",
})
SETTINGS_COLUMNS = frozenset({"user_id", "timezone", "created_at", "updated_at"})
LOCATION_COLUMNS = frozenset({
    "user_id", "city_name", "country", "region", "latitude", "longitude", "timezone", "geocoding_id",
    "created_at", "updated_at",
})


@dataclass(frozen=True)
class Task:
    id: int
    user_id: int
    title: str
    completed: bool
    created_at: str


@dataclass(frozen=True)
class TaskPage:
    tasks: tuple[Task, ...]
    page: int
    pages: int
    total: int


@dataclass(frozen=True)
class Note:
    id: int
    user_id: int
    title: str
    content: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class NotePage:
    notes: tuple[Note, ...]
    page: int
    pages: int
    total: int


def fold_text(text: str | None) -> str:
    """Case-insensitive form for search: SQLite LOWER() ignores Cyrillic, ё matches е."""
    return (text or "").casefold().replace("ё", "е")


def clean_note_title(title: str) -> str:
    title = " ".join(title.split())
    if not title or len(title) > MAX_NOTE_TITLE_LENGTH:
        raise ValueError(f"Заголовок должен содержать от 1 до {MAX_NOTE_TITLE_LENGTH} символов.")
    return title


def clean_note_content(content: str) -> str:
    content = content.strip()
    if not content or len(content) > MAX_NOTE_CONTENT_LENGTH:
        raise ValueError(f"Текст заметки должен содержать от 1 до {MAX_NOTE_CONTENT_LENGTH} символов.")
    return content


def clean_search_query(query: str) -> str:
    query = " ".join(query.split())
    if not query or len(query) > MAX_SEARCH_LENGTH:
        raise ValueError(f"Поисковый запрос должен содержать от 1 до {MAX_SEARCH_LENGTH} символов.")
    return query


class Database:
    def __init__(self, path: str | Path = DB_PATH) -> None:
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # New connections allow calls from asyncio.to_thread without sharing cursors.
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.create_function("fold_text", 1, fold_text, deterministic=True)
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create missing objects; never drop tables or overwrite existing records."""
        with self.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 300),
                    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id, id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
                    content TEXT NOT NULL CHECK(length(trim(content)) BETWEEN 1 AND 4000),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                    timezone TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # local_date/local_time/fold/timezone keep the original wall-clock anchor;
            # next_run_utc is derived from it. version changes on every user edit.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 500),
                    timezone TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    local_time TEXT NOT NULL,
                    fold INTEGER NOT NULL DEFAULT 0 CHECK(fold IN (0, 1)),
                    repeat TEXT NOT NULL CHECK(repeat IN ('once', 'daily', 'weekly', 'monthly', 'weekdays', 'hourly')),
                    repeat_days TEXT NOT NULL DEFAULT '',
                    next_run_utc TEXT,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'done', 'failed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_attempt_at TEXT,
                    last_sent_at TEXT,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """)
            # The weather city and its zone are separate from user_settings.timezone (reminders).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_locations (
                    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                    city_name TEXT NOT NULL CHECK(length(trim(city_name)) BETWEEN 1 AND 100),
                    country TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                    timezone TEXT NOT NULL DEFAULT '',
                    geocoding_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for table, required in (("notes", NOTE_COLUMNS), ("user_settings", SETTINGS_COLUMNS),
                                    ("reminders", REMINDER_COLUMNS), ("user_locations", LOCATION_COLUMNS)):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    # An older table must be inspected by hand, not silently altered.
                    raise RuntimeError(f"Таблица {table} в БД имеет другую структуру. Смотри README.md")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_user_id ON notes(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, next_run_utc)")

    def upsert_user(
        self, telegram_id: int, first_name: str, username: str | None = None,
        last_name: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute("""
                INSERT INTO users (telegram_id, first_name, username, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username = excluded.username,
                    last_name = excluded.last_name,
                    updated_at = CURRENT_TIMESTAMP
            """, (telegram_id, first_name, username, last_name))

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(row["id"], row["user_id"], row["title"],
                    bool(row["completed"]), row["created_at"])

    def add_task(self, user_id: int, title: str) -> int:
        title = title.strip()
        if not title or len(title) > MAX_TITLE_LENGTH:
            raise ValueError(f"Название должно содержать от 1 до {MAX_TITLE_LENGTH} символов.")
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (user_id, title) VALUES (?, ?)", (user_id, title),
            )
            return cursor.lastrowid

    def get_task(self, user_id: int, task_id: int) -> Task | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id),
            ).fetchone()
            return self._task(row) if row else None

    def list_tasks(self, user_id: int, page: int = 0) -> TaskPage:
        with self.connection() as conn:
            # A single read transaction keeps count and rows consistent.
            conn.execute("BEGIN")
            total = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE user_id = ?", (user_id,),
            ).fetchone()[0]
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (user_id, PAGE_SIZE, page * PAGE_SIZE),
            ).fetchall()
            return TaskPage(tuple(self._task(row) for row in rows), page, pages, total)

    def complete_task(self, user_id: int, task_id: int) -> bool:
        with self.connection() as conn:
            return conn.execute("""
                UPDATE tasks SET completed = 1, completed_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND id = ? AND completed = 0
            """, (user_id, task_id)).rowcount == 1

    def delete_task(self, user_id: int, task_id: int) -> bool:
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id),
            ).rowcount == 1

    @staticmethod
    def _note(row: sqlite3.Row) -> Note:
        return Note(row["id"], row["user_id"], row["title"], row["content"],
                    row["created_at"], row["updated_at"])

    def _note_page(self, user_id: int, page: int, query: str | None = None) -> NotePage:
        where, params = "user_id = ?", [user_id]
        if query is not None:
            # instr() treats % and _ literally, unlike LIKE.
            where += " AND (instr(fold_text(title), ?) > 0 OR instr(fold_text(content), ?) > 0)"
            params += [fold_text(query)] * 2
        with self.connection() as conn:
            conn.execute("BEGIN")
            total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", params).fetchone()[0]
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            rows = conn.execute(
                f"SELECT * FROM notes WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, page * PAGE_SIZE),
            ).fetchall()
            return NotePage(tuple(self._note(row) for row in rows), page, pages, total)

    def add_note(self, user_id: int, title: str, content: str) -> int:
        title, content = clean_note_title(title), clean_note_content(content)
        with self.connection() as conn:
            return conn.execute(
                "INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)", (user_id, title, content),
            ).lastrowid

    def get_note(self, user_id: int, note_id: int) -> Note | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE user_id = ? AND id = ?", (user_id, note_id),
            ).fetchone()
            return self._note(row) if row else None

    def list_notes(self, user_id: int, page: int = 0) -> NotePage:
        return self._note_page(user_id, page)

    def search_notes(self, user_id: int, query: str, page: int = 0) -> NotePage:
        return self._note_page(user_id, page, clean_search_query(query))

    def update_note(
        self, user_id: int, note_id: int, *, title: str | None = None, content: str | None = None,
    ) -> bool:
        changes = {}
        if title is not None:
            changes["title"] = clean_note_title(title)
        if content is not None:
            changes["content"] = clean_note_content(content)
        if not changes:
            raise ValueError("Нечего изменять.")
        # Column names come from the fixed keys above; values stay parameterized.
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.connection() as conn:
            return conn.execute(
                f"UPDATE notes SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
                (*changes.values(), user_id, note_id),
            ).rowcount == 1

    def delete_note(self, user_id: int, note_id: int) -> bool:
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM notes WHERE user_id = ? AND id = ?", (user_id, note_id),
            ).rowcount == 1
