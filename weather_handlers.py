"""Weather commands and inline menu.

Requests to Open-Meteo run as background tasks: updates are processed one at a time
(concurrent_updates(False)), and a hanging connection must not stall tasks, notes and
reminders. The handler shows "⏳" at once and the task replaces it with the result.

user_data["weather_state"] is the dialog state (waiting for a city name), cleared by
/cancel and by other sections. Search results and places shown without saving live in
separate keys under short nonces, so their buttons work until replaced.
"""

import asyncio
from dataclasses import asdict
import re
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from note_handlers import format_date
from task_handlers import get_database, respond
from weather_format import format_current, format_forecast, place_card, place_label, error_reason, unavailable
from weather_service import (
    MAX_QUERY_LENGTH, MIN_QUERY_LENGTH, Place, WeatherError, WeatherService, clean_query,
)
import weather_store as store

WEATHER_BUTTON = "🌤 Погода"
WEATHER_COMMANDS = frozenset({"weather", "forecast", "setcity", "mycity"})
STATE_KEY = "weather_state"
CHOICES_KEY = "weather_choices"  # the latest search results: {"nonce", "purpose", "places"}
PLACES_KEY = "weather_places"    # nonce -> place shown without saving it
SERVICE_KEY = "weather"
TASKS_KEY = "weather_tasks"
MAX_PLACES = 5
MAX_CHOICES = 8
LOADING = "⏳ Запрашиваю данные Open-Meteo…"
STALE = "Кнопка устарела. Открой /weather или меню погоды."
# Search purposes: "set" saves the main city, then shows current weather;
# "cur"/"fc" only show weather or forecast for the chosen place.


def get_service(context: ContextTypes.DEFAULT_TYPE) -> WeatherService:
    return context.bot_data[SERVICE_KEY]


def clear_weather_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(STATE_KEY, None)


def menu_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("🌡 Текущая погода", callback_data="wx:cur:me")],
        [InlineKeyboardButton("📅 Прогноз на 7 дней", callback_data="wx:fc:me")],
        [InlineKeyboardButton("🔎 Найти город", callback_data="wx:find")],
        [InlineKeyboardButton("📍 Мой город", callback_data="wx:my")],
        [InlineKeyboardButton("🔄 Обновить прогноз", callback_data="wx:fc:me:r")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="home")],
    ]


def weather_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(menu_rows())


def menu_button() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("🌤 Меню погоды", callback_data="wx:menu")]


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="wx:cancel")]])


async def private_chat(update: Update) -> bool:
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Погода доступна только в личном чате с ботом.")
    return False


def command_text(update: Update) -> str:
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) == 2 else ""


# ---------- Background delivery ----------

def run_background(update: Update, context: ContextTypes.DEFAULT_TYPE, coroutine) -> None:
    # PTB passes errors of the task to the error handler together with the update.
    task = context.application.create_task(coroutine, update=update, name="weather")
    tasks = context.bot_data.setdefault(TASKS_KEY, set())
    tasks.add(task)

    def finished(done) -> None:
        tasks.discard(done)
        if not done.cancelled():
            done.exception()  # already passed to the error handler; mark it as retrieved

    task.add_done_callback(finished)


async def begin_loading(update: Update) -> Message | None:
    """Show "⏳" at once: edit the menu for a button, or reply to a typed message."""
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
        await placeholder.edit_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await update.effective_message.reply_text(text, reply_markup=markup)


# ---------- Places ----------

def remember_place(context: ContextTypes.DEFAULT_TYPE, place: Place) -> str:
    places = context.user_data.setdefault(PLACES_KEY, {})
    data = asdict(place)
    for nonce, known in places.items():
        if known == data:
            return nonce
    nonce = secrets.token_hex(4)
    places[nonce] = data
    while len(places) > MAX_PLACES:
        del places[next(iter(places))]
    return nonce


def recall_place(context: ContextTypes.DEFAULT_TYPE, nonce: str) -> Place | None:
    data = (context.user_data.get(PLACES_KEY) or {}).get(nonce)
    return Place(**data) if data else None


async def saved_place(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[Place, str] | None:
    return await asyncio.to_thread(store.get_location, get_database(context), user_id)


# ---------- Weather ----------

def result_markup(kind: str, ref: str) -> InlineKeyboardMarkup:
    other, label = ("fc", "📅 Прогноз на 7 дней") if kind == "cur" else ("cur", "🌡 Погода сейчас")
    rows = [[InlineKeyboardButton("🔄 Обновить", callback_data=f"wx:{kind}:{ref}:r"),
             InlineKeyboardButton(label, callback_data=f"wx:{other}:{ref}")]]
    if ref != "me":
        rows.append([InlineKeyboardButton("📍 Сделать моим городом", callback_data=f"wx:save:{ref}")])
    rows.append(menu_button())
    return InlineKeyboardMarkup(rows)


async def fetch_and_show(
    update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder: Message | None, place: Place,
    kind: str, ref: str, force: bool = False, notice: str = "",
) -> None:
    service = get_service(context)
    try:
        result = await (service.current if kind == "cur" else service.forecast)(place, force)
    except WeatherError as error:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Повторить", callback_data=f"wx:{kind}:{ref}:r")], menu_button(),
        ])
        await deliver(update, placeholder, f"{notice}\n\n{unavailable(error)}" if notice else unavailable(error),
                      markup)
        return
    text = (format_current if kind == "cur" else format_forecast)(place, result, service.clock())
    await deliver(update, placeholder, f"{notice}\n\n{text}" if notice else text, result_markup(kind, ref))


async def show_place_weather(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place: Place, kind: str, ref: str, force: bool = False,
) -> None:
    placeholder = await begin_loading(update)
    run_background(update, context, fetch_and_show(update, context, placeholder, place, kind, ref, force))


async def show_saved_weather(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, force: bool = False,
) -> None:
    saved = await saved_place(context, update.effective_user.id)
    if saved is None:
        await begin_search(update, context, "set", "📍 Основной город ещё не выбран.\n\n")
        return
    await show_place_weather(update, context, saved[0], kind, "me", force)


# ---------- Menu and saved city ----------

async def show_weather_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    if not await private_chat(update):
        return
    clear_weather_state(context)
    saved = await saved_place(context, update.effective_user.id)
    city = (f"Мой город: {place_label(saved[0])}" if saved
            else "Город не выбран — нажми «🔎 Найти город» или отправь /setcity Москва.")
    header = f"{notice}\n\n" if notice else ""
    await respond(update, f"{header}🌤 Погода: выбери действие.\n{city}", weather_menu())


async def show_my_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await saved_place(context, update.effective_user.id)
    if saved is None:
        await respond(update, "📍 Основной город не выбран.\n\nВыбери его командой /setcity Москва "
                              "или кнопкой ниже.", InlineKeyboardMarkup([
                                  [InlineKeyboardButton("🔎 Выбрать город", callback_data="wx:find:set")],
                                  menu_button(),
                              ]))
        return
    place, updated_at = saved
    await respond(update, (
        f"{place_card(place)}\nСохранён: {format_date(updated_at)}\n\n"
        "Часовой пояс погоды не влияет на напоминания — для них есть /timezone."
    ), InlineKeyboardMarkup([
        [InlineKeyboardButton("🌡 Погода сейчас", callback_data="wx:cur:me"),
         InlineKeyboardButton("📅 Прогноз", callback_data="wx:fc:me")],
        [InlineKeyboardButton("🔄 Сменить город", callback_data="wx:find:set")],
        menu_button(),
    ]))


async def save_place(update: Update, context: ContextTypes.DEFAULT_TYPE, place: Place) -> None:
    await asyncio.to_thread(store.save_location, get_database(context), update.effective_user.id, place)


# ---------- City search ----------

async def begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE, purpose: str, notice: str = "") -> None:
    context.user_data[STATE_KEY] = {"action": "search", "purpose": purpose}
    await respond(update, (
        f"{notice}Введи название населённого пункта ({MIN_QUERY_LENGTH}–{MAX_QUERY_LENGTH} символов), "
        "например: Москва, Нижний Новгород, Paris. Отмена: /cancel."
    ), cancel_markup())


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, purpose: str) -> None:
    try:
        query = clean_query(text)
    except ValueError as exc:
        context.user_data[STATE_KEY] = {"action": "search", "purpose": purpose}
        await respond(update, f"{exc} Попробуй ещё раз или нажми /cancel.", cancel_markup())
        return
    clear_weather_state(context)
    placeholder = await begin_loading(update)
    run_background(update, context, finish_search(update, context, placeholder, query, purpose))


async def finish_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder: Message | None, query: str, purpose: str,
) -> None:
    again = [InlineKeyboardButton("🔎 Искать снова", callback_data=f"wx:find:{purpose}")]
    try:
        places = (await get_service(context).search(query))[:MAX_CHOICES]
    except WeatherError as error:
        await deliver(update, placeholder, (
            f"⚠️ Не удалось выполнить поиск через Open-Meteo: {error_reason(error)}.\n\n"
            "Проверь доступ к интернету (или VPN) на компьютере, где запущен бот, и попробуй позже."
        ), InlineKeyboardMarkup([again, menu_button()]))
        return
    if not places:
        await deliver(update, placeholder, f"Ничего не найдено по запросу «{query}». "
                                           "Проверь написание или попробуй другое название.",
                      InlineKeyboardMarkup([again, menu_button()]))
        return
    if len(places) == 1:
        await choose(update, context, placeholder, places[0], purpose)
        return
    nonce = secrets.token_hex(4)
    context.user_data[CHOICES_KEY] = {"nonce": nonce, "purpose": purpose, "places": [asdict(p) for p in places]}
    lines = [f"🔎 Найдено несколько мест по запросу «{query}». Выбери нужное:", ""]
    buttons = []
    for number, place in enumerate(places, start=1):
        lines.append(f"{number}. {place_label(place)} ({place.latitude:.2f}, {place.longitude:.2f})")
        buttons.append([InlineKeyboardButton(f"{number}. {place_label(place)}"[:60],
                                             callback_data=f"wx:pick:{nonce}:{number - 1}")])
    await deliver(update, placeholder, "\n".join(lines), InlineKeyboardMarkup(buttons + [again, menu_button()]))


async def choose(
    update: Update, context: ContextTypes.DEFAULT_TYPE, placeholder: Message | None, place: Place, purpose: str,
) -> None:
    if purpose == "set":
        await save_place(update, context, place)
        await fetch_and_show(update, context, placeholder, place, "cur", "me",
                             notice=f"📍 Основной город сохранён: {place_label(place)}")
    else:
        await fetch_and_show(update, context, placeholder, place, purpose, remember_place(context, place))


# ---------- Commands ----------

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str = "cur") -> None:
    if not await private_chat(update):
        return
    clear_weather_state(context)
    if text := command_text(update):
        await search(update, context, text, kind)
    else:
        await show_saved_weather(update, context, kind)


async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await weather_command(update, context, "fc")


async def setcity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    if text := command_text(update):
        await search(update, context, text, "set")
    else:
        await begin_search(update, context, "set")


async def mycity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_weather_state(context)
    await show_my_city(update, context)


async def cancel_weather_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle /cancel when a weather dialog is active; False lets other sections handle it."""
    if STATE_KEY not in (context.user_data or {}):
        return False
    if await private_chat(update):
        await show_weather_menu(update, context, "Действие отменено.")
    return True


async def consume_weather_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = (context.user_data or {}).get(STATE_KEY)
    if not state or state.get("action") != "search":
        return False
    if await private_chat(update):
        await search(update, context, update.effective_message.text or "", state["purpose"])
    return True


# ---------- Callback buttons ----------

async def weather_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await private_chat(update):
        return
    data = query.data if isinstance(query.data, str) else ""
    if data == "wx:menu":
        await show_weather_menu(update, context)
    elif data == "wx:cancel":
        await show_weather_menu(update, context, "Действие отменено.")
    elif data == "wx:my":
        clear_weather_state(context)
        await show_my_city(update, context)
    elif match := re.fullmatch(r"wx:find(?::(set|cur|fc))?", data):
        await begin_search(update, context, match[1] or "cur")
    elif match := re.fullmatch(r"wx:(cur|fc):(me|[0-9a-f]{8})(:r)?", data):
        clear_weather_state(context)
        kind, ref, force = match[1], match[2], bool(match[3])
        if ref == "me":
            await show_saved_weather(update, context, kind, force)
        elif (place := recall_place(context, ref)) is None:
            await respond(update, STALE, weather_menu())
        else:
            await show_place_weather(update, context, place, kind, ref, force)
    elif match := re.fullmatch(r"wx:save:([0-9a-f]{8})", data):
        clear_weather_state(context)
        if (place := recall_place(context, match[1])) is None:
            await respond(update, STALE, weather_menu())
            return
        await save_place(update, context, place)
        await show_weather_menu(update, context, f"📍 Основной город сохранён: {place_label(place)}")
    elif match := re.fullmatch(r"wx:pick:([0-9a-f]{8}):([0-9])", data):
        choices = context.user_data.get(CHOICES_KEY) or {}
        index = int(match[2])
        if choices.get("nonce") != match[1] or index >= len(choices.get("places", ())):
            await respond(update, "Результаты поиска устарели. Начни новый поиск.", weather_menu())
            return
        clear_weather_state(context)
        place = Place(**choices["places"][index])
        placeholder = await begin_loading(update)
        run_background(update, context, choose(update, context, placeholder, place, choices["purpose"]))
    else:
        await respond(update, STALE, weather_menu())


async def close_weather(application: Application) -> None:
    """post_shutdown: close the shared HTTP client."""
    service = application.bot_data.get(SERVICE_KEY)
    if service is not None:
        await service.aclose()


def register_weather_handlers(application: Application) -> None:
    application.bot_data.setdefault(SERVICE_KEY, WeatherService())
    for command, callback in (
        ("weather", weather_command), ("forecast", forecast_command), ("setcity", setcity_command),
        ("mycity", mycity_command),
    ):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(weather_callback, pattern=r"^wx:"))
