"""News interests and bookmarks in a temporary SQLite database."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import Database
import news_store as store
from news_service import Article
from news_sources import CATEGORIES


def article(n=1, published=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)) -> Article:
    return Article(
        id=f"{n:016x}", title=f"Статья {n}", url=f"https://example.com/{n}", source_id="tech",
        source_name="Tech Daily", category="ai_tech", summary=f"Описание {n}", published_at=published,
        fetched_at=datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc),
    )


class NewsStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.db"
        self.db = Database(self.path)
        self.db.initialize()
        for user in (101, 202):
            self.db.upsert_user(user, f"User{user}")

    def test_interests(self):
        self.assertEqual(store.get_interests(self.db, 101), [])
        store.set_interests(self.db, 101, ["science", "games"])
        self.assertEqual(store.get_interests(self.db, 101), ["games", "science"])  # menu order
        self.assertTrue(store.toggle_interest(self.db, 101, "ai_tech"))
        self.assertFalse(store.toggle_interest(self.db, 101, "games"))
        self.assertEqual(store.get_interests(self.db, 101), ["ai_tech", "science"])
        self.assertEqual(store.get_interests(self.db, 202), [])
        store.set_interests(self.db, 202, CATEGORIES)
        self.assertEqual(store.get_interests(self.db, 101), ["ai_tech", "science"])
        for bad in (lambda: store.toggle_interest(self.db, 101, "sport"),
                    lambda: store.set_interests(self.db, 101, ["games", "sport"])):
            with self.assertRaises(ValueError):
                bad()
        self.assertEqual(store.get_interests(self.db, 101), ["ai_tech", "science"])

    def test_removed_category_is_ignored(self):
        with self.db.connection() as conn:
            conn.execute("INSERT INTO news_interests (user_id, category) VALUES (101, 'retired')")
        self.assertEqual(store.get_interests(self.db, 101), [])

    def test_bookmarks_are_unique_and_keep_metadata(self):
        self.assertEqual(store.add_bookmark(self.db, 101, article(1)), "added")
        self.assertEqual(store.add_bookmark(self.db, 101, article(1)), "exists")
        self.assertEqual(store.add_bookmark(self.db, 202, article(1)), "added")
        self.assertEqual(store.add_bookmark(self.db, 101, article(2, published=None)), "added")
        page = store.list_bookmarks(self.db, 101)
        self.assertEqual(page.total, 2)
        newest, oldest = page.bookmarks
        self.assertEqual(newest.article.title, "Статья 2")
        self.assertIsNone(newest.article.published_at)
        saved = oldest.article
        original = article(1)
        for field in ("id", "title", "url", "source_name", "category", "summary", "published_at"):
            self.assertEqual(getattr(saved, field), getattr(original, field), field)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connection() as conn:
                conn.execute("INSERT INTO news_bookmarks (user_id, article_id, url, title) VALUES (101, ?, 'u', 't')",
                             (original.id,))

    def test_bookmarks_are_private(self):
        store.add_bookmark(self.db, 101, article(1))
        bookmark_id = store.list_bookmarks(self.db, 101).bookmarks[0].id
        self.assertEqual(store.list_bookmarks(self.db, 202).total, 0)
        self.assertEqual(store.bookmarked_ids(self.db, 202, [article(1).id]), set())
        self.assertEqual(store.bookmarked_ids(self.db, 101, [article(1).id, article(9).id]), {article(1).id})
        self.assertEqual(store.bookmarked_ids(self.db, 101, []), set())
        self.assertFalse(store.delete_bookmark(self.db, 202, bookmark_id))
        self.assertEqual(store.list_bookmarks(self.db, 101).total, 1)
        self.assertTrue(store.delete_bookmark(self.db, 101, bookmark_id))
        self.assertFalse(store.delete_bookmark(self.db, 101, bookmark_id))

    def test_bookmark_pages_and_limit(self):
        with patch.object(store, "MAX_BOOKMARKS", 7):
            for n in range(1, 9):
                status = store.add_bookmark(self.db, 101, article(n))
            self.assertEqual(status, "limit")
        page = store.list_bookmarks(self.db, 101, 1)
        self.assertEqual((page.page, page.pages, page.total, len(page.bookmarks)), (1, 2, 7, 2))
        self.assertEqual(store.list_bookmarks(self.db, 101, 99).page, 1)
        self.assertEqual(store.list_bookmarks(self.db, 101, -3).page, 0)

    def test_new_tables_added_to_existing_database(self):
        task_id = self.db.add_task(101, "Старая задача")
        note_id = self.db.add_note(101, "Заметка", "Текст")
        with self.db.connection() as conn:
            conn.execute("DROP TABLE news_interests")
            conn.execute("DROP TABLE news_bookmarks")
            before = {row["name"]: row["sql"] for row in conn.execute("SELECT name, sql FROM sqlite_master")}
        self.db.initialize()
        self.db.initialize()
        with self.db.connection() as conn:
            after = {row["name"]: row["sql"] for row in conn.execute("SELECT name, sql FROM sqlite_master")}
        self.assertTrue({"news_interests", "news_bookmarks"} <= after.keys())
        for name, sql in before.items():
            self.assertEqual(after[name], sql, name)  # existing tables are untouched
        self.assertEqual(self.db.get_task(101, task_id).title, "Старая задача")
        self.assertEqual(self.db.get_note(101, note_id).content, "Текст")

    def test_incompatible_bookmarks_table_is_reported_not_changed(self):
        with self.db.connection() as conn:
            conn.execute("DROP TABLE news_bookmarks")
            conn.execute("CREATE TABLE news_bookmarks (id INTEGER PRIMARY KEY, link TEXT)")
            conn.execute("INSERT INTO news_bookmarks (link) VALUES ('https://example.com')")
        with self.assertRaises(RuntimeError):
            self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT link FROM news_bookmarks").fetchone()[0], "https://example.com")


if __name__ == "__main__":
    unittest.main()
