"""Open-Meteo client, cache, formatting and storage with a fake HTTP transport; no internet."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from database import Database
from reminder_store import get_timezone, set_timezone
import weather_format as fmt
import weather_service as ws
import weather_store as store

# Expected warnings about failed requests would otherwise clutter the test output.
logging.getLogger("weather_service").addHandler(logging.NullHandler())

NOW = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc).timestamp()  # 14:30 in Moscow

MOSCOW = {"id": 524901, "name": "Москва", "latitude": 55.75222, "longitude": 37.61556, "country": "Россия",
          "admin1": "Москва", "timezone": "Europe/Moscow", "country_code": "RU"}
PARIS_FR = {"id": 2988507, "name": "Париж", "latitude": 48.85341, "longitude": 2.3488, "country": "Франция",
            "admin1": "Иль-де-Франс", "timezone": "Europe/Paris"}
PARIS_US = {"id": 4717560, "name": "Париж", "latitude": 33.66094, "longitude": -95.55551, "country": "США",
            "admin1": "Техас", "timezone": "America/Chicago"}
GEO = {"москва": [MOSCOW], "париж": [PARIS_FR, PARIS_US], "berlin": [
    {"id": 2950159, "name": "Берлин", "latitude": 52.52437, "longitude": 13.41053, "country": "Германия",
     "admin1": "Берлин", "timezone": "Europe/Berlin"}]}


def current_payload(**overrides):
    payload = {
        "latitude": 55.75, "longitude": 37.625, "timezone": "Europe/Moscow", "utc_offset_seconds": 10800,
        "current_units": {"time": "iso8601", "temperature_2m": "°C", "apparent_temperature": "°C",
                          "relative_humidity_2m": "%", "wind_speed_10m": "km/h", "wind_direction_10m": "°",
                          "weather_code": "wmo code", "precipitation": "mm", "is_day": ""},
        "current": {"time": "2026-10-16T14:30", "interval": 900, "temperature_2m": 15.2,
                    "apparent_temperature": 12.6, "relative_humidity_2m": 65, "wind_speed_10m": 14.4,
                    "wind_direction_10m": 315, "weather_code": 3, "precipitation": 0.4, "is_day": 1},
    }
    payload.update(overrides)
    return payload


def daily_payload(start=date(2026, 10, 16), offset=10800, zone="Europe/Moscow"):
    days = [(start + timedelta(days=n)).isoformat() for n in range(7)]
    return {
        "latitude": 55.75, "longitude": 37.625, "timezone": zone, "utc_offset_seconds": offset,
        "daily_units": {"time": "iso8601", "weather_code": "wmo code", "temperature_2m_max": "°C",
                        "temperature_2m_min": "°C", "precipitation_probability_max": "%", "precipitation_sum": "mm"},
        "daily": {"time": days, "weather_code": [0, 3, 61, 71, 95, 45, 80],
                  "temperature_2m_max": [9.4, 8.0, 5.5, 1.2, 12.0, 7.0, 6.0],
                  "temperature_2m_min": [2.6, -0.4, -3.6, -5.0, 4.0, 1.0, 0.0],
                  "precipitation_probability_max": [0, 10, 80, 60, 90, 5, 70],
                  "precipitation_sum": [0.0, 0.0, 4.2, 2.0, 15.5, 0.0, 3.1]},
    }


class FakeOpenMeteo:
    """Routes requests by host; `failures` are consumed one per HTTP attempt before success."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.failures: list = []
        self.current = current_payload()
        self.daily = daily_payload()
        self.delay = 0.0

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def params(self, n=-1):
        return {k: v[0] for k, v in parse_qs(self.requests[n].url.query.decode()).items()}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        if request.url.host == "geocoding-api.open-meteo.com":
            name = request.url.params["name"].casefold()
            results = GEO.get(name)
            return httpx.Response(200, json={"results": results} if results else {"generationtime_ms": 0.1})
        if "current" in request.url.params:
            return httpx.Response(200, json=self.current)
        return httpx.Response(200, json=self.daily)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def dns_error():
    try:
        raise socket.gaierror(-2, "Name or service not known")
    except socket.gaierror as cause:
        try:
            raise httpx.ConnectError("[Errno -2] Name or service not known") from cause
        except httpx.ConnectError as exc:
            return exc


def make_service(fake, clock=None):
    return ws.WeatherService(transport=fake.transport, clock=clock or Clock(), retry_delays=(0, 0))


MOSCOW_PLACE = ws.Place("Москва", "Россия", "Москва", 55.75222, 37.61556, "Europe/Moscow", 524901)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenMeteo()
        self.clock = Clock()
        self.service = make_service(self.fake, self.clock)
        self.addAsyncCleanup(self.service.aclose)

    async def test_russian_query_returns_full_place(self):
        places = await self.service.search("  москва ")
        self.assertEqual(places, [MOSCOW_PLACE])
        params = self.fake.params()
        self.assertEqual((params["name"], params["language"]), ("москва", "ru"))

    async def test_same_name_in_different_countries(self):
        places = await self.service.search("Париж")
        self.assertEqual([(p.country, p.region, p.timezone) for p in places],
                         [("Франция", "Иль-де-Франс", "Europe/Paris"), ("США", "Техас", "America/Chicago")])

    async def test_latin_query_and_nothing_found(self):
        self.assertEqual((await self.service.search("Berlin"))[0].country, "Германия")
        self.assertEqual(await self.service.search("Нетакогогорода"), [])

    async def test_query_length_is_checked_before_request(self):
        for text in ("", "   ", "М", "а" * (ws.MAX_QUERY_LENGTH + 1)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                await self.service.search(text)
        self.assertEqual(self.fake.requests, [])

    async def test_invalid_results_are_skipped_and_zone_checked(self):
        self.fake.failures.append(httpx.Response(200, json={"results": [
            {"name": "Без координат"}, "мусор", {"name": "Плохая широта", "latitude": 91, "longitude": 0},
            {"name": "X\u0000Y", "latitude": 1, "longitude": 2, "timezone": "Not/AZone", "id": True},
        ]}))
        places = await self.service.search("Тест")
        self.assertEqual(places, [ws.Place("XY", "", "", 1.0, 2.0, "", None)])

    async def test_geocoding_is_cached_for_a_day(self):
        await self.service.search("Москва")
        await self.service.search("москва")
        self.assertEqual(self.service.requests, 2)  # the query text is part of the key
        await self.service.search("Москва")
        self.assertEqual(self.service.requests, 2)
        self.clock.now += ws.GEOCODING_TTL + 1
        await self.service.search("Москва")
        self.assertEqual(self.service.requests, 3)


class WeatherRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenMeteo()
        self.clock = Clock()
        self.service = make_service(self.fake, self.clock)
        self.addAsyncCleanup(self.service.aclose)

    async def test_current_request_parameters_and_parsing(self):
        result = await self.service.current(MOSCOW_PLACE)
        params = self.fake.params()
        self.assertEqual((params["latitude"], params["longitude"], params["timezone"]),
                         ("55.7522", "37.6156", "Europe/Moscow"))
        self.assertEqual((params["temperature_unit"], params["wind_speed_unit"], params["precipitation_unit"]),
                         ("celsius", "kmh", "mm"))
        self.assertIn("weather_code", params["current"])
        self.assertFalse(result.stale)
        self.assertEqual(result.data.values["temperature_2m"], 15.2)
        self.assertEqual(result.data.units["wind_speed_10m"], "km/h")

    async def test_forecast_request_is_seven_days_in_city_zone(self):
        result = await self.service.forecast(MOSCOW_PLACE)
        params = self.fake.params()
        self.assertEqual((params["forecast_days"], params["timezone"]), ("7", "Europe/Moscow"))
        self.assertEqual(len(result.data.days), 7)
        self.assertEqual(result.data.utc_offset, 10800)

    async def test_unknown_zone_is_resolved_by_open_meteo(self):
        await self.service.current(ws.Place("Где-то", "", "", 10, 20, ""))
        self.assertEqual(self.fake.params()["timezone"], "auto")

    async def test_cache_and_ttl(self):
        await self.service.current(MOSCOW_PLACE)
        self.clock.now += ws.CURRENT_TTL - 1
        await self.service.current(MOSCOW_PLACE)
        self.assertEqual(self.service.requests, 1)
        self.clock.now += 2
        await self.service.current(MOSCOW_PLACE)
        self.assertEqual(self.service.requests, 2)
        await self.service.forecast(MOSCOW_PLACE)
        self.clock.now += ws.FORECAST_TTL - 1
        await self.service.forecast(MOSCOW_PLACE)
        self.assertEqual(self.service.requests, 3)
        self.clock.now += 2
        await self.service.forecast(MOSCOW_PLACE)
        self.assertEqual(self.service.requests, 4)

    async def test_cache_key_includes_place_zone_and_kind(self):
        await self.service.current(MOSCOW_PLACE)
        await self.service.forecast(MOSCOW_PLACE)
        await self.service.current(ws.Place("Москва", "Россия", "", 55.76, 37.61556, "Europe/Moscow"))
        await self.service.current(ws.Place("Москва", "Россия", "", 55.75222, 37.61556, "UTC"))
        self.assertEqual(self.service.requests, 4)

    async def test_refresh_is_throttled(self):
        await self.service.forecast(MOSCOW_PLACE)
        result = await self.service.forecast(MOSCOW_PLACE, force=True)
        self.assertTrue(result.throttled)
        self.assertEqual(self.service.requests, 1)
        self.clock.now += ws.MIN_REFRESH_INTERVAL
        result = await self.service.forecast(MOSCOW_PLACE, force=True)
        self.assertFalse(result.throttled)
        self.assertEqual(self.service.requests, 2)

    async def test_identical_concurrent_requests_share_one_call(self):
        self.fake.delay = 0.05
        first, second = await asyncio.gather(self.service.current(MOSCOW_PLACE), self.service.current(MOSCOW_PLACE))
        self.assertEqual(self.service.requests, 1)
        self.assertEqual(first.data, second.data)

    async def test_stale_current_is_marked_and_limited_to_an_hour(self):
        fresh = await self.service.current(MOSCOW_PLACE)
        self.clock.now += ws.CURRENT_TTL + 60
        self.fake.failures = [httpx.ConnectTimeout("timed out")] * 3
        result = await self.service.current(MOSCOW_PLACE)
        self.assertTrue(result.stale)
        self.assertEqual(result.error.reason, "timeout")
        self.assertEqual(result.fetched_at, fresh.fetched_at)
        self.clock.now = fresh.fetched_at + ws.CURRENT_STALE_LIMIT + 1
        self.fake.failures = [httpx.ConnectTimeout("timed out")] * 3
        with self.assertRaises(ws.WeatherError):
            await self.service.current(MOSCOW_PLACE)

    async def test_stale_forecast_is_limited_to_six_hours(self):
        await self.service.forecast(MOSCOW_PLACE)
        self.clock.now += ws.FORECAST_STALE_LIMIT - 1
        self.fake.failures = [httpx.Response(503)] * 3
        self.assertTrue((await self.service.forecast(MOSCOW_PLACE)).stale)
        self.clock.now += 2
        self.fake.failures = [httpx.Response(503)] * 3
        with self.assertRaises(ws.WeatherError):
            await self.service.forecast(MOSCOW_PLACE)

    async def test_failed_refresh_falls_back_to_cache(self):
        await self.service.current(MOSCOW_PLACE)
        self.clock.now += ws.MIN_REFRESH_INTERVAL
        self.fake.failures = [httpx.ReadTimeout("slow")] * 3
        result = await self.service.current(MOSCOW_PLACE, force=True)
        self.assertTrue(result.stale)

    async def test_no_cache_and_network_error(self):
        self.fake.failures = [httpx.ConnectError("refused")] * 3
        with self.assertRaises(ws.WeatherError) as caught:
            await self.service.current(MOSCOW_PLACE)
        self.assertEqual(caught.exception.reason, "network")
        self.assertEqual(self.service.requests, 3)

    async def test_error_kinds_and_retries(self):
        cases = [
            (dns_error(), "dns", 3), (httpx.ConnectTimeout("t"), "timeout", 3),
            (httpx.ReadTimeout("t"), "timeout", 3), (httpx.Response(500), "server", 3),
            (httpx.Response(503), "server", 3), (httpx.Response(429), "rate_limit", 3),
            (httpx.Response(429, headers={"Retry-After": "120"}), "rate_limit", 1),
            (httpx.Response(400, json={"error": True, "reason": "bad"}), "rejected", 1),
            (httpx.Response(200, content=b"<html>not json"), "bad_response", 1),
            (httpx.Response(200, json={"current": "oops"}), "bad_response", 1),
            (httpx.Response(200, json=[1, 2]), "bad_response", 1),
        ]
        for failure, reason, attempts in cases:
            with self.subTest(reason=reason, failure=failure):
                service = make_service(self.fake, self.clock)
                self.fake.failures = [failure] * 3
                with self.assertRaises(ws.WeatherError) as caught:
                    await service.current(MOSCOW_PLACE)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(service.requests, attempts)
                await service.aclose()

    async def test_transient_error_then_success(self):
        self.fake.failures = [httpx.Response(503), httpx.ConnectTimeout("t")]
        result = await self.service.current(MOSCOW_PLACE)
        self.assertFalse(result.stale)
        self.assertEqual(self.service.requests, 3)

    async def test_retry_waits_between_attempts(self):
        delays = []

        async def sleep(seconds):
            delays.append(seconds)

        service = ws.WeatherService(transport=self.fake.transport, clock=self.clock, sleep=sleep)
        self.addAsyncCleanup(service.aclose)
        self.fake.failures = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(500)]
        await service.current(MOSCOW_PLACE)
        self.assertEqual(delays, [3.0, ws.RETRY_DELAYS[1]])

    async def test_total_deadline(self):
        self.fake.delay = 1
        with patch.object(ws, "TOTAL_TIMEOUT", 0.05), self.assertRaises(ws.WeatherError) as caught:
            await self.service.current(MOSCOW_PLACE)
        self.assertEqual(caught.exception.reason, "timeout")

    async def test_wrong_column_shapes_are_treated_as_missing(self):
        payload = daily_payload()
        payload["daily"]["precipitation_sum"] = [1.0]
        payload["daily"]["temperature_2m_max"][0] = "warm"
        self.fake.daily = payload
        days = (await self.service.forecast(MOSCOW_PLACE)).data.days
        self.assertNotIn("precipitation_sum", days[0].values)
        self.assertNotIn("temperature_2m_max", days[0].values)
        self.assertEqual(days[1].values["temperature_2m_max"], 8.0)

    async def test_client_is_shared_and_closed(self):
        await self.service.current(MOSCOW_PLACE)
        client = self.service._client
        await self.service.forecast(MOSCOW_PLACE)
        self.assertIs(self.service._client, client)
        await self.service.aclose()
        self.assertTrue(client.is_closed)


def result(data, **kwargs):
    return ws.Result(data, kwargs.pop("fetched_at", NOW), **kwargs)


class FormatTests(unittest.TestCase):
    def test_every_documented_wmo_code_has_russian_text(self):
        for code in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77,
                     80, 81, 82, 85, 86, 95, 96, 99):
            icon, text = fmt.describe_code(code)
            self.assertTrue(icon and text and not text.startswith("Погодный код"), code)
        self.assertEqual(fmt.describe_code(0)[1], "Ясно")
        self.assertEqual(fmt.describe_code(0, is_day=0)[0], "🌙")
        self.assertEqual(fmt.describe_code(45)[1], "Туман")
        self.assertIn("Гроза", fmt.describe_code(95)[1])
        self.assertEqual(fmt.describe_code(42)[1], "Погодный код 42")
        self.assertIsNone(fmt.describe_code(None))

    def test_current_message(self):
        text = fmt.format_current(MOSCOW_PLACE, result(ws.parse_current(current_payload())), NOW)
        for part in ("🌤 Погода — Москва", "Россия", "🌡 Температура: +15 °C", "🤔 Ощущается как: +13 °C",
                     "💧 Влажность: 65%", "💨 Ветер: 14 км/ч (≈ 4 м/с), СЗ (315°)", "☁️ Пасмурно",
                     "☔ Осадки: 0,4 мм", "🕒 Данные на 16.10.2026 14:30 (местное время)", "Open-Meteo"):
            self.assertIn(part, text)
        self.assertNotIn("Москва, Москва", text)

    def test_missing_values_are_omitted(self):
        payload = current_payload()
        payload["current"] = {"time": "2026-10-16T14:30", "temperature_2m": -0.4, "wind_speed_10m": None}
        text = fmt.format_current(MOSCOW_PLACE, result(ws.parse_current(payload)), NOW)
        self.assertIn("🌡 Температура: 0 °C", text)
        for absent in ("Ощущается", "Влажность", "Ветер", "Осадки", "Пасмурно"):
            self.assertNotIn(absent, text)

    def test_units_come_from_the_response(self):
        payload = current_payload()
        payload["current_units"].update({"wind_speed_10m": "m/s", "temperature_2m": "°F"})
        text = fmt.format_current(MOSCOW_PLACE, result(ws.parse_current(payload)), NOW)
        self.assertIn("💨 Ветер: 14 м/с, СЗ", text)
        self.assertNotIn("км/ч", text)
        self.assertIn("+15 °F", text)

    def test_negative_temperatures(self):
        self.assertEqual(fmt.temperature(-3.6, {}, "temperature_2m"), "−4 °C")
        self.assertEqual(fmt.temperature(0.4, {}, "temperature_2m"), "0 °C")

    def test_forecast_message(self):
        text = fmt.format_forecast(MOSCOW_PLACE, result(ws.parse_forecast(daily_payload())), NOW)
        self.assertIn("📅 Прогноз на 7 дней — Москва, Россия", text)
        self.assertIn("Сегодня, 16.10 — ☀️ Ясно", text)
        self.assertIn("Завтра, 17.10 — ☁️ Пасмурно", text)
        self.assertIn("Вс, 18.10 — 🌦 Небольшой дождь", text)
        self.assertIn("🌡 −4…+6 °C · ☔ 80% · 💧 4,2 мм", text)
        self.assertIn("Чт, 22.10", text)
        self.assertIn("Europe/Moscow", text)

    def test_forecast_days_follow_city_zone_not_computer(self):
        # 03:00 UTC on 17.10 is still 16.10 in Texas (UTC−5).
        now = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc).timestamp()
        place = ws.Place("Париж", "США", "Техас", 33.66, -95.55, "America/Chicago")
        data = ws.parse_forecast(daily_payload(offset=-18000, zone="America/Chicago"))
        with patch.dict("os.environ", {"TZ": "Asia/Tokyo"}):
            text = fmt.format_forecast(place, result(data, fetched_at=now), now)
        self.assertIn("Сегодня, 16.10", text)

    def test_stale_forecast_hides_past_days_and_says_it_is_old(self):
        data = ws.parse_forecast(daily_payload())
        now = NOW + 11 * 3600  # 16.10 22:30 UTC = 17.10 01:30 in Moscow
        stale = result(data, stale=True, error=ws.WeatherError("timeout"))
        text = fmt.format_forecast(MOSCOW_PLACE, stale, now)
        self.assertIn("Последний сохранённый прогноз", text)
        self.assertIn("сервер не ответил вовремя", text)
        self.assertIn("полученные 16.10 14:30 по местному времени, 11 ч назад", text)
        self.assertNotIn("16.10 —", text)
        self.assertIn("Сегодня, 17.10", text)

    def test_stale_current_is_not_presented_as_current(self):
        data = ws.parse_current(current_payload())
        text = fmt.format_current(MOSCOW_PLACE, result(data, stale=True, error=ws.WeatherError("dns")), NOW + 1800)
        self.assertTrue(text.startswith("⚠️ Open-Meteo сейчас недоступен: не удалось определить адрес сервера"))
        self.assertIn("Это не текущие данные", text)
        self.assertNotIn("🌤 Погода —", text)

    def test_message_fits_telegram_limit(self):
        long = ws.Place("😀" * 3000, "😀" * 100, "", 1, 2, "UTC")
        for text in (fmt.format_forecast(long, result(ws.parse_forecast(daily_payload())), NOW),
                     fmt.format_current(long, result(ws.parse_current(current_payload())), NOW),
                     fmt.fit_message("a" * 5000)):
            self.assertLessEqual(fmt.utf16_length(text), fmt.MESSAGE_LIMIT)
        self.assertEqual(fmt.fit_message("short"), "short")

    def test_names_are_limited_and_cleaned(self):
        places = ws.parse_places({"results": [{"name": "Город\n" + "а" * 500, "latitude": 1, "longitude": 1}]})
        self.assertEqual(len(places[0].name), ws.MAX_NAME_LENGTH)
        self.assertNotIn("\n", places[0].name)


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.db"
        self.db = Database(self.path)
        self.db.initialize()
        for user in (1, 2):
            self.db.upsert_user(user, f"U{user}")

    def test_save_update_and_isolation(self):
        self.assertIsNone(store.get_location(self.db, 1))
        store.save_location(self.db, 1, MOSCOW_PLACE)
        self.assertEqual(store.get_location(self.db, 1)[0], MOSCOW_PLACE)
        self.assertIsNone(store.get_location(self.db, 2))
        paris = ws.Place("Париж", "Франция", "Иль-де-Франс", 48.85, 2.35, "Europe/Paris", 1)
        store.save_location(self.db, 2, paris)
        store.save_location(self.db, 1, paris)
        self.assertEqual(store.get_location(self.db, 1)[0], paris)
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_locations").fetchone()[0], 2)

    def test_invalid_place_is_rejected(self):
        for place in (ws.Place(" ", "", "", 1, 1, ""), ws.Place("X", "", "", 95, 1, "")):
            with self.subTest(place=place), self.assertRaises(ValueError):
                store.save_location(self.db, 1, place)

    def test_weather_city_and_reminder_zone_are_independent(self):
        set_timezone(self.db, 1, "Asia/Yekaterinburg")
        store.save_location(self.db, 1, MOSCOW_PLACE)
        self.assertEqual(get_timezone(self.db, 1), "Asia/Yekaterinburg")
        set_timezone(self.db, 1, "UTC")
        self.assertEqual(store.get_location(self.db, 1)[0].timezone, "Europe/Moscow")

    def test_initialize_keeps_existing_data(self):
        self.db.add_task(1, "Задача")
        self.db.add_note(1, "Заметка", "Текст")
        store.save_location(self.db, 1, MOSCOW_PLACE)
        self.db.initialize()
        self.assertEqual(self.db.list_tasks(1).total, 1)
        self.assertEqual(self.db.list_notes(1).total, 1)
        self.assertEqual(store.get_location(self.db, 1)[0], MOSCOW_PLACE)

    def test_incompatible_locations_table_is_reported_not_changed(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE user_locations (user_id INTEGER PRIMARY KEY, city TEXT)")
            conn.execute("INSERT INTO user_locations VALUES (1, 'Москва')")
        conn.close()
        with self.assertRaises(RuntimeError):
            Database(path).initialize()
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT city FROM user_locations").fetchone()[0], "Москва")
        conn.close()


if __name__ == "__main__":
    unittest.main()
