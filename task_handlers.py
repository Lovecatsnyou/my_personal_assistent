"""Task commands and inline menus; no token or application startup side effects."""

import asyncio
import re
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database import Database, MAX_ID, MAX_TITLE_LENGTH

TASKS_BUTTON = "📋 Задачи"
STATE_KEY = "tasks_state"


def get_database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.bot_data["database"]


def clear_task_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(STATE_KEY, None)


def task_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать задачу", callback_data="tasks:add")],
        [InlineKeyboardButton("📋 Посмотреть задачи", callback_data="tasks:list:0")],
        [InlineKeyboardButton("✅ Выполнить задачу", callback_data="tasks:done_list:0")],
        [InlineKeyboardButton("🗑 Удалить задачу", callback_data="tasks:delete_list:0")],
    ])


async def private_chat(update: Update) -> bool:
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Управление личными задачами доступно в личном чате с ботом.")
    return False


async def respond(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            # Deleted or no longer editable menu: send a fresh one.
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_task_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_task_state(context)
    await respond(update, "📋 Задачи: выбери действие.", task_menu())


async def show_tasks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0,
    mode: str = "list", notice: str = "",
) -> None:
    result = await asyncio.to_thread(get_database(context).list_tasks, update.effective_user.id, page)
    lines = [notice] if notice else []
    lines.append(f"📋 Мои задачи — {result.total}. Страница {result.page + 1}/{result.pages}")
    if not result.tasks:
        lines.append("Задач пока нет. Нажми «Создать задачу» или используй /add.")
    elif mode == "done_list":
        lines.append("Выбери задачу для выполнения:")
    elif mode == "delete_list":
        lines.append("Выбери задачу для удаления:")
    buttons = []
    for task in result.tasks:
        title = " ".join(task.title.split())
        lines.append(f"{'✅' if task.completed else '⬜'} #{task.id} — {title}")
        row = []
        if not task.completed and mode != "delete_list":
            row.append(InlineKeyboardButton(
                f"✅ #{task.id}", callback_data=f"tasks:done:{task.id}:{result.page}",
            ))
        if mode != "done_list":
            row.append(InlineKeyboardButton(
                f"🗑 #{task.id}", callback_data=f"tasks:delete:{task.id}:{result.page}",
            ))
        if row:
            buttons.append(row)
    navigation = []
    if result.page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"tasks:{mode}:{result.page - 1}"))
    if result.page + 1 < result.pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"tasks:{mode}:{result.page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("➕ Создать задачу", callback_data="tasks:add")])
    buttons.append([InlineKeyboardButton("📋 Меню задач", callback_data="tasks:menu")])
    await respond(update, "\n\n".join(lines), InlineKeyboardMarkup(buttons))


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_task_state(context)
    if context.args:
        await respond(update, "Использование: /tasks. Страницы переключаются кнопками.")
        return
    await show_tasks(update, context)


async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[STATE_KEY] = {"action": "add"}
    await respond(update, f"Введи название задачи (1–{MAX_TITLE_LENGTH} символов). Отмена: /cancel.")


async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str) -> None:
    try:
        task_id = await asyncio.to_thread(get_database(context).add_task, update.effective_user.id, title)
    except ValueError as exc:
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.")
        return
    clear_task_state(context)
    await respond(update, f"Задача #{task_id} создана.", task_menu())


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    # Keep the original spacing and line breaks in a title supplied after /add.
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) == 1:
        await begin_add(update, context)
    else:
        context.user_data[STATE_KEY] = {"action": "add"}
        await save_task(update, context, parts[1])


async def consume_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = (context.user_data or {}).get(STATE_KEY, {})
    if state.get("action") != "add":
        return False
    if await private_chat(update):
        await save_task(update, context, update.effective_message.text or "")
    return True


def parse_id(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or not 0 < int(value) <= MAX_ID:
        raise ValueError("Некорректный номер задачи.")
    return int(value)


async def complete(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int, page: int) -> None:
    changed = await asyncio.to_thread(get_database(context).complete_task, update.effective_user.id, task_id)
    notice = f"Задача #{task_id} выполнена." if changed else "Задача не найдена или уже выполнена."
    await show_tasks(update, context, page, notice=notice)


async def confirm_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int, page: int,
) -> None:
    task = await asyncio.to_thread(get_database(context).get_task, update.effective_user.id, task_id)
    if task is None:
        await show_tasks(update, context, page, notice="Задача не найдена.")
        return
    nonce = secrets.token_hex(8)
    context.user_data[STATE_KEY] = {"action": "delete", "task_id": task_id, "page": page, "nonce": nonce}
    await respond(update, f"Удалить задачу #{task.id}?\n\n{task.title}\n\nОтмена: /cancel.", InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"tasks:confirm:{nonce}")],
        [InlineKeyboardButton("Отмена", callback_data=f"tasks:reject:{nonce}")],
    ]))


async def task_action_command(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if not await private_chat(update):
        return
    clear_task_state(context)
    if not context.args:
        await show_tasks(update, context, mode=f"{action}_list")
        return
    try:
        if len(context.args) != 1:
            raise ValueError
        task_id = parse_id(context.args[0])
    except ValueError:
        await respond(update, f"Использование: /{action} ID, например /{action} 12.")
        return
    if action == "done":
        await complete(update, context, task_id, 0)
    else:
        await confirm_delete(update, context, task_id, 0)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await task_action_command(update, context, "done")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await task_action_command(update, context, "delete")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_task_state(context)
    await respond(update, "Действие отменено.", task_menu())


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await private_chat(update):
        return
    data = query.data if isinstance(query.data, str) else ""
    if data == "tasks:menu":
        await show_task_menu(update, context)
    elif data == "tasks:add":
        await begin_add(update, context)
    elif match := re.fullmatch(r"tasks:(confirm|reject):([a-f0-9]{16})", data):
        state = context.user_data.get(STATE_KEY, {})
        if state.get("action") != "delete" or state.get("nonce") != match[2]:
            await respond(update, "Подтверждение устарело. Открой /tasks и выбери задачу заново.")
            return
        if match[1] == "reject":
            clear_task_state(context)
            await show_tasks(update, context, state["page"], notice="Удаление отменено.")
            return
        deleted = await asyncio.to_thread(
            get_database(context).delete_task, update.effective_user.id, state["task_id"],
        )
        clear_task_state(context)
        await show_tasks(update, context, state["page"], notice="Задача удалена." if deleted else "Задача не найдена.")
    elif match := re.fullmatch(r"tasks:(list|done_list|delete_list):([0-9]{1,10})", data):
        clear_task_state(context)
        await show_tasks(update, context, int(match[2]), match[1])
    elif match := re.fullmatch(r"tasks:(done|delete):([0-9]{1,19}):([0-9]{1,10})", data):
        try:
            task_id = parse_id(match[2])
        except ValueError:
            await respond(update, "Некорректная кнопка. Открой /tasks.")
            return
        clear_task_state(context)
        if match[1] == "done":
            await complete(update, context, task_id, int(match[3]))
        else:
            await confirm_delete(update, context, task_id, int(match[3]))
    else:
        await respond(update, "Кнопка устарела или некорректна. Открой /tasks.")


TASK_COMMANDS = frozenset({"add", "tasks", "done", "delete"})


def register_task_handlers(application) -> None:
    # /cancel is registered in main.py because it is shared with other sections.
    for command, callback in (
        ("add", add_command), ("tasks", tasks_command), ("done", done_command),
        ("delete", delete_command),
    ):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(task_callback, pattern=r"^tasks:"))
