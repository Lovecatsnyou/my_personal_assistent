from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import Database, DB_PATH, MAX_TITLE_LENGTH, PAGE_SIZE


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.initialize()
        self.db.upsert_user(101, "Аня", "anya")
        self.db.upsert_user(202, "Борис")

    def test_default_path_is_project_absolute_path(self):
        self.assertTrue(DB_PATH.is_absolute())
        self.assertEqual(DB_PATH, Path(__file__).resolve().parents[1] / "assistant.db")

    def test_initialize_and_reopen_preserve_existing_data(self):
        task_id = self.db.add_task(101, "Сохранить данные")
        self.db.complete_task(101, task_id)
        reopened = Database(self.db.path)
        reopened.initialize()
        reopened.initialize()
        self.assertTrue(reopened.get_task(101, task_id).completed)
        self.assertEqual(reopened.list_tasks(101).total, 1)

    def test_profile_upsert_preserves_user_and_tasks(self):
        task_id = self.db.add_task(101, "Купить чай")
        self.db.upsert_user(101, "Анна", "new_name", "Иванова")
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (101,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_name"], "Анна")
        self.assertEqual(rows[0]["username"], "new_name")
        self.assertEqual(self.db.get_task(101, task_id).title, "Купить чай")

    def test_foreign_key_rejects_unknown_user(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_task(999, "Нет пользователя")

    def test_add_trims_title_and_keeps_unicode(self):
        task_id = self.db.add_task(101, "  Купить хлеб 🥖\nи чай  ")
        task = self.db.get_task(101, task_id)
        self.assertEqual(task.title, "Купить хлеб 🥖\nи чай")
        self.assertFalse(task.completed)
        self.assertEqual(task.user_id, 101)

    def test_invalid_titles_are_not_inserted(self):
        for title in ("", " \n\t", "а" * (MAX_TITLE_LENGTH + 1)):
            with self.subTest(title_length=len(title)), self.assertRaises(ValueError):
                self.db.add_task(101, title)
        self.assertEqual(self.db.list_tasks(101).total, 0)

    def test_maximum_title_is_accepted(self):
        self.db.add_task(101, "😀" * MAX_TITLE_LENGTH)
        self.assertEqual(self.db.list_tasks(101).total, 1)

    def test_sql_payload_is_stored_as_text(self):
        payload = "'); DROP TABLE users; --"
        task_id = self.db.add_task(101, payload)
        self.assertEqual(self.db.get_task(101, task_id).title, payload)
        self.db.upsert_user(303, payload, payload)
        self.assertEqual(self.db.list_tasks(101).total, 1)

    def test_all_crud_operations_enforce_owner(self):
        task_id = self.db.add_task(101, "Личная задача")
        self.assertIsNone(self.db.get_task(202, task_id))
        self.assertEqual(self.db.list_tasks(202).total, 0)
        self.assertFalse(self.db.complete_task(202, task_id))
        self.assertFalse(self.db.delete_task(202, task_id))
        self.assertFalse(self.db.get_task(101, task_id).completed)

    def test_completion_and_deletion_are_idempotent(self):
        task_id = self.db.add_task(101, "Завершить")
        self.assertTrue(self.db.complete_task(101, task_id))
        self.assertFalse(self.db.complete_task(101, task_id))
        with self.db.connection() as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertTrue(self.db.delete_task(101, task_id))
        self.assertFalse(self.db.delete_task(101, task_id))

    def test_pagination_is_stable_and_isolated(self):
        ids = [self.db.add_task(101, f"Задача {n}") for n in range(12)]
        self.db.add_task(202, "Чужая")
        pages = [self.db.list_tasks(101, n) for n in range(3)]
        self.assertEqual([len(p.tasks) for p in pages], [PAGE_SIZE, PAGE_SIZE, 2])
        self.assertEqual([t.id for p in pages for t in p.tasks], list(reversed(ids)))
        self.assertTrue(all(p.total == 12 and p.pages == 3 for p in pages))

    def test_page_clamps_after_last_task_on_page_is_deleted(self):
        ids = [self.db.add_task(101, str(n)) for n in range(6)]
        self.db.delete_task(101, ids[0])
        self.assertEqual(self.db.list_tasks(101, 1).page, 0)
        self.assertEqual(self.db.list_tasks(101, 10**30).page, 0)
        self.assertEqual(self.db.list_tasks(101, -1).page, 0)

    def test_empty_page_and_missing_task(self):
        page = self.db.list_tasks(101, 10)
        self.assertEqual((page.tasks, page.total, page.page, page.pages), ((), 0, 0, 1))
        self.assertIsNone(self.db.get_task(101, 123))
        self.assertFalse(self.db.complete_task(101, 123))
        self.assertFalse(self.db.delete_task(101, 123))

    def test_connection_rolls_back_and_closes_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.db.connection() as conn:
                conn.execute("INSERT INTO tasks (user_id, title) VALUES (?, ?)", (101, "Откат"))
                raise RuntimeError("rollback")
        self.assertEqual(self.db.list_tasks(101).total, 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
