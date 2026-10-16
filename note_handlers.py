"""Note commands, dialogs and inline menus; dialog state lives in user_data["notes_state"]."""

import asyncio
from datetime import datetime, timezone
import re
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database import (
    MAX_NOTE_CONTENT_LENGTH, MAX_NOTE_TITLE_LENGTH, MAX_SEARCH_LENGTH,
    clean_note_title, clean_search_query,
)
from task_handlers import get_database, parse_id, respond

NOTES_BUTTON = "📝 Заметки"
STATE_KEY = "notes_state"
# The last search query; pages of results are requested by buttons without the query text.
QUERY_KEY = "notes_query"
NOTE_COMMANDS = frozenset({"note", "notes", "searchnotes", "editnote", "deletenote"})
MESSAGE_LIMIT = 4096
PREVIEW_LENGTH = 80
BACK = r":(list|edit_list|delete_list|found):([0-9]{1,10})"


def clear_note_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(STATE_KEY, None)


def menu_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("➕ Добавить заметку", callback_data="notes:add")],
        [InlineKeyboardButton("📋 Мои заметки", callback_data="notes:list:0")],
        [InlineKeyboardButton("🔎 Поиск", callback_data="notes:search")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="notes:edit_list:0")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="notes:delete_list:0")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="home")],
    ]


def note_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(menu_rows())


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="notes:cancel")]])


async def private_chat(update: Update) -> bool:
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Личные заметки доступны только в личном чате с ботом.")
    return False


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split by Telegram's limit, which counts UTF-16 code units (emoji take two)."""
    parts, current, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    parts.append("".join(current))
    return parts


async def send_long(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Replace the menu with the first part; buttons go under the last part."""
    parts = split_message(text)
    await respond(update, parts[0], markup if len(parts) == 1 else None)
    for number, part in enumerate(parts[1:], start=2):
        await update.effective_message.reply_text(part, reply_markup=markup if number == len(parts) else None)


def format_date(value: str) -> str:
    """SQLite stores UTC; show the server's local time."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(value)
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def shorten(text: str, length: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[:length - 1] + "…"


async def show_note_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    if not await private_chat(update):
        return
    clear_note_state(context)
    await respond(update, f"{notice}\n\n📝 Заметки: выбери действие." if notice else "📝 Заметки: выбери действие.",
                  note_menu())


async def show_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "list", page: int = 0,
    notice: str = "",
) -> None:
    database, user_id = get_database(context), update.effective_user.id
    lines = [notice] if notice else []
    if mode == "found":
        query = context.user_data.get(QUERY_KEY)
        if not query:
            await respond(update, "Результаты поиска устарели. Начни новый поиск.", note_menu())
            return
        result = await asyncio.to_thread(database.search_notes, user_id, query, page)
        lines.append(f"🔎 Поиск «{query}»: найдено {result.total}. Страница {result.page + 1}/{result.pages}")
        empty = "Ничего не найдено. Попробуй другой запрос."
    else:
        result = await asyncio.to_thread(database.list_notes, user_id, page)
        lines.append(f"📝 Мои заметки — {result.total}. Страница {result.page + 1}/{result.pages}")
        empty = "Заметок пока нет. Нажми «Добавить заметку» или используй /note."
    if not result.notes:
        lines.append(empty)
    elif mode == "edit_list":
        lines.append("Выбери заметку для редактирования:")
    elif mode == "delete_list":
        lines.append("Выбери заметку для удаления:")
    icon = {"edit_list": "✏️", "delete_list": "🗑"}.get(mode, "📄")
    action = "delete" if mode == "delete_list" else "open"
    buttons = []
    for note in result.notes:
        dates = f"🕒 {format_date(note.created_at)}"
        if note.updated_at != note.created_at:
            dates += f" · изменена {format_date(note.updated_at)}"
        lines.append(f"#{note.id} — {shorten(note.title, MAX_NOTE_TITLE_LENGTH)}\n"
                     f"{shorten(note.content, PREVIEW_LENGTH)}\n{dates}")
        buttons.append([InlineKeyboardButton(
            f"{icon} #{note.id} {shorten(note.title, 30)}",
            callback_data=f"notes:{action}:{note.id}:{mode}:{result.page}",
        )])
    navigation = []
    if result.page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"notes:{mode}:{result.page - 1}"))
    if result.page + 1 < result.pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"notes:{mode}:{result.page + 1}"))
    if navigation:
        buttons.append(navigation)
    if mode == "found":
        buttons.append([InlineKeyboardButton("🔎 Новый поиск", callback_data="notes:search")])
    buttons.append([InlineKeyboardButton("➕ Добавить заметку", callback_data="notes:add")])
    buttons.append([InlineKeyboardButton("📝 Меню заметок", callback_data="notes:menu")])
    await respond(update, "\n\n".join(lines), InlineKeyboardMarkup(buttons))


async def show_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE, note_id: int, mode: str = "list",
    page: int = 0, notice: str = "",
) -> None:
    note = await asyncio.to_thread(get_database(context).get_note, update.effective_user.id, note_id)
    if note is None:
        await show_notes(update, context, mode, page, "Заметка не найдена — возможно, она уже удалена.")
        return
    back = f"{mode}:{page}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Заголовок", callback_data=f"notes:edit_title:{note.id}:{back}"),
         InlineKeyboardButton("✏️ Текст", callback_data=f"notes:edit_content:{note.id}:{back}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"notes:delete:{note.id}:{back}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data=f"notes:{back}"),
         InlineKeyboardButton("📝 Меню заметок", callback_data="notes:menu")],
    ])
    header = f"{notice}\n\n" if notice else ""
    await send_long(update, (
        f"{header}📝 #{note.id} — {note.title}\n"
        f"Создана: {format_date(note.created_at)}\nИзменена: {format_date(note.updated_at)}\n\n"
        f"{note.content}"
    ), markup)


async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[STATE_KEY] = {"action": "add_title"}
    await respond(update, f"Введи заголовок заметки (1–{MAX_NOTE_TITLE_LENGTH} символов). Отмена: /cancel.",
                  cancel_markup())


async def accept_title(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        title = clean_note_title(text)
    except ValueError as exc:
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    context.user_data[STATE_KEY] = {"action": "add_content", "title": title}
    await respond(update, f"Заголовок: «{title}».\n\nТеперь отправь текст заметки "
                          f"(1–{MAX_NOTE_CONTENT_LENGTH} символов). Отмена: /cancel.", cancel_markup())


async def accept_content(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, text: str) -> None:
    try:
        note_id = await asyncio.to_thread(
            get_database(context).add_note, update.effective_user.id, state["title"], text,
        )
    except ValueError as exc:
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    clear_note_state(context)
    await respond(update, f"✅ Заметка #{note_id} «{state['title']}» сохранена.", InlineKeyboardMarkup(
        [[InlineKeyboardButton("📄 Открыть", callback_data=f"notes:open:{note_id}:list:0")]] + menu_rows(),
    ))


async def begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[STATE_KEY] = {"action": "search"}
    await respond(update, f"Введи текст для поиска по заголовкам и содержимому "
                          f"(1–{MAX_SEARCH_LENGTH} символов). Отмена: /cancel.", cancel_markup())


async def accept_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        query = clean_search_query(text)
    except ValueError as exc:
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    clear_note_state(context)
    context.user_data[QUERY_KEY] = query
    await show_notes(update, context, "found")


async def begin_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, note_id: int, field: str, mode: str, page: int,
) -> None:
    note = await asyncio.to_thread(get_database(context).get_note, update.effective_user.id, note_id)
    if note is None:
        await show_notes(update, context, mode, page, "Заметка не найдена — возможно, она уже удалена.")
        return
    context.user_data[STATE_KEY] = {"action": f"edit_{field}", "note_id": note_id, "mode": mode, "page": page}
    if field == "title":
        text = (f"Текущий заголовок заметки #{note.id}:\n\n{note.title}\n\n"
                f"Отправь новый заголовок (1–{MAX_NOTE_TITLE_LENGTH} символов). Отмена: /cancel.")
    else:
        text = (f"Текущий текст заметки #{note.id} «{note.title}»:\n\n{note.content}\n\n"
                f"Отправь новый текст (1–{MAX_NOTE_CONTENT_LENGTH} символов). Отмена: /cancel.")
    await send_long(update, text, cancel_markup())


async def accept_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, text: str) -> None:
    field = state["action"].removeprefix("edit_")
    try:
        changed = await asyncio.to_thread(
            get_database(context).update_note, update.effective_user.id, state["note_id"], **{field: text},
        )
    except ValueError as exc:
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    clear_note_state(context)
    if changed:
        await show_note(update, context, state["note_id"], state["mode"], state["page"], "✅ Заметка обновлена.")
    else:
        await show_notes(update, context, state["mode"], state["page"], "Заметка не найдена — возможно, она уже удалена.")


async def confirm_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, note_id: int, mode: str, page: int,
) -> None:
    note = await asyncio.to_thread(get_database(context).get_note, update.effective_user.id, note_id)
    if note is None:
        await show_notes(update, context, mode, page, "Заметка не найдена — возможно, она уже удалена.")
        return
    nonce = secrets.token_hex(8)
    context.user_data[STATE_KEY] = {"action": "delete", "note_id": note_id, "mode": mode, "page": page, "nonce": nonce}
    await respond(update, f"Удалить заметку #{note.id} «{note.title}»?\nВосстановить её будет нельзя. Отмена: /cancel.",
                  InlineKeyboardMarkup([
                      [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"notes:confirm:{nonce}")],
                      [InlineKeyboardButton("Отмена", callback_data=f"notes:reject:{nonce}")],
                  ]))


def command_text(update: Update) -> str:
    # Keep the original spacing of text after the command, as /add does.
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) == 2 else ""


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    title = command_text(update)
    if not title:
        await begin_add(update, context)
    else:
        context.user_data[STATE_KEY] = {"action": "add_title"}
        await accept_title(update, context, title)


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_note_state(context)
    if context.args:
        await respond(update, "Использование: /notes. Страницы переключаются кнопками.")
        return
    await show_notes(update, context)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    query = command_text(update)
    if not query:
        await begin_search(update, context)
    else:
        context.user_data[STATE_KEY] = {"action": "search"}
        await accept_query(update, context, query)


async def note_action_command(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if not await private_chat(update):
        return
    clear_note_state(context)
    if not context.args:
        await show_notes(update, context, f"{action}_list")
        return
    try:
        if len(context.args) != 1:
            raise ValueError
        note_id = parse_id(context.args[0])
    except ValueError:
        await respond(update, f"Использование: /{action}note ID, например /{action}note 12.")
        return
    if action == "edit":
        await show_note(update, context, note_id)
    else:
        await confirm_delete(update, context, note_id, "list", 0)


async def editnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await note_action_command(update, context, "edit")


async def deletenote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await note_action_command(update, context, "delete")


async def cancel_note_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle /cancel when a note dialog is active; False lets other sections handle it."""
    if STATE_KEY not in (context.user_data or {}):
        return False
    if await private_chat(update):
        clear_note_state(context)
        await respond(update, "Действие отменено.", note_menu())
    return True


async def consume_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = (context.user_data or {}).get(STATE_KEY)
    if not state or state.get("action") == "delete":
        return False
    if not await private_chat(update):
        return True
    text = update.effective_message.text or ""
    if state["action"] == "add_title":
        await accept_title(update, context, text)
    elif state["action"] == "add_content":
        await accept_content(update, context, state, text)
    elif state["action"] == "search":
        await accept_query(update, context, text)
    else:
        await accept_edit(update, context, state, text)
    return True


async def note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await private_chat(update):
        return
    data = query.data if isinstance(query.data, str) else ""
    if data == "notes:menu":
        await show_note_menu(update, context)
    elif data == "notes:cancel":
        await show_note_menu(update, context, "Действие отменено.")
    elif data == "notes:add":
        await begin_add(update, context)
    elif data == "notes:search":
        await begin_search(update, context)
    elif match := re.fullmatch(r"notes:(confirm|reject):([a-f0-9]{16})", data):
        state = context.user_data.get(STATE_KEY, {})
        if state.get("action") != "delete" or state.get("nonce") != match[2]:
            await respond(update, "Подтверждение устарело. Открой /notes и выбери заметку заново.", note_menu())
            return
        clear_note_state(context)
        if match[1] == "reject":
            await show_notes(update, context, state["mode"], state["page"], "Удаление отменено.")
            return
        deleted = await asyncio.to_thread(
            get_database(context).delete_note, update.effective_user.id, state["note_id"],
        )
        await show_notes(update, context, state["mode"], state["page"],
                         "🗑 Заметка удалена." if deleted else "Заметка не найдена.")
    elif match := re.fullmatch(r"notes" + BACK, data):
        clear_note_state(context)
        await show_notes(update, context, match[1], int(match[2]))
    elif match := re.fullmatch(r"notes:(open|edit_title|edit_content|delete):([0-9]{1,19})" + BACK, data):
        try:
            note_id = parse_id(match[2])
        except ValueError:
            await respond(update, "Некорректная кнопка. Открой /notes.", note_menu())
            return
        clear_note_state(context)
        action, mode, page = match[1], match[3], int(match[4])
        if action == "open":
            await show_note(update, context, note_id, mode, page)
        elif action == "delete":
            await confirm_delete(update, context, note_id, mode, page)
        else:
            await begin_edit(update, context, note_id, action.removeprefix("edit_"), mode, page)
    else:
        await respond(update, "Кнопка устарела или некорректна. Открой /notes.", note_menu())


def register_note_handlers(application) -> None:
    for command, callback in (
        ("note", note_command), ("notes", notes_command), ("searchnotes", search_command),
        ("editnote", editnote_command), ("deletenote", deletenote_command),
    ):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(note_callback, pattern=r"^notes:"))
