from datetime import date, datetime, time, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import Database
from reminder_schedule import Schedule
import reminder_store as store

FIRST = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)


def schedule(repeat="once", days=()):
    return Schedule("Europe/Moscow", date(2026, 10, 20), time(10, 0), 0, repeat, frozenset(days))


class ReminderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.initialize()
        self.db.upsert_user(101, "Аня")
        self.db.upsert_user(202, "Борис")

    def create(self, user=101, repeat="once", text="Позвонить"):
        return store.create_reminder(self.db, user, text, schedule(repeat), FIRST)

    def test_migration_keeps_existing_data(self):
        path = Path(self.temp.name) / "old.db"
        old = Database(path)
        with old.connection() as conn:
            # Schema of the previous release: users, tasks, notes only.
            conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT "
                         "NOT NULL, last_name TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                         "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO users (telegram_id, first_name) VALUES (1, 'Старый')")
        old.initialize()
        task_id = old.add_task(1, "Задача")
        note_id = old.add_note(1, "Заметка", "Текст")
        old.initialize()
        self.assertEqual(old.get_task(1, task_id).title, "Задача")
        self.assertEqual(old.get_note(1, note_id).content, "Текст")
        with old.connection() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "tasks", "notes", "user_settings", "reminders"} <= tables)

    def test_incompatible_reminders_table_is_reported_not_changed(self):
        legacy = Database(Path(self.temp.name) / "legacy.db")
        with legacy.connection() as conn:
            conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT)")
            conn.execute("INSERT INTO reminders (text) VALUES ('старое')")
        with self.assertRaises(RuntimeError):
            legacy.initialize()
        with legacy.connection() as conn:
            self.assertEqual(conn.execute("SELECT text FROM reminders").fetchone()[0], "старое")

    def test_timezone_setting(self):
        self.assertIsNone(store.get_timezone(self.db, 101))
        self.assertEqual(store.set_timezone(self.db, 101, "Europe/Moscow"), 0)
        self.create()
        self.assertEqual(store.set_timezone(self.db, 101, "Europe/Berlin"), 1)
        self.assertEqual(store.get_timezone(self.db, 101), "Europe/Berlin")
        self.assertIsNone(store.get_timezone(self.db, 202))

    def test_create_and_read_fields(self):
        reminder_id = store.create_reminder(self.db, 101, "  Купить хлеб  ", schedule("weekdays", {0, 4}), FIRST)
        reminder = store.get_reminder(self.db, 101, reminder_id)
        self.assertEqual(reminder.text, "Купить хлеб")
        self.assertEqual((reminder.local_date, reminder.local_time, reminder.timezone),
                         ("2026-10-20", "10:00", "Europe/Moscow"))
        self.assertEqual((reminder.repeat, reminder.repeat_days, reminder.status), ("weekdays", "0,4", "active"))
        self.assertEqual(reminder.next_run, FIRST)
        self.assertEqual(reminder.schedule, schedule("weekdays", {0, 4}))

    def test_text_limits(self):
        store.create_reminder(self.db, 101, "😀" * store.MAX_TEXT_LENGTH, schedule(), FIRST)
        for text in ("", "  ", "а" * (store.MAX_TEXT_LENGTH + 1)):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                store.create_reminder(self.db, 101, text, schedule(), FIRST)

    def test_owner_is_checked(self):
        reminder_id = self.create()
        version = store.get_reminder(self.db, 101, reminder_id).version
        self.assertIsNone(store.get_reminder(self.db, 202, reminder_id))
        self.assertEqual(store.list_reminders(self.db, 202).total, 0)
        self.assertFalse(store.update_reminder(self.db, 202, reminder_id, version, text="Взлом"))
        self.assertFalse(store.delete_reminder(self.db, 202, reminder_id))
        self.assertEqual(store.get_reminder(self.db, 101, reminder_id).text, "Позвонить")

    def test_foreign_key(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(user=999)

    def test_version_protects_against_stale_edits(self):
        reminder_id = self.create()
        version = store.get_reminder(self.db, 101, reminder_id).version
        self.assertTrue(store.update_reminder(self.db, 101, reminder_id, version, text="Новый"))
        self.assertFalse(store.update_reminder(self.db, 101, reminder_id, version, text="Старый"))
        self.assertEqual(store.get_reminder(self.db, 101, reminder_id).text, "Новый")
        with self.assertRaises(ValueError):
            store.update_reminder(self.db, 101, reminder_id, version + 1, id=7)

    def test_delivery_lifecycle_once(self):
        reminder_id = self.create()
        version = store.get_reminder(self.db, 101, reminder_id).version
        self.assertTrue(store.claim_delivery(self.db, reminder_id, version))
        self.assertTrue(store.record_success(self.db, reminder_id, version, None))
        reminder = store.get_reminder(self.db, 101, reminder_id)
        self.assertEqual(reminder.status, "done")
        self.assertIsNotNone(reminder.last_sent_at)
        self.assertFalse(store.claim_delivery(self.db, reminder_id, version))
        self.assertEqual(store.all_active(self.db), [])

    def test_delivery_lifecycle_repeating_and_failures(self):
        reminder_id = self.create(repeat="daily")
        version = store.get_reminder(self.db, 101, reminder_id).version
        later = datetime(2026, 10, 21, 7, tzinfo=timezone.utc)
        self.assertTrue(store.record_failure(self.db, reminder_id, version, "NetworkError"))
        self.assertTrue(store.record_success(self.db, reminder_id, version, later))
        reminder = store.get_reminder(self.db, 101, reminder_id)
        self.assertEqual((reminder.status, reminder.next_run, reminder.error_count), ("active", later, 0))
        self.assertTrue(store.record_failure(self.db, reminder_id, version, "Forbidden", failed=True))
        reminder = store.get_reminder(self.db, 101, reminder_id)
        self.assertEqual((reminder.status, reminder.last_error, reminder.error_count), ("failed", "Forbidden", 1))

    def test_delivery_ignores_edited_or_paused(self):
        reminder_id = self.create()
        version = store.get_reminder(self.db, 101, reminder_id).version
        store.update_reminder(self.db, 101, reminder_id, version, status="paused")
        self.assertFalse(store.claim_delivery(self.db, reminder_id, version))
        self.assertFalse(store.claim_delivery(self.db, reminder_id, version + 1))
        self.assertFalse(store.record_success(self.db, reminder_id, version, None))
        self.assertEqual(store.get_reminder(self.db, 101, reminder_id).status, "paused")

    def test_list_pagination_order_and_filter(self):
        ids = [self.create(repeat="daily" if n % 2 else "once", text=f"Н{n}") for n in range(7)]
        version = store.get_reminder(self.db, 101, ids[0]).version
        store.update_reminder(self.db, 101, ids[0], version, status="paused")
        self.create(user=202)
        first, second = store.list_reminders(self.db, 101, 0), store.list_reminders(self.db, 101, 1)
        self.assertEqual((first.total, first.pages, len(first.reminders), len(second.reminders)), (7, 2, 5, 2))
        self.assertEqual(second.reminders[-1].id, ids[0])  # paused after active ones
        recurring = store.list_reminders(self.db, 101, recurring_only=True)
        self.assertEqual({r.id for r in recurring.reminders}, {ids[1], ids[3], ids[5]})
        self.assertEqual(store.list_reminders(self.db, 101, 99).page, 1)
