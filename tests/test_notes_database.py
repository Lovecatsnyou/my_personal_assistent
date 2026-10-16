from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import (
    Database, MAX_NOTE_CONTENT_LENGTH, MAX_NOTE_TITLE_LENGTH, MAX_SEARCH_LENGTH, PAGE_SIZE,
)


class NoteDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.initialize()
        self.db.upsert_user(101, "Аня")
        self.db.upsert_user(202, "Борис")

    def test_schema_has_required_columns(self):
        with self.db.connection() as conn:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(notes)")]
        self.assertEqual(columns, ["id", "user_id", "title", "content", "created_at", "updated_at"])

    def test_initialize_keeps_existing_tasks_and_notes(self):
        task_id = self.db.add_task(101, "Задача")
        note_id = self.db.add_note(101, "Заметка", "Текст")
        reopened = Database(self.db.path)
        reopened.initialize()
        reopened.initialize()
        self.assertEqual(reopened.get_task(101, task_id).title, "Задача")
        self.assertEqual(reopened.get_note(101, note_id).content, "Текст")

    def test_initialize_adds_notes_to_database_without_them(self):
        path = Path(self.temp.name) / "old.db"
        old = Database(path)
        with old.connection() as conn:
            conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, "
                         "first_name TEXT NOT NULL, last_name TEXT, created_at TEXT NOT NULL "
                         "DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO users (telegram_id, first_name) VALUES (1, 'Старый')")
        old.initialize()
        with old.connection() as conn:
            self.assertEqual(conn.execute("SELECT first_name FROM users").fetchone()[0], "Старый")
        self.assertEqual(old.list_notes(1).total, 0)

    def test_incompatible_notes_table_is_reported_not_changed(self):
        path = Path(self.temp.name) / "legacy.db"
        legacy = Database(path)
        with legacy.connection() as conn:
            conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT)")
            conn.execute("INSERT INTO notes (user_id, text) VALUES (1, 'старое')")
        with self.assertRaises(RuntimeError):
            legacy.initialize()
        with legacy.connection() as conn:
            self.assertEqual(conn.execute("SELECT text FROM notes").fetchone()[0], "старое")

    def test_create_and_read(self):
        note_id = self.db.add_note(101, "  Список\n покупок ", "  Молоко\nХлеб 🥖  ")
        note = self.db.get_note(101, note_id)
        self.assertEqual((note.title, note.content, note.user_id), ("Список покупок", "Молоко\nХлеб 🥖", 101))
        self.assertTrue(note.created_at and note.updated_at)

    def test_limits(self):
        self.db.add_note(101, "😀" * MAX_NOTE_TITLE_LENGTH, "😀" * MAX_NOTE_CONTENT_LENGTH)
        for title, content in (("", "x"), (" \n", "x"), ("а" * (MAX_NOTE_TITLE_LENGTH + 1), "x"),
                               ("x", ""), ("x", "  \n"), ("x", "а" * (MAX_NOTE_CONTENT_LENGTH + 1))):
            with self.subTest(title=len(title), content=len(content)), self.assertRaises(ValueError):
                self.db.add_note(101, title, content)
        with self.assertRaises(ValueError):
            self.db.search_notes(101, "а" * (MAX_SEARCH_LENGTH + 1))
        with self.assertRaises(ValueError):
            self.db.search_notes(101, "  ")
        self.assertEqual(self.db.list_notes(101).total, 1)

    def test_update_changes_fields_and_updated_at(self):
        note_id = self.db.add_note(101, "Старый", "Текст")
        with self.db.connection() as conn:
            conn.execute("UPDATE notes SET created_at = '2020-01-01 00:00:00', "
                         "updated_at = '2020-01-01 00:00:00' WHERE id = ?", (note_id,))
        self.assertTrue(self.db.update_note(101, note_id, title="Новый"))
        self.assertTrue(self.db.update_note(101, note_id, content="Новый текст"))
        note = self.db.get_note(101, note_id)
        self.assertEqual((note.title, note.content), ("Новый", "Новый текст"))
        self.assertEqual(note.created_at, "2020-01-01 00:00:00")
        self.assertNotEqual(note.updated_at, note.created_at)
        with self.assertRaises(ValueError):
            self.db.update_note(101, note_id, title=" ")
        with self.assertRaises(ValueError):
            self.db.update_note(101, note_id)
        self.assertEqual(self.db.get_note(101, note_id).title, "Новый")

    def test_delete_is_idempotent(self):
        note_id = self.db.add_note(101, "Удалить", "Текст")
        self.assertTrue(self.db.delete_note(101, note_id))
        self.assertFalse(self.db.delete_note(101, note_id))
        self.assertIsNone(self.db.get_note(101, note_id))

    def test_all_operations_enforce_owner(self):
        note_id = self.db.add_note(101, "Секрет", "Пароль от Wi-Fi")
        self.assertIsNone(self.db.get_note(202, note_id))
        self.assertEqual(self.db.list_notes(202).total, 0)
        self.assertEqual(self.db.search_notes(202, "секрет").total, 0)
        self.assertFalse(self.db.update_note(202, note_id, title="Взлом"))
        self.assertFalse(self.db.delete_note(202, note_id))
        self.assertEqual(self.db.get_note(101, note_id).title, "Секрет")

    def test_foreign_key_rejects_unknown_user(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_note(999, "Нет", "пользователя")

    def test_cyrillic_case_insensitive_search(self):
        first = self.db.add_note(101, "ПОКУПКИ", "Молоко")
        second = self.db.add_note(101, "Дела", "Купить Ёлку и хлеб")
        self.db.add_note(101, "Другое", "Ничего")
        self.db.add_note(202, "Покупки", "чужие")
        self.assertEqual({n.id for n in self.db.search_notes(101, "покупки").notes}, {first})
        self.assertEqual({n.id for n in self.db.search_notes(101, "КУП").notes}, {first, second})
        self.assertEqual({n.id for n in self.db.search_notes(101, "елку").notes}, {second})
        self.assertEqual({n.id for n in self.db.search_notes(101, "МОЛОКО").notes}, {first})

    def test_search_treats_wildcards_and_sql_literally(self):
        self.db.add_note(101, "Скидка", "50% на всё")
        self.db.add_note(101, "Другое", "просто текст")
        payload = "'); DROP TABLE notes; --"
        payload_id = self.db.add_note(101, payload, payload)
        self.assertEqual(self.db.search_notes(101, "%").total, 1)
        self.assertEqual(self.db.search_notes(101, "_").total, 0)
        self.assertEqual([n.id for n in self.db.search_notes(101, payload).notes], [payload_id])
        self.assertEqual(self.db.list_notes(101).total, 3)

    def test_pagination_is_stable_and_isolated(self):
        ids = [self.db.add_note(101, f"Заметка {n}", "текст") for n in range(12)]
        self.db.add_note(202, "Чужая", "текст")
        pages = [self.db.list_notes(101, n) for n in range(3)]
        self.assertEqual([len(p.notes) for p in pages], [PAGE_SIZE, PAGE_SIZE, 2])
        self.assertEqual([n.id for p in pages for n in p.notes], list(reversed(ids)))
        self.assertTrue(all(p.total == 12 and p.pages == 3 for p in pages))
        self.assertEqual(self.db.list_notes(101, 10**30).page, 2)
        self.assertEqual(self.db.list_notes(101, -1).page, 0)
        found = self.db.search_notes(101, "заметка", 1)
        self.assertEqual((found.total, found.page, len(found.notes)), (12, 1, PAGE_SIZE))

    def test_notes_do_not_affect_tasks(self):
        task_id = self.db.add_task(101, "Задача")
        note_id = self.db.add_note(101, "Заметка", "Текст")
        self.db.delete_note(101, note_id)
        self.assertEqual(self.db.get_task(101, task_id).title, "Задача")
        self.assertEqual(self.db.list_tasks(101).total, 1)
