"""News commands and inline menu.

Downloads run as background tasks, as in the weather section: updates are processed one at
a time (concurrent_updates(False)), and a slow feed must not stall tasks, notes and reminders.
The handler shows "⏳" at once and the task replaces it with the result.

user_data["news_state"] is the dialog state (waiting for a search query), cleared by /cancel
and by other sections. The last shown list is kept in user_data["news_view"] under a short
nonce together with copies of its articles: page and bookmark buttons work with exactly what
the user saw, and buttons of an older list are reported as outdated.
"""

import asyncio
import re
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from zoneinfo import ZoneInfo

from database import MAX_ID, PAGE_SIZE
from news_format import category_label, collection_notes, page_text, zone_footer
from news_service import MAX_QUERY_LENGTH, MIN_QUERY_LENGTH, Article, NewsService, clean_query
from news_sources import CATEGORIES, find_category, load_sources
import news_store as store
from reminder_store import get_timezone
from task_handlers import get_database
from weather_service import valid_zone

NEWS_BUTTON = "📰 Новости"
NEWS_COMMANDS = frozenset({"news", "newssearch", "interests", "bookmarks"})
STATE_KEY = "news_state"
VIEW_KEY = "news_view"
SERVICE_KEY = "news"
TASKS_KEY = "news_tasks"
MAX_VIEW = 50  # articles kept for one list: 10 pages
LOADING = "⏳ Загружаю новости из RSS-лент…"
STALE = "Кнопка устарела. Открой /news или меню новостей."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SEARCH_NOTE = "Поиск идёт по последним материалам подключённых RSS-лент, а не по всему интернету."


def get_service(context: ContextTypes.DEFAULT_TYPE) -> NewsService:
    return context.bot_data[SERVICE_KEY]


def clear_news_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(STATE_KEY, None)


def news_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Последние новости", callback_data="news:feed")],
        [InlineKeyboardButton("🎯 Мои интересы", callback_data="news:int")],
        [InlineKeyboardButton("📚 Категории", callback_data="news:cats")],
        [InlineKeyboardButton("🔎 Поиск", callback_data="news:find")],
        [InlineKeyboardButton("🔖 Сохранённые", callback_data="news:bms:0")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="news:feed:r")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="home")],
    ])


def menu_button() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("📰 Меню новостей", callback_data="news:menu")]


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="news:cancel")]])


async def private_chat(update: Update) -> bool:
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Новости доступны только в личном чате с ботом.")
    return False


def command_text(update: Update) -> str:
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) == 2 else ""


async def respond(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Like task_handlers.respond, without link previews: a page has up to five links."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


# ---------- Background delivery ----------

def run_background(update: Update, context: ContextTypes.DEFAULT_TYPE, coroutine) -> None:
    # PTB passes errors of the task to the error handler together with the update.
    task = context.application.create_task(coroutine, update=update, name="news")
    tasks = context.bot_data.setdefault(TASKS_KEY, set())
    tasks.add(task)

    def finished(done) -> None:
        tasks.discard(done)
        if not done.cancelled():
            done.exception()  # already passed to the error handler; mark it as retrieved

    task.add_done_callback(finished)


async def begin_loading(update: Update) -> Message | None:
    if update.callback_query:
        await respond(update, LOADING)
        return None
    return await update.effective_message.reply_text(LOADING)


async def deliver(
    update: Update, placeholder: Message | None, text: str, markup: InlineKeyboardMarkup | None = None,
) -> None:
    if placeholder is None:
        await respond(update, text, markup)
        return
    try:
        await placeholder.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await update.effective_message.reply_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


# ---------- Helpers ----------

async def user_zone(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[ZoneInfo, str]:
    """Dates are shown in the reminder time zone (/timezone) if it is set, otherwise in UTC."""
    name = await asyncio.to_thread(get_timezone, get_database(context), user_id)
    name = name if valid_zone(name) else "UTC"
    return ZoneInfo(name), name


async def interests(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[str]:
    return await asyncio.to_thread(store.get_interests, get_database(context), user_id)


def view_title(spec: dict) -> str:
    if spec["kind"] == "feed":
        return "📰 Моя лента: " + ", ".join(CATEGORIES[key].title for key in spec["categories"])
    if spec["kind"] == "all":
        return "📰 Все категории"
    if spec["kind"] == "cat":
        return f"📰 {category_label(spec['categories'][0])}"
    return f"🔎 Поиск «{spec['query']}»"


def current_view(context: ContextTypes.DEFAULT_TYPE, nonce: str) -> dict | None:
    view = context.user_data.get(VIEW_KEY)
    return view if view and view.get("nonce") == nonce else None


# ---------- Lists of articles ----------

async def load(update: Update, context: ContextTypes.DEFAULT_TYPE, spec: dict, force: bool = False) -> None:
    clear_news_state(context)
    placeholder = await begin_loading(update)
    run_background(update, context, fetch_and_show(update, context, placeholder, spec, force))


async def fetch_and_show(
    update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder: Message | None, spec: dict, force: bool,
) -> None:
    service = get_service(context)
    if spec["kind"] == "search":
        collection = await service.search(spec["query"], spec["categories"], force)
    else:
        collection = await service.collect(spec["categories"], force)
    notes = collection_notes(collection, service.clock())
    nonce = secrets.token_hex(4)
    view = {
        "nonce": nonce, "spec": spec, "title": view_title(spec), "notes": notes,
        "articles": [article.to_dict() for article in collection.articles[:MAX_VIEW]],
    }
    context.user_data[VIEW_KEY] = view
    if view["articles"]:
        text, markup = await render_page(update, context, view, 0)
        await deliver(update, placeholder, text, markup)
        return
    retry = [InlineKeyboardButton("🔄 Повторить", callback_data=f"news:re:{nonce}")]
    if spec["kind"] == "search":
        lines = [f"🔎 По запросу «{spec['query']}» ничего не найдено.", SEARCH_NOTE,
                 "Попробуй другие слова или загляни позже."]
        rows = [[InlineKeyboardButton("🔎 Искать снова", callback_data="news:find")]]
    elif collection.failed and not collection.stale and len(collection.failed) == len(
            service.sources_for(spec["categories"])):
        lines = ["⚠️ Не удалось загрузить новости ни из одного источника.",
                 "Проверь доступ к интернету (или VPN) на компьютере, где запущен бот, и попробуй позже."]
        rows = [retry]
    else:
        lines = [f"{view['title']}", "Свежих материалов пока нет."]
        rows = [retry]
    await deliver(update, placeholder, "\n\n".join([*notes, *lines]), InlineKeyboardMarkup(rows + [menu_button()]))


async def render_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, view: dict, page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    articles = view["articles"]
    pages = max(1, (len(articles) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    first = page * PAGE_SIZE
    shown = [Article.from_dict(data) for data in articles[first:first + PAGE_SIZE]]
    user_id = update.effective_user.id
    saved = await asyncio.to_thread(store.bookmarked_ids, get_database(context), user_id,
                                    [article.id for article in shown])
    zone, zone_name = await user_zone(context, user_id)
    header = f"{view['title']}\nМатериалов: {len(articles)}. Страница {page + 1}/{pages}"
    footer = zone_footer(zone_name)
    if view["spec"]["kind"] == "search":
        footer = f"{SEARCH_NOTE}\n{footer}"
    text = page_text(header, shown, first + 1, zone, saved, view["notes"] if page == 0 else [], footer)
    nonce = view["nonce"]
    rows = [[InlineKeyboardButton(f"{'✅' if article.id in saved else '🔖'} {first + offset + 1}",
                                  callback_data=f"news:bm:{nonce}:{first + offset}")
             for offset, article in enumerate(shown)]]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"news:pg:{nonce}:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"news:pg:{nonce}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"news:re:{nonce}")])
    if view["spec"]["kind"] == "search":
        rows.append([InlineKeyboardButton("🔎 Новый поиск", callback_data="news:find")])
    rows.append(menu_button())
    return text, InlineKeyboardMarkup(rows)


async def show_feed(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    chosen = await interests(context, update.effective_user.id)
    if not chosen:
        clear_news_state(context)
        await respond(update, (
            "🎯 Интересы ещё не выбраны.\n\nОтметь категории, которые хочешь видеть в своей ленте, "
            "или посмотри новости всех категорий."
        ), InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Выбрать интересы", callback_data="news:int")],
            [InlineKeyboardButton("📰 Все категории", callback_data="news:all")],
            menu_button(),
        ]))
        return
    await load(update, context, {"kind": "feed", "categories": chosen}, force)


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, force: bool = False) -> None:
    await load(update, context, {"kind": "cat", "categories": [key]}, force)


async def show_all(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    await load(update, context, {"kind": "all", "categories": list(CATEGORIES)}, force)


# ---------- Menu, interests, categories ----------

async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    if not await private_chat(update):
        return
    clear_news_state(context)
    chosen = await interests(context, update.effective_user.id)
    line = ("Мои интересы: " + ", ".join(category_label(key) for key in chosen) if chosen
            else "Интересы не выбраны — нажми «🎯 Мои интересы».")
    header = f"{notice}\n\n" if notice else ""
    await respond(update, f"{header}📰 Новости из RSS-лент: выбери действие.\n{line}", news_menu())


async def show_interests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_news_state(context)
    chosen = await interests(context, update.effective_user.id)
    rows = [[InlineKeyboardButton(f"{'✅' if key in chosen else '⬜'} {category.label}",
                                  callback_data=f"news:int:{key}")]
            for key, category in CATEGORIES.items()]
    rows.append([InlineKeyboardButton("📰 Показать мою ленту", callback_data="news:feed")])
    rows.append(menu_button())
    selected = ", ".join(CATEGORIES[key].title for key in chosen) if chosen else "ничего"
    await respond(update, (
        "🎯 Мои интересы\n\nНажимай на категории, чтобы включить или выключить их. "
        "Можно выбрать несколько — они составят ленту «📰 Последние новости».\n\n"
        f"Выбрано: {selected}."
    ), InlineKeyboardMarkup(rows))


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    clear_news_state(context)
    rows = [[InlineKeyboardButton(category.label, callback_data=f"news:cat:{key}")]
            for key, category in CATEGORIES.items()]
    rows.append([InlineKeyboardButton("📰 Все категории", callback_data="news:all")])
    rows.append(menu_button())
    header = f"{notice}\n\n" if notice else ""
    await respond(update, f"{header}📚 Категории: выбери, что показать.\nТакже можно: /news игры, /news наука.",
                  InlineKeyboardMarkup(rows))


# ---------- Bookmarks ----------

async def show_bookmarks(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, notice: str = "") -> None:
    clear_news_state(context)
    user_id = update.effective_user.id
    result = await asyncio.to_thread(store.list_bookmarks, get_database(context), user_id, page)
    if not result.total:
        await respond(update, (f"{notice}\n\n" if notice else "") + (
            "🔖 Сохранённых статей пока нет.\n\nНажми «🔖 N» под новостью, чтобы сохранить её сюда."
        ), InlineKeyboardMarkup([[InlineKeyboardButton("📰 Последние новости", callback_data="news:feed")],
                                 menu_button()]))
        return
    zone, zone_name = await user_zone(context, user_id)
    first = result.page * PAGE_SIZE
    header = f"🔖 Сохранённые статьи — {result.total}. Страница {result.page + 1}/{result.pages}"
    text = page_text(header, [bookmark.article for bookmark in result.bookmarks], first + 1, zone, set(),
                     [notice] if notice else [], zone_footer(zone_name))
    rows = [[InlineKeyboardButton(f"🗑 {first + offset + 1}", callback_data=f"news:bdel:{bookmark.id}:{result.page}")
             for offset, bookmark in enumerate(result.bookmarks)]]
    navigation = []
    if result.page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"news:bms:{result.page - 1}"))
    if result.page + 1 < result.pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"news:bms:{result.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append(menu_button())
    await respond(update, text, InlineKeyboardMarkup(rows))


async def bookmark(update: Update, context: ContextTypes.DEFAULT_TYPE, nonce: str, index: int) -> str | None:
    """Save an article of the current list; returns the notification text or None if the button is stale."""
    view = current_view(context, nonce)
    if view is None or index >= len(view["articles"]):
        return None
    article = Article.from_dict(view["articles"][index])
    status = await asyncio.to_thread(store.add_bookmark, get_database(context), update.effective_user.id, article)
    return {"added": "🔖 Сохранено в закладки", "exists": "Эта статья уже в закладках",
            "limit": f"Достигнут предел: {store.MAX_BOOKMARKS} закладок. Удали ненужные."}[status]


# ---------- Search ----------

async def begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    context.user_data[STATE_KEY] = {"action": "search"}
    await respond(update, (
        f"{notice}🔎 Введи слова для поиска ({MIN_QUERY_LENGTH}–{MAX_QUERY_LENGTH} символов), "
        f"например: Python, космос, видеокарты.\n{SEARCH_NOTE}\nОтмена: /cancel."
    ), cancel_markup())


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        query = clean_query(text)
    except ValueError as exc:
        context.user_data[STATE_KEY] = {"action": "search"}
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    await load(update, context, {"kind": "search", "categories": list(CATEGORIES), "query": query})


# ---------- Commands ----------

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_news_state(context)
    text = command_text(update)
    if not text:
        await show_feed(update, context)
    elif " ".join(text.split()).casefold() in ("все", "всё", "all"):
        await show_all(update, context)
    elif (category := find_category(text)) is not None:
        await show_category(update, context, category.key)
    else:
        await show_categories(update, context, "Такой категории нет.")


async def newssearch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    if text := command_text(update):
        await search(update, context, text)
    else:
        await begin_search(update, context)


async def interests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await private_chat(update):
        await show_interests(update, context)


async def bookmarks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await private_chat(update):
        await show_bookmarks(update, context)


async def cancel_news_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle /cancel when a news dialog is active; False lets other sections handle it."""
    if STATE_KEY not in (context.user_data or {}):
        return False
    if await private_chat(update):
        await show_news_menu(update, context, "Действие отменено.")
    return True


async def consume_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = (context.user_data or {}).get(STATE_KEY)
    if not state or state.get("action") != "search":
        return False
    if await private_chat(update):
        await search(update, context, update.effective_message.text or "")
    return True


# ---------- Callback buttons ----------

async def news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data if isinstance(query.data, str) else ""
    if not (update.effective_chat and update.effective_chat.type == "private"):
        await query.answer()
        await private_chat(update)
        return
    if match := re.fullmatch(r"news:bm:([0-9a-f]{8}):([0-9]{1,2})", data):
        # The result is shown as a notification; the list stays where it was.
        toast = await bookmark(update, context, match[1], int(match[2]))
        await query.answer(toast or "Список устарел. Открой новости заново.")
        if toast is None:
            await respond(update, STALE, news_menu())
            return
        view = current_view(context, match[1])
        await respond(update, *await render_page(update, context, view, int(match[2]) // PAGE_SIZE))
        return
    await query.answer()
    if data == "news:menu":
        await show_news_menu(update, context)
    elif data == "news:cancel":
        await show_news_menu(update, context, "Действие отменено.")
    elif match := re.fullmatch(r"news:feed(:r)?", data):
        await show_feed(update, context, bool(match[1]))
    elif match := re.fullmatch(r"news:all(:r)?", data):
        await show_all(update, context, bool(match[1]))
    elif data == "news:cats":
        await show_categories(update, context)
    elif (match := re.fullmatch(r"news:cat:([a-z_]{1,32})(:r)?", data)) and match[1] in CATEGORIES:
        await show_category(update, context, match[1], bool(match[2]))
    elif data == "news:int":
        await show_interests(update, context)
    elif (match := re.fullmatch(r"news:int:([a-z_]{1,32})", data)) and match[1] in CATEGORIES:
        await asyncio.to_thread(store.toggle_interest, get_database(context), update.effective_user.id, match[1])
        await show_interests(update, context)
    elif data == "news:find":
        await begin_search(update, context)
    elif match := re.fullmatch(r"news:pg:([0-9a-f]{8}):([0-9]{1,2})", data):
        if (view := current_view(context, match[1])) is None:
            await respond(update, STALE, news_menu())
            return
        clear_news_state(context)
        await respond(update, *await render_page(update, context, view, int(match[2])))
    elif match := re.fullmatch(r"news:re:([0-9a-f]{8})", data):
        if (view := current_view(context, match[1])) is None:
            await respond(update, STALE, news_menu())
            return
        await load(update, context, view["spec"], force=True)
    elif match := re.fullmatch(r"news:bms:([0-9]{1,5})", data):
        await show_bookmarks(update, context, int(match[1]))
    elif match := re.fullmatch(r"news:bdel:([0-9]{1,19}):([0-9]{1,5})", data):
        bookmark_id = int(match[1])
        deleted = 0 < bookmark_id <= MAX_ID and await asyncio.to_thread(
            store.delete_bookmark, get_database(context), update.effective_user.id, bookmark_id)
        await show_bookmarks(update, context, int(match[2]),
                             "🗑 Удалено из закладок." if deleted else "Закладка не найдена или уже удалена.")
    else:
        await respond(update, STALE, news_menu())


async def close_news(application: Application) -> None:
    """post_shutdown: stop downloads and close the shared HTTP client."""
    service = application.bot_data.get(SERVICE_KEY)
    if service is not None:
        await service.aclose()


def register_news_handlers(application: Application) -> None:
    application.bot_data.setdefault(SERVICE_KEY, NewsService(load_sources()))
    for command, callback in (
        ("news", news_command), ("newssearch", newssearch_command), ("interests", interests_command),
        ("bookmarks", bookmarks_command),
    ):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(news_callback, pattern=r"^news:"))
