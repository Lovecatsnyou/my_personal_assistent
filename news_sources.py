"""News categories and the allow-list of RSS/Atom sources.

Sources live in news_sources.json (or the file named by NEWS_SOURCES_FILE), so they can be
added, removed or switched off without touching the code. Only these URLs are ever fetched,
and redirects may lead only to the source's own host or to hosts listed in its "hosts".
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

SOURCES_PATH = Path(__file__).resolve().parent / "news_sources.json"
MAX_NAME_LENGTH = 60
MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    title: str
    aliases: frozenset[str]

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.title}"


CATEGORIES = {category.key: category for category in (
    Category("ai_tech", "🤖", "Технологии и ИИ", frozenset({
        "технологии", "ии", "ai", "tech", "технологии и ии", "ит", "it"})),
    Category("programming", "💻", "Программирование", frozenset({
        "программирование", "код", "разработка", "programming", "dev"})),
    Category("games", "🎮", "Игры", frozenset({"игры", "игра", "games", "gaming"})),
    Category("economy", "💰", "Экономика и финансы", frozenset({
        "экономика", "финансы", "экономика и финансы", "economy", "finance", "business"})),
    Category("world", "🌍", "Мировые события", frozenset({
        "мир", "мировые события", "в мире", "world"})),
    Category("science", "🔬", "Наука", frozenset({"наука", "science"})),
)}


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    category: str
    hosts: frozenset[str]  # hosts this source may be fetched from, including redirects
    summaries: bool = True  # false for feeds whose descriptions carry no text (e.g. only a link)


def find_category(text: str) -> Category | None:
    """Category by key, title or alias; case-insensitive, «ё» equals «е»."""
    wanted = " ".join(text.split()).casefold().replace("ё", "е")
    for category in CATEGORIES.values():
        names = {category.key, category.title.casefold().replace("ё", "е"), *category.aliases}
        if wanted in names:
            return category
    return None


def host_of(url: str) -> str | None:
    """Lower-case host of an absolute http(s) URL without credentials, else None."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH or any(ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return parts.hostname.lower().rstrip(".")


def _source(raw: object) -> Source | None:
    if not isinstance(raw, dict):
        raise ValueError("каждый источник должен быть объектом")
    source_id, name, url, category = (raw.get(key) for key in ("id", "name", "url", "category"))
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9_]{1,32}", source_id):
        raise ValueError(f"некорректный id: {source_id!r}")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME_LENGTH:
        raise ValueError(f"{source_id}: некорректное название")
    if category not in CATEGORIES:
        raise ValueError(f"{source_id}: неизвестная категория {category!r}")
    host = host_of(url)
    if host is None:
        raise ValueError(f"{source_id}: разрешены только абсолютные http/https URL")
    extra = raw.get("hosts", [])
    if not isinstance(extra, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]{1,253}", h)
                                              for h in extra):
        raise ValueError(f"{source_id}: hosts должен быть списком имён хостов")
    for flag in ("enabled", "summaries"):
        if not isinstance(raw.get(flag, True), bool):
            raise ValueError(f"{source_id}: {flag} должен быть true или false")
    if not raw.get("enabled", True):
        return None
    return Source(source_id, name.strip(), url, category, frozenset({host, *extra}), raw.get("summaries", True))


def load_sources(path: str | Path | None = None) -> tuple[Source, ...]:
    """Read and validate the source list; a broken file stops the bot at startup."""
    path = Path(path or os.getenv("NEWS_SOURCES_FILE") or SOURCES_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Не удалось прочитать список источников новостей {path}: {exc}") from None
    try:
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise ValueError('нужен объект с ключом "sources"')
        sources = [source for raw in data["sources"] if (source := _source(raw)) is not None]
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("id источников повторяются")
    except ValueError as exc:
        raise RuntimeError(f"Ошибка в списке источников новостей {path}: {exc}") from None
    return tuple(sources)
