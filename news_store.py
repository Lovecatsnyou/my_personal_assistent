"""SQLite operations for news interests and bookmarks (tables news_interests, news_bookmarks).

Every query is limited to the owner's Telegram ID. A bookmark stores the article's title,
link, summary, source and date, so it stays readable after the article leaves the feed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from database import PAGE_SIZE, Database
from news_service import Article
from news_sources import CATEGORIES

MAX_BOOKMARKS = 500


@dataclass(frozen=True)
class Bookmark:
    id: int
    article: Article
    created_at: str  # UTC, SQLite format


@dataclass(frozen=True)
class BookmarkPage:
    bookmarks: tuple[Bookmark, ...]
    page: int
    pages: int
    total: int


def get_interests(db: Database, user_id: int) -> list[str]:
    """Selected category keys in menu order; categories no longer offered are ignored."""
    with db.connection() as conn:
        chosen = {row["category"] for row in conn.execute(
            "SELECT category FROM news_interests WHERE user_id = ?", (user_id,))}
    return [key for key in CATEGORIES if key in chosen]


def set_interests(db: Database, user_id: int, categories: Iterable[str]) -> None:
    categories = set(categories)
    if not categories <= CATEGORIES.keys():
        raise ValueError("Неизвестная категория.")
    with db.connection() as conn:
        conn.execute("DELETE FROM news_interests WHERE user_id = ?", (user_id,))
        conn.executemany("INSERT INTO news_interests (user_id, category) VALUES (?, ?)",
                         [(user_id, key) for key in categories])


def toggle_interest(db: Database, user_id: int, category: str) -> bool:
    """Select or deselect one category; returns whether it is selected now."""
    if category not in CATEGORIES:
        raise ValueError("Неизвестная категория.")
    with db.connection() as conn:
        if conn.execute("DELETE FROM news_interests WHERE user_id = ? AND category = ?",
                        (user_id, category)).rowcount:
            return False
        conn.execute("INSERT INTO news_interests (user_id, category) VALUES (?, ?)", (user_id, category))
        return True


def _bookmark(row) -> Bookmark:
    published = row["published_at"]
    try:
        published_at = datetime.fromisoformat(published) if published else None
    except ValueError:
        published_at = None
    created = datetime.fromisoformat(row["created_at"] + "+00:00")
    article = Article(
        id=row["article_id"], title=row["title"], url=row["url"], source_id="", source_name=row["source_name"],
        category=row["category"], summary=row["summary"], published_at=published_at, fetched_at=created,
    )
    return Bookmark(row["id"], article, row["created_at"])


def add_bookmark(db: Database, user_id: int, article: Article) -> str:
    """'added', 'exists' or 'limit'."""
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")  # the count check and the insert must not interleave
        if conn.execute("SELECT 1 FROM news_bookmarks WHERE user_id = ? AND article_id = ?",
                        (user_id, article.id)).fetchone():
            return "exists"
        if conn.execute("SELECT COUNT(*) FROM news_bookmarks WHERE user_id = ?",
                        (user_id,)).fetchone()[0] >= MAX_BOOKMARKS:
            return "limit"
        conn.execute("""
            INSERT INTO news_bookmarks
                (user_id, article_id, url, title, summary, source_name, category, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, article.id, article.url, article.title, article.summary, article.source_name,
              article.category, article.published_at.isoformat() if article.published_at else None))
        return "added"


def bookmarked_ids(db: Database, user_id: int, article_ids: Iterable[str]) -> set[str]:
    article_ids = list(article_ids)
    if not article_ids:
        return set()
    placeholders = ", ".join("?" * len(article_ids))
    with db.connection() as conn:
        return {row["article_id"] for row in conn.execute(
            f"SELECT article_id FROM news_bookmarks WHERE user_id = ? AND article_id IN ({placeholders})",
            (user_id, *article_ids))}


def list_bookmarks(db: Database, user_id: int, page: int = 0) -> BookmarkPage:
    with db.connection() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) FROM news_bookmarks WHERE user_id = ?", (user_id,)).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = conn.execute(
            "SELECT * FROM news_bookmarks WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, PAGE_SIZE, page * PAGE_SIZE),
        ).fetchall()
    return BookmarkPage(tuple(_bookmark(row) for row in rows), page, pages, total)


def delete_bookmark(db: Database, user_id: int, bookmark_id: int) -> bool:
    with db.connection() as conn:
        return conn.execute("DELETE FROM news_bookmarks WHERE user_id = ? AND id = ?",
                            (user_id, bookmark_id)).rowcount == 1
