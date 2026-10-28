"""RSS/Atom news: download, parse, normalise, merge, cache and search.

Only allow-listed sources (news_sources.json) are fetched. Every request and redirect is
checked: http/https only, a host of that source, and no loopback, private or link-local
addresses. Responses are limited in size, XML entity declarations are refused, and feed
text is reduced to plain text: RSS is untrusted content, and nothing in it is executed
or followed. Article links are shown as given by the source and never fetched.

One shared httpx.AsyncClient lives for the whole application. Each source is cached in
memory for CACHE_TTL; if it stops answering, its last articles are used, marked as stale,
for up to STALE_LIMIT. Identical requests running at the same time share one download,
and a slow source never delays the answer longer than REFRESH_DEADLINE.
"""

import asyncio
import calendar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import io
import ipaddress
import logging
import re
import socket
import time
from typing import Awaitable, Callable, Iterable
import urllib.request
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import feedparser
import httpx

from database import fold_text
from news_sources import CATEGORIES, Source, host_of
from weather_service import _is_dns_error

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 4.0     # seconds; without VPN the TCP handshake may hang
READ_TIMEOUT = 8.0
SOURCE_TIMEOUT = 15.0     # one source including retries and redirects
REFRESH_DEADLINE = 20.0   # the user gets an answer by then; slower sources finish in background
RETRY_DELAYS = (1.5,)     # one retry for transient failures
MAX_RETRY_AFTER = 5.0     # a longer Retry-After on HTTP 429 is not waited for
MAX_REDIRECTS = 3
MAX_FEED_BYTES = 2_000_000
MAX_CONCURRENT = 4        # simultaneous downloads

CACHE_TTL = 15 * 60
STALE_LIMIT = 6 * 60 * 60       # older articles are not shown even if nothing newer is available
MIN_REFRESH_INTERVAL = 2 * 60   # "Refresh" does not re-download a source more often than this
FAILURE_PAUSE = 60              # a source that just failed is not retried on every click

MAX_ITEMS_PER_SOURCE = 30
MAX_TITLE_LENGTH = 250
MAX_SUMMARY_LENGTH = 300
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100
FUTURE_TOLERANCE = timedelta(days=1)  # later publication dates are treated as unknown
EARLIEST_DATE = datetime(1995, 1, 1, tzinfo=timezone.utc)

USER_AGENT = "personal-assistant-bot/1.0 (personal RSS reader, non-commercial)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
TRACKING_PARAMS = re.compile(r"(utm_[a-z0-9_]+|fbclid|gclid|yclid|mc_cid|mc_eid|at_medium|at_campaign|_openstat)")
# Fake-IP range of TUN-mode VPN clients (xray, sing-box): public sites resolve into it.
VPN_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


class NewsError(Exception):
    """A failed source; `reason` is one of REASONS."""

    REASONS = frozenset({
        "dns", "timeout", "network", "rate_limit", "server", "rejected", "too_large", "forbidden_url",
        "bad_feed",
    })
    TRANSIENT = frozenset({"dns", "timeout", "network", "rate_limit", "server"})

    def __init__(self, reason: str, detail: str = "", retry_after: str | None = None) -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True)
class Article:
    id: str                       # hash of the normalised link
    title: str
    url: str                      # exactly as given by the source
    source_id: str
    source_name: str
    category: str
    summary: str                  # plain text, may be empty
    published_at: datetime | None  # UTC; None when the source gives no trustworthy date
    fetched_at: datetime          # UTC

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "url": self.url, "source_id": self.source_id,
            "source_name": self.source_name, "category": self.category, "summary": self.summary,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fetched_at": self.fetched_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Article":
        published = data.get("published_at")
        return cls(**{**data, "published_at": datetime.fromisoformat(published) if published else None,
                      "fetched_at": datetime.fromisoformat(data["fetched_at"])})


@dataclass(frozen=True)
class Collection:
    articles: tuple[Article, ...]
    stale: tuple[tuple[Source, float, NewsError | None], ...] = ()  # source, fetched_at, why not fresh
    failed: tuple[tuple[Source, NewsError | None], ...] = ()       # no usable data; None: still loading
    throttled: bool = False   # "Refresh" came too soon, cached articles are shown


@dataclass(frozen=True)
class _Entry:
    articles: tuple[Article, ...]
    fetched_at: float


# ---------- Text and links ----------

class _TextExtractor(HTMLParser):
    SKIP = frozenset({"script", "style", "template", "noscript", "iframe", "object", "svg", "math", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        else:
            self.parts.append(" ")  # <p>, <br>, <li>… separate words

    def handle_endtag(self, tag) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        else:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs) -> None:
        self.parts.append(" ")

    def handle_data(self, data) -> None:
        if not self.skipping:
            self.parts.append(data)


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:—-") + "…"


def plain_text(value: object, limit: int, html: bool = True) -> str:
    """Untrusted feed text -> one line of plain text: no tags, scripts, entities or control characters."""
    if not isinstance(value, str):
        return ""
    if html:
        parser = _TextExtractor()
        try:
            parser.feed(value)
            parser.close()
        except Exception:  # HTMLParser is lenient, but the input is hostile
            return ""
        value = "".join(parser.parts)
    # isprintable() also drops bidi overrides and zero-width characters that could disguise text.
    value = "".join(ch if ch.isprintable() else " " for ch in value)
    return shorten(" ".join(value.split()), limit)


def _detail_is_html(detail: object) -> bool:
    kind = detail.get("type", "") if isinstance(detail, dict) else ""
    return kind in ("text/html", "application/xhtml+xml", "")


def normalize_url(url: str) -> str:
    """Comparison key for duplicates: scheme, www., default port, fragment, trailing slash and
    tracking parameters do not matter, other query parameters are kept in sorted order."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".").removeprefix("www.")
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    query = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if not TRACKING_PARAMS.fullmatch(key.lower()))
    return f"{netloc}{path}" + (f"?{urlencode(query)}" if query else "")


def article_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


def entry_date(entry: dict, fetched_at: datetime) -> datetime | None:
    """Publication date in UTC, falling back to the source's own "updated" date; never the fetch time."""
    for key in ("published_parsed", "updated_parsed"):
        # dict.get: FeedParserDict.get would map a missing key to the other one, with a warning.
        value = dict.get(entry, key)
        if not value:
            continue
        try:
            moment = datetime.fromtimestamp(calendar.timegm(value), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if EARLIEST_DATE <= moment <= fetched_at + FUTURE_TOLERANCE:
            return moment
    return None


def parse_feed(body: bytes, source: Source, fetched_at: datetime, content_type: str = "") -> list[Article]:
    """RSS 0.9x/1.0/2.0 or Atom -> normalised articles in feed order."""
    if re.search(rb"<!ENTITY", body, re.IGNORECASE):
        # Feeds have no use for entity declarations; refusing them rules out entity expansion attacks.
        raise NewsError("bad_feed", "entity declarations")
    # BytesIO: given bytes or str, feedparser may treat the data as a file path or URL.
    parsed = feedparser.parse(io.BytesIO(body), response_headers={
        "content-type": content_type or "application/xml", "content-location": source.url,
    })
    if not parsed.get("version") and not parsed.entries:
        raise NewsError("bad_feed", type(parsed.get("bozo_exception")).__name__ if parsed.bozo else "not a feed")
    articles, seen_ids, seen_guids = [], set(), set()
    for entry in parsed.entries:
        link = entry.get("link")
        if host_of(link) is None:
            continue  # relative, javascript:, data: and other links are dropped
        title = plain_text(entry.get("title"), MAX_TITLE_LENGTH, _detail_is_html(entry.get("title_detail")))
        if not title:
            continue
        guid = entry.get("id") if isinstance(entry.get("id"), str) else None
        key = article_id(link)
        if key in seen_ids or (guid and guid in seen_guids):
            continue
        seen_ids.add(key)
        if guid:
            seen_guids.add(guid)
        summary = plain_text(entry.get("summary"), MAX_SUMMARY_LENGTH,
                             _detail_is_html(entry.get("summary_detail"))) if source.summaries else ""
        articles.append(Article(
            id=key, title=title, url=link, source_id=source.id, source_name=source.name,
            category=source.category, summary="" if summary.casefold() == title.casefold() else summary,
            published_at=entry_date(entry, fetched_at), fetched_at=fetched_at,
        ))
        if len(articles) >= MAX_ITEMS_PER_SOURCE:
            break
    return articles


def merge(groups: Iterable[Iterable[Article]]) -> list[Article]:
    """Newest first, undated articles last in feed order; the first copy of a duplicate wins."""
    ordered = sorted(
        (article for group in groups for article in group),
        key=lambda a: (a.published_at is None, -a.published_at.timestamp() if a.published_at else 0),
    )
    seen, result = set(), []
    for article in ordered:
        if article.id not in seen:
            seen.add(article.id)
            result.append(article)
    return result


def clean_query(text: str) -> str:
    query = " ".join(text.split())
    if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
        raise ValueError(f"Запрос должен содержать от {MIN_QUERY_LENGTH} до {MAX_QUERY_LENGTH} символов.")
    return query


def search_articles(articles: Iterable[Article], query: str) -> list[Article]:
    """Every word of the query must occur in the title or summary; case and «ё»/«е» are ignored."""
    words = fold_text(clean_query(query)).split()
    return [article for article in articles
            if all(word in fold_text(f"{article.title} {article.summary}") for word in words)]


# ---------- Address checks ----------

def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in VPN_FAKE_IP:
        return True
    return ip.is_global and not ip.is_multicast


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def proxied(url: str) -> bool:
    """True when httpx will send this URL through a proxy from the environment (trust_env)."""
    parts = urlsplit(url)
    return bool(urllib.request.getproxies().get(parts.scheme)) and not urllib.request.proxy_bypass(parts.hostname)


# ---------- Service ----------

class NewsService:
    def __init__(
        self, sources: Iterable[Source], *, transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time, retry_delays: tuple[float, ...] = RETRY_DELAYS,
        sleep=asyncio.sleep, resolve: Callable[[str, int], Awaitable[list[str]]] = system_resolve,
    ) -> None:
        self.sources = tuple(sources)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, _Entry] = {}
        self._failures: dict[str, tuple[float, NewsError]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.clock = clock
        self.retry_delays = retry_delays
        self._sleep = sleep
        self._resolve = resolve
        self.deadline = REFRESH_DEADLINE
        self.requests = 0  # HTTP attempts made; for diagnostics and tests

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Redirects are followed by hand so that every hop is checked. TLS verification stays on.
            self._client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=False,
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                # identity: a compressed response could expand far beyond MAX_FEED_BYTES in one chunk.
                headers={"User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": "identity"},
            )
        return self._client

    async def aclose(self) -> None:
        pending = [future for future in self._inflight.values() if not future.done()]
        for future in pending:
            future.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- HTTP ----------

    async def _check_url(self, url: str, source: Source) -> None:
        host = host_of(url)
        if host is None or host not in source.hosts:
            raise NewsError("forbidden_url", "host not allowed")
        if proxied(url):
            return  # the proxy resolves the name; the host allow-list still applies
        port = urlsplit(url).port or (443 if url.startswith("https:") else 80)
        try:
            addresses = await self._resolve(host, port)
        except (socket.gaierror, UnicodeError):
            raise NewsError("dns", host) from None
        except OSError as exc:
            raise NewsError("network", type(exc).__name__) from None
        if not addresses or not all(public_address(address) for address in addresses):
            raise NewsError("forbidden_url", "non-public address")

    async def _download(self, source: Source) -> tuple[bytes, str]:
        url = source.url
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_url(url, source)
            self.requests += 1
            try:
                async with self._get_client().stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise NewsError("rejected", "redirect without location")
                        url = urljoin(url, location)
                        continue
                    if response.status_code == 429:
                        raise NewsError("rate_limit", "HTTP 429", response.headers.get("Retry-After"))
                    if response.status_code >= 500:
                        raise NewsError("server", f"HTTP {response.status_code}")
                    if response.status_code != 200:
                        raise NewsError("rejected", f"HTTP {response.status_code}")
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > MAX_FEED_BYTES:
                        raise NewsError("too_large", length)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_FEED_BYTES:
                            raise NewsError("too_large", "body")
                    return bytes(body), response.headers.get("content-type", "")
            except httpx.TimeoutException as exc:
                raise NewsError("timeout", type(exc).__name__) from None
            except httpx.TransportError as exc:
                # Includes httpx.DecodingError: an encoding the server applied despite "identity".
                raise NewsError("dns" if _is_dns_error(exc) else "network", type(exc).__name__) from None
        raise NewsError("forbidden_url", "too many redirects")

    async def _fetch(self, source: Source) -> tuple[Article, ...]:
        """Limited retries for transient failures, all within SOURCE_TIMEOUT."""
        async with self._semaphore:
            try:
                async with asyncio.timeout(SOURCE_TIMEOUT):
                    for attempt in range(len(self.retry_delays) + 1):
                        try:
                            body, content_type = await self._download(source)
                            break
                        except NewsError as error:
                            if error.reason not in NewsError.TRANSIENT or attempt == len(self.retry_delays):
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
                raise NewsError("timeout", "source deadline") from None
        fetched_at = datetime.fromtimestamp(self.clock(), timezone.utc)
        # Parsing a large feed takes tens of milliseconds; keep the event loop (and JobQueue) free.
        return tuple(await asyncio.to_thread(parse_feed, body, source, fetched_at, content_type))

    async def _refresh(self, source: Source) -> None:
        """Download one source into the cache; failures are recorded, never raised."""
        try:
            articles = await self._fetch(source)
        except NewsError as error:
            logger.warning("Источник новостей %s недоступен: %s", source.id, error)
            self._failures[source.id] = (self.clock(), error)
            return
        except Exception:
            logger.exception("Ошибка обработки источника новостей %s", source.id)
            self._failures[source.id] = (self.clock(), NewsError("bad_feed", "unexpected"))
            return
        self._failures.pop(source.id, None)
        self._cache[source.id] = _Entry(articles, self.clock())

    def _start(self, source: Source) -> asyncio.Future:
        """One download per source at a time; callers share it."""
        future = self._inflight.get(source.id)
        if future is None:
            future = asyncio.ensure_future(self._refresh(source))
            self._inflight[source.id] = future

            def forget(done, key=source.id):
                if self._inflight.get(key) is done:
                    del self._inflight[key]

            future.add_done_callback(forget)
        return future

    # ---------- Public API ----------

    def sources_for(self, categories: Iterable[str]) -> tuple[Source, ...]:
        wanted = set(categories)
        return tuple(source for source in self.sources if source.category in wanted)

    async def collect(self, categories: Iterable[str] = CATEGORIES, force: bool = False) -> Collection:
        sources = self.sources_for(categories)
        now = self.clock()
        throttled, due = False, []
        for source in sources:
            entry = self._cache.get(source.id)
            age = now - entry.fetched_at if entry else None
            failed_at = self._failures.get(source.id, (None,))[0]
            if age is not None and age < (MIN_REFRESH_INTERVAL if force else CACHE_TTL):
                throttled |= force
            elif failed_at is not None and now - failed_at < FAILURE_PAUSE and source.id not in self._inflight:
                throttled |= force
            else:
                due.append(source)
        if due:
            # shield: an abandoned request (deadline, cancelled handler) keeps filling the cache.
            await asyncio.wait([asyncio.shield(self._start(source)) for source in due], timeout=self.deadline)
        now = self.clock()
        groups, stale, failed = [], [], []
        for source in sources:
            entry = self._cache.get(source.id)
            error = self._failures.get(source.id, (None, None))[1]
            if entry is None or now - entry.fetched_at > STALE_LIMIT:
                failed.append((source, error))
                continue
            groups.append(entry.articles)
            if now - entry.fetched_at > CACHE_TTL or (error is not None and source in due):
                stale.append((source, entry.fetched_at, error))
        return Collection(tuple(merge(groups)), tuple(stale), tuple(failed), throttled)

    async def search(self, query: str, categories: Iterable[str] = CATEGORIES, force: bool = False) -> Collection:
        query = clean_query(query)
        collection = await self.collect(categories, force)
        return replace(collection, articles=tuple(search_articles(collection.articles, query)))


async def check_sources() -> None:
    """python -m news_service: download every enabled source once and print a summary."""
    from news_sources import load_sources

    service = NewsService(load_sources())
    service.deadline = SOURCE_TIMEOUT * 2
    try:
        await service.collect()
        for source in service.sources:
            entry = service._cache.get(source.id)
            if entry is not None:
                dated = sum(article.published_at is not None for article in entry.articles)
                print(f"OK    {source.id:16} {len(entry.articles):3} материалов, с датой {dated:3}  {source.url}")
            else:
                error = service._failures.get(source.id, (None, "нет ответа"))[1]
                print(f"ОШИБКА {source.id:15} {error}  {source.url}")
    finally:
        await service.aclose()


if __name__ == "__main__":
    asyncio.run(check_sources())
