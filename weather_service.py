"""Open-Meteo client: geocoding, current weather and a 7-day forecast.

One shared httpx.AsyncClient lives for the whole application. Successful responses are
cached in memory; if Open-Meteo stops answering, recent cached data is returned marked
as stale. Identical requests running at the same time share one HTTP call.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
import logging
import math
import socket
import time
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

CONNECT_TIMEOUT = 4.0   # seconds; without VPN the TCP handshake may hang
READ_TIMEOUT = 8.0
TOTAL_TIMEOUT = 15.0    # whole operation including retries
RETRY_DELAYS = (1.0, 2.0)  # at most two retries
MAX_RETRY_AFTER = 5.0   # a longer Retry-After on HTTP 429 is not waited for

CURRENT_TTL = 10 * 60
FORECAST_TTL = 30 * 60
GEOCODING_TTL = 24 * 60 * 60
CURRENT_STALE_LIMIT = 60 * 60
FORECAST_STALE_LIMIT = 6 * 60 * 60
MIN_REFRESH_INTERVAL = 60  # "Refresh" does not hit the API more often than this
CACHE_SIZE = 256

MIN_QUERY_LENGTH = 2  # Open-Meteo returns nothing for a single character
MAX_QUERY_LENGTH = 100
SEARCH_COUNT = 10
MAX_NAME_LENGTH = 100

CURRENT_FIELDS = (
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m",
    "wind_direction_10m", "weather_code", "precipitation", "is_day",
)
DAILY_FIELDS = (
    "weather_code", "temperature_2m_max", "temperature_2m_min",
    "precipitation_probability_max", "precipitation_sum",
)
UNITS = {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"}
FORECAST_DAYS = 7


class WeatherError(Exception):
    """A failed Open-Meteo request; `reason` is one of REASONS."""

    REASONS = frozenset({"dns", "timeout", "network", "rate_limit", "server", "rejected", "bad_response"})

    def __init__(self, reason: str, detail: str = "", retry_after: str | None = None) -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.retry_after = retry_after  # HTTP 429 Retry-After header, if any


@dataclass(frozen=True)
class Place:
    name: str
    country: str
    region: str
    latitude: float
    longitude: float
    timezone: str
    geocoding_id: int | None = None


@dataclass(frozen=True)
class Current:
    time: str                  # local ISO time of the observation, e.g. 2026-10-16T14:30
    timezone: str
    values: dict[str, float]   # only fields present in the response
    units: dict[str, str]


@dataclass(frozen=True)
class Day:
    date: date
    values: dict[str, float]


@dataclass(frozen=True)
class Forecast:
    days: tuple[Day, ...]
    timezone: str
    utc_offset: int            # seconds, at the moment of the request
    units: dict[str, str]


@dataclass(frozen=True)
class Result:
    data: object
    fetched_at: float          # epoch seconds
    stale: bool = False        # fresh data could not be fetched; `error` says why
    error: WeatherError | None = None
    throttled: bool = False    # a refresh was asked too soon, cached data is returned


@dataclass
class _Entry:
    data: object
    fetched_at: float


def clean_query(text: str) -> str:
    query = " ".join(text.split())
    if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
        raise ValueError(
            f"Название населённого пункта должно содержать от {MIN_QUERY_LENGTH} до {MAX_QUERY_LENGTH} символов."
        )
    return query


def valid_zone(name: object) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return False
    return True


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _text(value: object) -> str:
    # Names go to Telegram as plain text; drop control characters and runaway lengths.
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch for ch in value if ch.isprintable()).split())[:MAX_NAME_LENGTH]


def _units(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def parse_places(payload: object) -> list[Place]:
    if not isinstance(payload, dict):
        raise WeatherError("bad_response", "geocoding is not an object")
    results = payload.get("results", [])  # absent when nothing is found
    if not isinstance(results, list):
        raise WeatherError("bad_response", "geocoding results")
    places = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name, lat, lon = _text(item.get("name")), _number(item.get("latitude")), _number(item.get("longitude"))
        if not name or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        geo_id = item.get("id")
        places.append(Place(
            name=name, country=_text(item.get("country")), region=_text(item.get("admin1")),
            latitude=float(lat), longitude=float(lon),
            # An unknown zone is resolved by Open-Meteo from the coordinates ("auto").
            timezone=item["timezone"] if valid_zone(item.get("timezone")) else "",
            geocoding_id=geo_id if isinstance(geo_id, int) and not isinstance(geo_id, bool) else None,
        ))
    return places


def parse_current(payload: object) -> Current:
    if not isinstance(payload, dict) or not isinstance(payload.get("current"), dict):
        raise WeatherError("bad_response", "no current block")
    block = payload["current"]
    if not isinstance(block.get("time"), str):
        raise WeatherError("bad_response", "no current time")
    values = {field: _number(block.get(field)) for field in CURRENT_FIELDS}
    return Current(
        time=block["time"], timezone=payload.get("timezone") if valid_zone(payload.get("timezone")) else "",
        values={k: v for k, v in values.items() if v is not None}, units=_units(payload.get("current_units")),
    )


def parse_forecast(payload: object) -> Forecast:
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        raise WeatherError("bad_response", "no daily block")
    block = payload["daily"]
    dates = block.get("time")
    if not isinstance(dates, list) or not dates:
        raise WeatherError("bad_response", "no daily time")
    try:
        days_dates = [date.fromisoformat(value) for value in dates]
    except (TypeError, ValueError):
        raise WeatherError("bad_response", "daily dates") from None
    columns = {}
    for field in DAILY_FIELDS:
        column = block.get(field)
        # A column of the wrong shape is treated as missing rather than misaligned.
        if isinstance(column, list) and len(column) == len(dates):
            columns[field] = column
    days = tuple(
        Day(day, {f: v for f, column in columns.items() if (v := _number(column[i])) is not None})
        for i, day in enumerate(days_dates)
    )
    offset = _number(payload.get("utc_offset_seconds"))
    return Forecast(
        days=days, timezone=payload.get("timezone") if valid_zone(payload.get("timezone")) else "",
        utc_offset=int(offset) if offset is not None else 0, units=_units(payload.get("daily_units")),
    )


def _is_dns_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, socket.gaierror):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


class WeatherService:
    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time,
        retry_delays: tuple[float, ...] = RETRY_DELAYS, sleep=asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[tuple, _Entry] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self.clock = clock
        self.retry_delays = retry_delays
        self._sleep = sleep
        self.requests = 0  # HTTP attempts made; for diagnostics and tests

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # trust_env (default) honours HTTPS_PROXY if the system routes traffic through a proxy.
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                headers={"User-Agent": "personal-assistant-bot (educational, non-commercial)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- HTTP ----------

    async def _attempt(self, url: str, params: dict) -> object:
        self.requests += 1
        try:
            response = await self._get_client().get(url, params=params)
        except httpx.TimeoutException as exc:
            raise WeatherError("timeout", type(exc).__name__) from None
        except httpx.TransportError as exc:
            raise WeatherError("dns" if _is_dns_error(exc) else "network", type(exc).__name__) from None
        if response.status_code == 429:
            raise WeatherError("rate_limit", "HTTP 429", response.headers.get("Retry-After"))
        if response.status_code >= 500:
            raise WeatherError("server", f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise WeatherError("rejected", f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise WeatherError("bad_response", "invalid JSON") from None

    async def _get_json(self, url: str, params: dict) -> object:
        """Limited retries for transient failures, all within TOTAL_TIMEOUT."""
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                for attempt in range(len(self.retry_delays) + 1):
                    try:
                        return await self._attempt(url, params)
                    except WeatherError as error:
                        if error.reason in ("rejected", "bad_response") or attempt == len(self.retry_delays):
                            raise
                        delay = self.retry_delays[attempt]
                        if error.reason == "rate_limit":
                            try:
                                wait = float(error.retry_after or 0)
                            except ValueError:
                                wait = 0
                            if wait > MAX_RETRY_AFTER:
                                raise
                            delay = max(delay, wait)
                        await self._sleep(delay)
        except TimeoutError:
            raise WeatherError("timeout", "total deadline") from None
        raise AssertionError("unreachable")

    async def _shared(self, key: tuple, url: str, params: dict, parse: Callable[[object], object]) -> object:
        """Run one HTTP request per key at a time; concurrent callers await the same result."""
        future = self._inflight.get(key)
        if future is None:
            async def fetch():
                return parse(await self._get_json(url, params))

            future = asyncio.ensure_future(fetch())
            self._inflight[key] = future

            def forget(done, key=key):
                if self._inflight.get(key) is done:
                    del self._inflight[key]

            future.add_done_callback(forget)
        # shield: one cancelled caller must not cancel the request for the others.
        return await asyncio.shield(future)

    # ---------- Cache ----------

    def _remember(self, key: tuple, data: object, fetched_at: float) -> None:
        self._cache[key] = _Entry(data, fetched_at)
        if len(self._cache) > CACHE_SIZE:
            oldest = min(self._cache, key=lambda k: self._cache[k].fetched_at)
            del self._cache[oldest]

    async def _cached(
        self, kind: str, url: str, params: dict, parse, ttl: float, stale_limit: float, force: bool = False,
    ) -> Result:
        # Every parameter sent to Open-Meteo (coordinates, zone, fields, units) is part of the key.
        key = (kind, tuple(sorted((k, str(v)) for k, v in params.items())))
        entry = self._cache.get(key)
        now = self.clock()
        if entry is not None:
            age = now - entry.fetched_at
            if force and age < MIN_REFRESH_INTERVAL:
                return Result(entry.data, entry.fetched_at, throttled=True)
            if not force and age < ttl:
                return Result(entry.data, entry.fetched_at)
        try:
            data = await self._shared(key, url, params, parse)
        except WeatherError as error:
            logger.warning("Open-Meteo (%s) недоступен: %s", kind, error)
            entry = self._cache.get(key)
            if entry is not None and self.clock() - entry.fetched_at <= stale_limit:
                return Result(entry.data, entry.fetched_at, stale=True, error=error)
            raise
        fetched_at = self.clock()
        self._remember(key, data, fetched_at)
        return Result(data, fetched_at)

    # ---------- Public API ----------

    async def search(self, query: str) -> list[Place]:
        query = clean_query(query)
        params = {"name": query, "count": SEARCH_COUNT, "language": "ru", "format": "json"}
        result = await self._cached("geo", GEOCODING_URL, params, parse_places, GEOCODING_TTL, GEOCODING_TTL)
        return list(result.data)

    @staticmethod
    def _location_params(place: Place) -> dict:
        return {
            "latitude": f"{place.latitude:.4f}", "longitude": f"{place.longitude:.4f}",
            "timezone": place.timezone or "auto", **UNITS,
        }

    async def current(self, place: Place, force: bool = False) -> Result:
        params = {**self._location_params(place), "current": ",".join(CURRENT_FIELDS)}
        return await self._cached(
            "current", FORECAST_URL, params, parse_current, CURRENT_TTL, CURRENT_STALE_LIMIT, force,
        )

    async def forecast(self, place: Place, force: bool = False) -> Result:
        params = {**self._location_params(place), "daily": ",".join(DAILY_FIELDS), "forecast_days": FORECAST_DAYS}
        return await self._cached(
            "forecast", FORECAST_URL, params, parse_forecast, FORECAST_TTL, FORECAST_STALE_LIMIT, force,
        )
