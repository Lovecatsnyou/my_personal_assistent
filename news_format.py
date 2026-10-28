"""Plain-text news messages.

Messages are sent without parse_mode: feed text is already reduced to plain text and is shown
literally, so markup from a feed cannot format, hide or forge anything. Telegram itself makes
the bare http(s) links clickable.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from news_service import MIN_REFRESH_INTERVAL, Article, Collection, NewsError
from news_sources import CATEGORIES
from weather_format import MESSAGE_LIMIT, fit_message, utf16_length

MAX_NAMES = 4
REASONS = {
    "dns": "ошибка DNS",
    "timeout": "нет ответа вовремя",
    "network": "нет соединения",
    "rate_limit": "слишком много запросов (HTTP 429)",
    "server": "ошибка сервера",
    "rejected": "запрос отклонён",
    "too_large": "слишком большой ответ",
    "forbidden_url": "запрещённый адрес",
    "bad_feed": "некорректная лента",
}


def reason(error: NewsError | None) -> str:
    return REASONS.get(error.reason, "неизвестная ошибка") if error else "не успел ответить"


def format_moment(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def ago(seconds: float) -> str:
    minutes = max(0, int(seconds) // 60)
    if minutes == 0:
        return "меньше минуты назад"
    if minutes < 60:
        return f"{minutes} мин назад"
    return f"{minutes // 60} ч {minutes % 60} мин назад" if minutes % 60 else f"{minutes // 60} ч назад"


def category_label(key: str) -> str:
    category = CATEGORIES.get(key)
    return category.label if category else key


def card(number: int, article: Article, zone: ZoneInfo, saved: bool = False, summary: bool = True) -> str:
    lines = [f"{number}. {'🔖 ' if saved else ''}{article.title}"]
    if summary and article.summary:
        lines.append(article.summary)
    date = format_moment(article.published_at, zone) if article.published_at else "дата не указана"
    lines.append(f"📰 {article.source_name} · 🕒 {date}" if article.source_name else f"🕒 {date}")
    lines.append(f"🔗 {article.url}")
    return "\n".join(lines)


def names(items) -> str:
    shown = [f"{source.name} ({detail})" for source, detail in items[:MAX_NAMES]]
    if len(items) > MAX_NAMES:
        shown.append(f"и ещё {len(items) - MAX_NAMES}")
    return ", ".join(shown)


def collection_notes(collection: Collection, now: float) -> list[str]:
    notes = []
    if collection.throttled:
        notes.append(f"ℹ️ Ленты обновлялись меньше {MIN_REFRESH_INTERVAL // 60} минут назад — "
                     "показаны уже полученные материалы.")
    if collection.stale:
        notes.append("⚠️ Не удалось обновить: " + names([
            (source, f"{reason(error)}; данные получены {ago(now - fetched_at)}")
            for source, fetched_at, error in collection.stale
        ]) + ". Их материалы могут быть устаревшими.")
    if collection.failed:
        notes.append("⚠️ Недоступны сейчас: " + names([(source, reason(error)) for source, error in collection.failed])
                     + ".")
    return notes


def page_text(
    header: str, articles: list[Article], first_number: int, zone: ZoneInfo, saved: set[str],
    notes: list[str] = (), footer: str = "",
) -> str:
    """A page of cards; summaries are dropped before anything else if the message gets too long."""
    tail = [footer] if footer else []
    for with_summary in (True, False):
        parts = [*notes, header] + [
            card(first_number + offset, article, zone, article.id in saved, with_summary)
            for offset, article in enumerate(articles)
        ] + tail
        text = "\n\n".join(parts)
        if utf16_length(text) <= MESSAGE_LIMIT:
            return text
    return fit_message(text)


def zone_footer(zone_name: str) -> str:
    return f"Время публикаций: {zone_name}." if zone_name != "UTC" else (
        "Время публикаций: UTC. Свой часовой пояс можно задать командой /timezone.")
