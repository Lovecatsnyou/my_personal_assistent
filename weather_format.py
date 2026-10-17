"""Plain-text weather messages. Only values present in the Open-Meteo response are shown.

Messages are sent without parse_mode, so city names need no HTML/Markdown escaping.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from weather_service import Current, Forecast, Place, Result, WeatherError, valid_zone

MESSAGE_LIMIT = 4096
SOURCE = "Источник: Open-Meteo (open-meteo.com)"

# WMO weather interpretation codes as documented by Open-Meteo.
WMO_CODES = {
    0: ("☀️", "Ясно"),
    1: ("🌤", "Преимущественно ясно"),
    2: ("⛅", "Переменная облачность"),
    3: ("☁️", "Пасмурно"),
    45: ("🌫", "Туман"),
    48: ("🌫", "Туман с изморозью"),
    51: ("🌦", "Слабая морось"),
    53: ("🌦", "Морось"),
    55: ("🌧", "Сильная морось"),
    56: ("🧊", "Слабая ледяная морось"),
    57: ("🧊", "Сильная ледяная морось"),
    61: ("🌦", "Небольшой дождь"),
    63: ("🌧", "Дождь"),
    65: ("🌧", "Сильный дождь"),
    66: ("🧊", "Слабый ледяной дождь"),
    67: ("🧊", "Сильный ледяной дождь"),
    71: ("🌨", "Небольшой снег"),
    73: ("🌨", "Снег"),
    75: ("❄️", "Сильный снег"),
    77: ("🌨", "Снежные зёрна"),
    80: ("🌦", "Слабый ливень"),
    81: ("🌧", "Ливень"),
    82: ("🌧", "Сильный ливень"),
    85: ("🌨", "Небольшой снегопад"),
    86: ("❄️", "Сильный снегопад"),
    95: ("⛈", "Гроза"),
    96: ("⛈", "Гроза с небольшим градом"),
    99: ("⛈", "Гроза с сильным градом"),
}
NIGHT_ICONS = {0: "🌙", 1: "🌙"}
UNIT_NAMES = {"°C": "°C", "°F": "°F", "km/h": "км/ч", "m/s": "м/с", "mph": "миль/ч", "kn": "уз",
              "mm": "мм", "inch": "дюйм", "%": "%", "°": "°"}
REQUESTED_UNITS = {"temperature": "°C", "wind": "km/h", "precipitation": "mm"}
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
COMPASS = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")
REASONS = {
    "dns": "не удалось определить адрес сервера (ошибка DNS)",
    "timeout": "сервер не ответил вовремя",
    "network": "нет соединения с сервером",
    "rate_limit": "слишком много запросов к сервису (HTTP 429)",
    "server": "ошибка на стороне сервиса",
    "rejected": "сервис отклонил запрос",
    "bad_response": "сервис вернул некорректный ответ",
}


def describe_code(code: float | None, is_day: float | None = None) -> tuple[str, str] | None:
    if code is None:
        return None
    code = int(code)
    icon, text = WMO_CODES.get(code, ("🌡", f"Погодный код {code}"))
    if is_day == 0 and code in NIGHT_ICONS:
        icon = NIGHT_ICONS[code]
    return icon, text


def unit(units: dict[str, str], field: str, kind: str) -> str:
    # Label values with what the API says it returned, not with what was requested.
    raw = units.get(field) or REQUESTED_UNITS[kind]
    return UNIT_NAMES.get(raw, raw)


def temperature(value: float, units: dict[str, str], field: str) -> str:
    rounded = round(value)
    sign = "+" if rounded > 0 else "−" if rounded < 0 else ""
    return f"{sign}{abs(rounded)} {unit(units, field, 'temperature')}"


def amount(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def wind_direction(degrees: float) -> str:
    return f"{COMPASS[round(degrees % 360 / 45) % 8]} ({round(degrees) % 360}°)"


def place_area(place: Place) -> str:
    """Region and country; a region named like the city itself is not repeated."""
    parts = [place.region] if place.region and place.region != place.name else []
    if place.country:
        parts.append(place.country)
    return ", ".join(parts)


def place_label(place: Place) -> str:
    return ", ".join(filter(None, (place.name, place_area(place))))


def place_card(place: Place) -> str:
    lines = [f"📍 {place.name}"]
    if place.region and place.region != place.name:
        lines.append(f"Регион: {place.region}")
    if place.country:
        lines.append(f"Страна: {place.country}")
    lines.append(f"Координаты: {place.latitude:.4f}, {place.longitude:.4f}")
    lines.append(f"Часовой пояс: {place.timezone or 'определяется Open-Meteo по координатам'}")
    return "\n".join(lines)


def zone_of(*names: str) -> ZoneInfo | timezone:
    for name in names:
        if valid_zone(name):
            return ZoneInfo(name)
    return timezone.utc


def fetched_note(result: Result, zone, now: float) -> str:
    moment = datetime.fromtimestamp(result.fetched_at, zone).strftime("%d.%m %H:%M")
    minutes = max(0, int(now - result.fetched_at) // 60)
    if minutes == 0:
        ago = "меньше минуты назад"
    elif minutes < 60:
        ago = f"{minutes} мин назад"
    else:
        ago = f"{minutes // 60} ч {minutes % 60} мин назад" if minutes % 60 else f"{minutes // 60} ч назад"
    return f"{moment} по местному времени, {ago}"


def stale_header(result: Result, zone, now: float) -> list[str]:
    if result.stale:
        return [
            f"⚠️ Open-Meteo сейчас недоступен: {error_reason(result.error)}.",
            f"Показаны последние сохранённые данные, полученные {fetched_note(result, zone, now)}. "
            "Это не текущие данные.",
        ]
    if result.throttled:
        return ["ℹ️ Данные обновлялись меньше минуты назад — показаны они."]
    return []


def error_reason(error: WeatherError | None) -> str:
    return REASONS.get(error.reason, "неизвестная ошибка") if error else "неизвестная ошибка"


def unavailable(error: WeatherError) -> str:
    return (f"⚠️ Не удалось получить данные Open-Meteo: {error_reason(error)}.\n\n"
            "Сохранённых недавних данных нет. Проверь доступ к интернету (или VPN) на компьютере, "
            "где запущен бот, и попробуй позже.")


def format_current(place: Place, result: Result, now: float) -> str:
    data: Current = result.data
    values, units = data.values, data.units
    zone = zone_of(place.timezone, data.timezone)
    title = "🗂 Последние сохранённые данные" if result.stale else "🌤 Погода"
    lines = stale_header(result, zone, now)
    lines.append(f"{title} — {place.name}")
    if area := place_area(place):
        lines.append(area)
    lines.append("")
    if "temperature_2m" in values:
        lines.append(f"🌡 Температура: {temperature(values['temperature_2m'], units, 'temperature_2m')}")
    if "apparent_temperature" in values:
        lines.append(f"🤔 Ощущается как: {temperature(values['apparent_temperature'], units, 'apparent_temperature')}")
    if "relative_humidity_2m" in values:
        lines.append(f"💧 Влажность: {round(values['relative_humidity_2m'])}%")
    if "wind_speed_10m" in values:
        speed, speed_unit = values["wind_speed_10m"], unit(units, "wind_speed_10m", "wind")
        wind = f"💨 Ветер: {round(speed)} {speed_unit}"
        if speed_unit == "км/ч":
            wind += f" (≈ {round(speed / 3.6)} м/с)"
        if "wind_direction_10m" in values:
            wind += f", {wind_direction(values['wind_direction_10m'])}"
        lines.append(wind)
    if (description := describe_code(values.get("weather_code"), values.get("is_day"))) is not None:
        lines.append(" ".join(description))
    if "precipitation" in values:
        lines.append(f"☔ Осадки: {amount(values['precipitation'])} {unit(units, 'precipitation', 'precipitation')}")
    try:
        observed = datetime.fromisoformat(data.time).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        observed = data.time
    lines += ["", f"🕒 Данные на {observed} (местное время)", SOURCE]
    return fit_message("\n".join(lines))


def local_today(forecast: Forecast, now: float) -> date:
    # The city's own offset from the response, never the computer's zone.
    return (datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=forecast.utc_offset)).date()


def format_forecast(place: Place, result: Result, now: float) -> str:
    data: Forecast = result.data
    units = data.units
    zone = zone_of(place.timezone, data.timezone)
    today = local_today(data, now)
    title = "🗂 Последний сохранённый прогноз" if result.stale else "📅 Прогноз на 7 дней"
    lines = stale_header(result, zone, now)
    lines += [f"{title} — {place_label(place)}", ""]
    # Days that are already over in the city (possible with stale data) are not shown.
    days = [day for day in data.days if day.date >= today]
    if not days:
        lines.append("Нет данных на ближайшие дни.")
    for day in days:
        values = day.values
        label = {today: "Сегодня", today + timedelta(days=1): "Завтра"}.get(day.date, WEEKDAYS[day.date.weekday()])
        heading = f"{label}, {day.date.strftime('%d.%m')}"
        if (description := describe_code(values.get("weather_code"))) is not None:
            heading += f" — {' '.join(description)}"
        details = []
        low, high = values.get("temperature_2m_min"), values.get("temperature_2m_max")
        if low is not None and high is not None:
            details.append(f"🌡 {temperature(low, units, 'temperature_2m_min').split()[0]}…"
                           f"{temperature(high, units, 'temperature_2m_max')}")
        elif high is not None:
            details.append(f"🌡 макс. {temperature(high, units, 'temperature_2m_max')}")
        elif low is not None:
            details.append(f"🌡 мин. {temperature(low, units, 'temperature_2m_min')}")
        if "precipitation_probability_max" in values:
            details.append(f"☔ {round(values['precipitation_probability_max'])}%")
        if "precipitation_sum" in values:
            details.append(f"💧 {amount(values['precipitation_sum'])} "
                           f"{unit(units, 'precipitation_sum', 'precipitation')}")
        lines.append(heading)
        if details:
            lines.append("    " + " · ".join(details))
    zone_name = place.timezone or data.timezone or "UTC"
    lines += ["", f"Даты указаны по местному времени ({zone_name}).", SOURCE]
    return fit_message("\n".join(lines))


def utf16_length(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def fit_message(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Telegram counts UTF-16 code units; cut the tail instead of failing to send."""
    if utf16_length(text) <= limit:
        return text
    size, kept = 0, []
    for ch in text:
        size += 2 if ord(ch) > 0xFFFF else 1
        if size > limit - 1:
            break
        kept.append(ch)
    return "".join(kept) + "…"
