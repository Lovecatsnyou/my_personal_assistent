"""RSS parsing, normalisation, network safety and cache with a fake HTTP transport; no internet."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx

import news_service as ns
from news_sources import CATEGORIES, SOURCES_PATH, Source, find_category, load_sources
from tests.test_weather_service import Clock, dns_error

# Expected warnings about failed sources would otherwise clutter the test output.
logging.getLogger("news_service").addHandler(logging.NullHandler())
logging.getLogger("news_service").propagate = False

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp()

TECH = Source("tech", "Tech Daily", "https://feeds.example.com/tech.xml", "ai_tech", frozenset({"feeds.example.com"}))
GAMES = Source("games", "Игровой канал", "https://games.example.org/rss", "games",
               frozenset({"games.example.org", "mirror.example.org"}))
SCIENCE = Source("sci", "Наука сегодня", "https://science.example.net/atom", "science",
                 frozenset({"science.example.net"}))
SOURCES = (TECH, GAMES, SCIENCE)


def item(title="Новость", link="https://example.com/a", date="Sat, 17 Oct 2026 10:00:00 +0000",
         description=None, guid=None) -> str:
    parts = []
    if title is not None:
        parts.append(f"<title>{title}</title>")
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if date is not None:
        parts.append(f"<pubDate>{date}</pubDate>")
    if description is not None:
        parts.append(f"<description>{description}</description>")
    if guid is not None:
        parts.append(f'<guid isPermaLink="false">{guid}</guid>')
    return f"<item>{''.join(parts)}</item>"


def rss(*items: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Feed</title>'
            '<link>https://example.com/</link><description>d</description>'
            + "".join(items) + "</channel></rss>").encode()


def atom(*entries: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<title>Atom feed</title><id>urn:feed</id><updated>2026-10-17T10:00:00Z</updated>'
            + "".join(entries) + "</feed>").encode()


def entry(title, link, updated="2026-10-17T09:00:00Z", published=None, summary=None, title_type="text") -> str:
    parts = [f'<title type="{title_type}">{title}</title>', f'<link rel="alternate" href="{link}"/>',
             f"<id>urn:{link}</id>", f"<updated>{updated}</updated>"]
    if published:
        parts.append(f"<published>{published}</published>")
    if summary:
        parts.append(f'<summary type="html">{summary}</summary>')
    return f"<entry>{''.join(parts)}</entry>"


FEEDS = {
    TECH.url: rss(
        item("Новый процессор представлен", "https://example.com/tech/cpu?utm_source=rss",
             "Sat, 17 Oct 2026 11:00:00 +0300", "Компания &lt;b&gt;показала&lt;/b&gt; чип"),
        item("Ёлочные игрушки с ИИ", "https://example.com/tech/tree", "Fri, 16 Oct 2026 09:00:00 +0000",
             "Нейросеть рисует украшения"),
    ),
    GAMES.url: rss(
        item("Вышла новая игра", "https://example.org/games/1", "Sat, 17 Oct 2026 09:30:00 +0000"),
        item("Игра без даты", "https://example.org/games/2", None, "Описание"),
    ),
    SCIENCE.url: atom(
        entry("Учёные нашли воду", "https://example.net/sci/water", published="2026-10-17T07:00:00Z",
              summary="&lt;p&gt;Подробности &lt;i&gt;открытия&lt;/i&gt;&lt;/p&gt;"),
    ),
}


class FakeFeeds:
    """Answers by exact URL; a list is consumed one response per request, the last one repeats."""

    def __init__(self, routes=None):
        self.routes = dict(FEEDS if routes is None else routes)
        self.requests: list[httpx.Request] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.active = 0
        self.max_active = 0

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def urls(self):
        return [str(request.url) for request in self.requests]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if url in self.gates:
                await self.gates[url].wait()
            await asyncio.sleep(0)
            route = self.routes.get(url)
            if isinstance(route, list):
                route = route.pop(0) if len(route) > 1 else route[0]
            if route is None:
                return httpx.Response(404)
            if isinstance(route, BaseException):
                raise route
            if isinstance(route, httpx.Response):
                return route
            return httpx.Response(200, content=route, headers={"Content-Type": "application/rss+xml; charset=utf-8"})
        finally:
            self.active -= 1


ADDRESSES = {"internal.example.com": ["10.0.0.5"]}


async def fake_resolve(host, port):
    if host == "nxdomain.example.com":
        raise socket.gaierror(-2, "Name or service not known")
    return ADDRESSES.get(host, ["93.184.216.34"])


def make_service(fake, clock=None, sources=SOURCES, resolve=fake_resolve):
    return ns.NewsService(sources, transport=fake.transport, clock=clock or Clock(NOW), retry_delays=(0,),
                          resolve=resolve)


def parse(body, source=TECH):
    return ns.parse_feed(body, source, datetime.fromtimestamp(NOW, timezone.utc))


class ParsingTests(unittest.TestCase):
    def test_rss20_is_normalised(self):
        articles = parse(FEEDS[TECH.url])
        self.assertEqual(len(articles), 2)
        first = articles[0]
        self.assertEqual(first.title, "Новый процессор представлен")
        self.assertEqual(first.url, "https://example.com/tech/cpu?utm_source=rss")  # shown as given
        self.assertEqual(first.summary, "Компания показала чип")
        self.assertEqual(first.published_at, datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(first.fetched_at, datetime.fromtimestamp(NOW, timezone.utc))
        self.assertEqual((first.source_id, first.source_name, first.category), ("tech", "Tech Daily", "ai_tech"))
        self.assertEqual(len(first.id), 16)
        self.assertEqual(ns.Article.from_dict(first.to_dict()), first)

    def test_atom_published_updated_and_html_title(self):
        articles = parse(atom(
            entry("&lt;b&gt;Жирный&lt;/b&gt; заголовок", "https://example.net/1", published="2026-10-15T10:00:00+02:00",
                  title_type="html"),
            entry("Только updated", "https://example.net/2", updated="2026-10-16T10:00:00Z"),
        ), SCIENCE)
        self.assertEqual([a.title for a in articles], ["Жирный заголовок", "Только updated"])
        self.assertEqual(articles[0].published_at, datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(articles[1].published_at, datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc))

    def test_rss10(self):
        body = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
            <channel rdf:about="https://example.com/"><title>R</title><link>https://example.com/</link></channel>
            <item rdf:about="https://example.com/r1"><title>RSS 1.0 item</title><link>https://example.com/r1</link>
            <dc:date>2026-10-17T08:00:00Z</dc:date></item></rdf:RDF>"""
        [article] = parse(body)
        self.assertEqual(article.title, "RSS 1.0 item")
        self.assertEqual(article.published_at, datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc))

    def test_missing_or_implausible_date_is_unknown(self):
        articles = parse(rss(
            item("Без даты", "https://example.com/1", None),
            item("Из будущего", "https://example.com/2", "Mon, 01 Jan 2035 00:00:00 +0000"),
            item("Мусор вместо даты", "https://example.com/3", "вчера вечером"),
            item("Слишком старая", "https://example.com/4", "Mon, 01 Jan 1990 00:00:00 +0000"),
        ))
        self.assertEqual(len(articles), 4)
        # Never replaced by the fetch time.
        self.assertTrue(all(article.published_at is None for article in articles))

    def test_text_plain_title_is_literal(self):
        [article] = parse(rss(item("Тег &lt;div&gt; &amp; атрибуты", "https://example.com/1")))
        self.assertEqual(article.title, "Тег <div> & атрибуты")

    def test_broken_xml_and_non_feeds(self):
        for body in (b"", b"not xml at all <<<", b"<html><body><h1>Login</h1></body></html>",
                     b'{"items": []}', b"\x00\xff\xfe garbage"):
            with self.subTest(body=body[:20]):
                with self.assertRaises(ns.NewsError) as caught:
                    parse(body)
                self.assertEqual(caught.exception.reason, "bad_feed")

    def test_truncated_feed_keeps_complete_items(self):
        body = rss(item("Целая", "https://example.com/1"))[:-len("</channel></rss>")] + "<item><title>Оборв".encode()
        self.assertEqual([a.title for a in parse(body)], ["Целая"])

    def test_empty_feed_is_not_an_error(self):
        self.assertEqual(parse(rss()), [])

    def test_entity_declarations_are_refused(self):
        xxe = (b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<rss version="2.0"><channel><item><title>&xxe;</title><link>https://example.com/1</link>'
               b"</item></channel></rss>")
        laughs = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
                  + rss(item("&lol2;", "https://example.com/1")).split(b"?>", 1)[1])
        for body in (xxe, laughs, xxe.replace(b"ENTITY", b"entity")):
            with self.assertRaises(ns.NewsError) as caught:
                parse(body)
            self.assertEqual(caught.exception.reason, "bad_feed")

    def test_response_body_is_never_opened_as_a_file(self):
        with tempfile.NamedTemporaryFile(suffix=".xml") as local:
            local.write(rss(item("Локальный файл", "https://example.com/secret")))
            local.flush()
            with self.assertRaises(ns.NewsError):
                parse(local.name.encode())

    def test_items_without_title_or_web_link_are_dropped(self):
        articles = parse(rss(
            item(None, "https://example.com/no-title"),
            item("   ", "https://example.com/blank-title"),
            item("Без ссылки", None),
            item("javascript", "javascript:alert(1)"),
            item("data", "data:text/html,hi"),
            item("ftp", "ftp://example.com/file"),
            item("Пробел в ссылке", "https://example.com/a b"),
            item("С паролем", "https://user:pass@example.com/"),
            item("Нормальная", "http://example.com/ok"),
        ))
        self.assertEqual([a.title for a in articles], ["Нормальная"])

    def test_html_is_reduced_to_safe_plain_text(self):
        description = ("&lt;script&gt;alert('x')&lt;/script&gt;&lt;style&gt;p{}&lt;/style&gt;"
                       "&lt;p&gt;Первый&lt;br/&gt;второй &amp;amp; &lt;a href='javascript:x'&gt;ссылка&lt;/a&gt;"
                       "&lt;img src=x onerror=alert(1)&gt;&lt;/p&gt;‮текст​")
        [article] = parse(rss(item("Заголовок", "https://example.com/1", description=description)))
        self.assertEqual(article.summary, "Первый второй & ссылка текст")
        for bad in ("<", ">", "script", "alert", "‮", "​"):
            self.assertNotIn(bad, article.summary)
        self.assertEqual(ns.plain_text("<b>a</b><script>evil()</script>b", 100), "a b")
        self.assertEqual(ns.plain_text("<p>&lt;b&gt;</p>", 100), "<b>")  # escaped text stays text
        self.assertEqual(ns.plain_text("x\x00y\x1b[31mz", 100), "x y [31mz")

    def test_long_text_is_shortened(self):
        [article] = parse(rss(item("Т" * 400, "https://example.com/1", description="слово " * 200)))
        self.assertEqual(len(article.title), ns.MAX_TITLE_LENGTH)
        self.assertTrue(article.title.endswith("…"))
        self.assertLessEqual(len(article.summary), ns.MAX_SUMMARY_LENGTH)
        self.assertTrue(article.summary.endswith("слово…"))

    def test_source_without_summaries(self):
        source = Source("hn", "HN", "https://hn.example/rss", "programming", frozenset({"hn.example"}), False)
        [article] = parse(rss(item("Show HN", "https://example.com/1", description="&lt;a href='x'&gt;Comments&lt;/a&gt;")),
                          source)
        self.assertEqual(article.summary, "")

    def test_summary_equal_to_title_is_dropped(self):
        [article] = parse(rss(item("Одно и то же", "https://example.com/1", description="одно и то же")))
        self.assertEqual(article.summary, "")

    def test_items_per_source_are_limited(self):
        body = rss(*(item(f"N{n}", f"https://example.com/{n}") for n in range(100)))
        self.assertEqual(len(parse(body)), ns.MAX_ITEMS_PER_SOURCE)

    def test_duplicates_inside_a_feed(self):
        articles = parse(rss(
            item("A", "https://example.com/a?utm_source=rss&utm_medium=x"),
            item("A again", "https://www.example.com/a/#comments"),
            item("Same guid", "https://example.com/other", guid="g-1"),
            item("Same guid 2", "https://example.com/other-2", guid="g-1"),
        ))
        self.assertEqual([a.title for a in articles], ["A", "Same guid"])

    def test_normalize_url(self):
        same = ["https://example.com/a?b=2&a=1", "http://www.example.com/a/?a=1&b=2&utm_campaign=x",
                "https://EXAMPLE.com:443/a?a=1&b=2#top", "https://example.com/a?at_medium=RSS&a=1&b=2&fbclid=1"]
        self.assertEqual({ns.normalize_url(url) for url in same}, {"example.com/a?a=1&b=2"})
        self.assertNotEqual(ns.normalize_url("https://example.com/a?id=1"), ns.normalize_url("https://example.com/a?id=2"))
        self.assertNotEqual(ns.normalize_url("https://example.com:8443/a"), ns.normalize_url("https://example.com/a"))

    def test_merge_sorts_and_removes_cross_source_duplicates(self):
        tech, games = parse(FEEDS[TECH.url]), parse(FEEDS[GAMES.url], GAMES)
        copy = parse(rss(item("Копия", "http://www.example.com/tech/cpu/", "Sat, 17 Oct 2026 11:30:00 +0000")),
                     GAMES)
        merged = ns.merge([tech, games, copy])
        # "Копия" is the same article as "Новый процессор представлен" under another URL form.
        self.assertEqual([a.title for a in merged], [
            "Копия", "Вышла новая игра", "Ёлочные игрушки с ИИ", "Игра без даты",
        ])

    def test_search(self):
        articles = parse(FEEDS[TECH.url]) + parse(FEEDS[GAMES.url], GAMES)
        titles = lambda query: [a.title for a in ns.search_articles(articles, query)]  # noqa: E731
        self.assertEqual(titles("ИГРА"), ["Вышла новая игра", "Игра без даты"])
        self.assertEqual(titles("елочные"), ["Ёлочные игрушки с ИИ"])  # «е» finds «ё»
        self.assertEqual(titles("ёлочные"), ["Ёлочные игрушки с ИИ"])
        self.assertEqual(titles("нейросеть"), ["Ёлочные игрушки с ИИ"])  # summary
        self.assertEqual(titles("ИИ  игрушки"), ["Ёлочные игрушки с ИИ"])  # every word, any order
        self.assertEqual(titles("ПОКАЗАЛА"), ["Новый процессор представлен"])
        self.assertEqual(titles("квантовый компьютер"), [])
        for bad in ("", " ", "я", "x" * (ns.MAX_QUERY_LENGTH + 1)):
            with self.assertRaises(ValueError):
                ns.search_articles(articles, bad)


class AddressTests(unittest.TestCase):
    def test_public_address(self):
        for address in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "198.18.0.7"):
            self.assertTrue(ns.public_address(address), address)
        for address in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                        "::1", "fe80::1%eth0", "fc00::1", "::ffff:127.0.0.1", "100.64.0.1", "224.0.0.1",
                        "255.255.255.255", "not-an-ip"):
            self.assertFalse(ns.public_address(address), address)


class SourceConfigTests(unittest.TestCase):
    def write(self, data) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "sources.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def test_bundled_sources(self):
        sources = load_sources(SOURCES_PATH)
        self.assertGreaterEqual(len(sources), 18)
        self.assertEqual({source.category for source in sources}, set(CATEGORIES))
        self.assertTrue(all(source.url.startswith("https://") for source in sources))

    def test_environment_override_and_disabled_sources(self):
        path = self.write({"sources": [
            {"id": "a", "name": "A", "category": "games", "url": "https://a.example/rss", "hosts": ["cdn.example"]},
            {"id": "b", "name": "B", "category": "games", "url": "https://b.example/rss", "enabled": False},
        ]})
        with patch.dict("os.environ", {"NEWS_SOURCES_FILE": str(path)}):
            [source] = load_sources()
        self.assertEqual(source.hosts, frozenset({"a.example", "cdn.example"}))

    def test_invalid_sources_stop_startup(self):
        good = {"id": "a", "name": "A", "category": "games", "url": "https://a.example/rss"}
        for broken in (
            "{not json", {"items": []},
            {"sources": [{**good, "url": "file:///etc/passwd"}]},
            {"sources": [{**good, "url": "/relative/rss"}]},
            {"sources": [{**good, "url": "https://user:pw@a.example/rss"}]},
            {"sources": [{**good, "category": "sport"}]},
            {"sources": [{**good, "id": "Bad id"}]},
            {"sources": [good, good]},
            {"sources": [{**good, "hosts": "a.example"}]},
            {"sources": [{**good, "enabled": "no"}]},
            {"sources": [{**good, "summaries": 0}]},
        ):
            with self.subTest(broken=broken), self.assertRaises(RuntimeError):
                load_sources(self.write(broken))

    def test_find_category(self):
        for text, key in (("игры", "games"), ("Игры", "games"), ("ИИ", "ai_tech"), ("наука", "science"),
                          ("Мировые  события", "world"), ("экономика", "economy"), ("programming", "programming")):
            self.assertEqual(find_category(text).key, key, text)
        self.assertIsNone(find_category("спорт"))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeFeeds()
        self.clock = Clock(NOW)
        self.service = make_service(self.fake, self.clock)
        self.addAsyncCleanup(self.service.aclose)

    async def test_collect_merges_categories(self):
        collection = await self.service.collect(["ai_tech", "games"])
        self.assertEqual([a.title for a in collection.articles], [
            "Вышла новая игра", "Новый процессор представлен", "Ёлочные игрушки с ИИ", "Игра без даты",
        ])
        self.assertEqual((collection.stale, collection.failed, collection.throttled), ((), (), False))
        self.assertEqual(sorted(self.fake.urls()), sorted([TECH.url, GAMES.url]))
        request = self.fake.requests[0]
        self.assertIn("personal-assistant-bot", request.headers["User-Agent"])
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        science = await self.service.collect(["science"])
        self.assertEqual([a.summary for a in science.articles], ["Подробности открытия"])

    async def test_search_over_loaded_feeds(self):
        result = await self.service.search("ИГРА")
        self.assertEqual([a.title for a in result.articles], ["Вышла новая игра", "Игра без даты"])
        with self.assertRaises(ValueError):
            await self.service.search("x")

    async def test_cache_ttl(self):
        await self.service.collect(["ai_tech"])
        await self.service.collect(["ai_tech"])
        self.assertEqual(len(self.fake.requests), 1)
        self.clock.now += ns.CACHE_TTL + 1
        await self.service.collect(["ai_tech"])
        self.assertEqual(len(self.fake.requests), 2)

    async def test_refresh_is_rate_limited(self):
        await self.service.collect(["ai_tech"])
        self.clock.now += ns.MIN_REFRESH_INTERVAL - 1
        collection = await self.service.collect(["ai_tech"], force=True)
        self.assertTrue(collection.throttled)
        self.assertEqual(len(self.fake.requests), 1)
        self.clock.now += 1
        collection = await self.service.collect(["ai_tech"], force=True)
        self.assertFalse(collection.throttled)
        self.assertEqual(len(self.fake.requests), 2)

    async def test_stale_data_is_marked_and_expires(self):
        await self.service.collect(["ai_tech"])
        fetched = self.clock.now
        self.clock.now += ns.CACHE_TTL + 1
        self.fake.routes[TECH.url] = httpx.Response(503)
        collection = await self.service.collect(["ai_tech"])
        self.assertEqual(len(collection.articles), 2)
        [(source, fetched_at, error)] = collection.stale
        self.assertEqual((source, fetched_at, error.reason), (TECH, fetched, "server"))
        self.clock.now = fetched + ns.STALE_LIMIT + 1
        collection = await self.service.collect(["ai_tech"])
        self.assertEqual(collection.articles, ())
        self.assertEqual([(s.id, e.reason) for s, e in collection.failed], [("tech", "server")])

    async def test_failed_source_is_not_retried_on_every_request(self):
        self.fake.routes[TECH.url] = httpx.Response(404)
        await self.service.collect(["ai_tech"])
        await self.service.collect(["ai_tech"], force=True)
        self.assertEqual(len(self.fake.requests), 1)
        self.clock.now += ns.FAILURE_PAUSE
        self.fake.routes[TECH.url] = FEEDS[TECH.url]
        collection = await self.service.collect(["ai_tech"])
        self.assertEqual(len(collection.articles), 2)
        self.assertEqual(collection.failed, ())

    async def test_one_failing_source_does_not_hide_others(self):
        self.fake.routes[GAMES.url] = b"<html>maintenance</html>"
        collection = await self.service.collect(["ai_tech", "games"])
        self.assertEqual(len(collection.articles), 2)
        self.assertEqual([(s.id, e.reason) for s, e in collection.failed], [("games", "bad_feed")])

    async def test_hanging_source_does_not_delay_the_answer(self):
        self.fake.gates[GAMES.url] = asyncio.Event()
        self.service.deadline = 0.05
        collection = await self.service.collect(["ai_tech", "games"])
        self.assertEqual({a.source_id for a in collection.articles}, {"tech"})
        self.assertEqual(collection.failed, ((GAMES, None),))
        # The download goes on in the background and fills the cache.
        self.fake.gates[GAMES.url].set()
        for _ in range(50):
            if GAMES.id not in self.service._inflight:
                break
            await asyncio.sleep(0.01)
        requests = len(self.fake.requests)
        collection = await self.service.collect(["ai_tech", "games"])
        self.assertEqual({a.source_id for a in collection.articles}, {"tech", "games"})
        self.assertEqual(len(self.fake.requests), requests)

    async def test_identical_requests_share_one_download(self):
        self.fake.gates[TECH.url] = asyncio.Event()
        first = asyncio.ensure_future(self.service.collect(["ai_tech"]))
        second = asyncio.ensure_future(self.service.search("процессор", ["ai_tech"]))
        await asyncio.sleep(0.01)
        self.fake.gates[TECH.url].set()
        await asyncio.gather(first, second)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(len(second.result().articles), 1)

    async def test_concurrent_downloads_are_limited(self):
        sources = tuple(Source(f"s{n}", f"S{n}", f"https://feeds.example.com/{n}.xml", "games",
                               frozenset({"feeds.example.com"})) for n in range(10))
        fake = FakeFeeds({source.url: rss(item(f"N{n}", f"https://example.com/{n}"))
                          for n, source in enumerate(sources)})

        async def slow(request):
            await asyncio.sleep(0.01)
            return await FakeFeeds.handle(fake, request)

        service = ns.NewsService(sources, transport=httpx.MockTransport(slow), clock=Clock(NOW),
                                 retry_delays=(0,), resolve=fake_resolve)
        self.addAsyncCleanup(service.aclose)
        collection = await service.collect(["games"])
        self.assertEqual(len(collection.articles), 10)
        self.assertLessEqual(fake.max_active, ns.MAX_CONCURRENT)
        self.assertGreater(fake.max_active, 1)

    async def test_aclose_stops_background_downloads(self):
        self.fake.gates[TECH.url] = asyncio.Event()
        self.service.deadline = 0.01
        await self.service.collect(["ai_tech"])
        future = self.service._inflight[TECH.id]
        await self.service.aclose()
        self.assertTrue(future.done())


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def collect_one(self, route, source=TECH, resolve=fake_resolve, **routes):
        self.fake = FakeFeeds({source.url: route, **routes})
        service = make_service(self.fake, sources=(source,), resolve=resolve)
        self.addAsyncCleanup(service.aclose)
        collection = await service.collect([source.category])
        error = collection.failed[0][1] if collection.failed else None
        return collection, error

    async def test_rate_limit_retry(self):
        collection, error = await self.collect_one([httpx.Response(429, headers={"Retry-After": "0"}), FEEDS[TECH.url]])
        self.assertIsNone(error)
        self.assertEqual(len(self.fake.requests), 2)
        collection, error = await self.collect_one(httpx.Response(429, headers={"Retry-After": "120"}))
        self.assertEqual(error.reason, "rate_limit")
        self.assertEqual(len(self.fake.requests), 1)  # a long Retry-After is respected, not waited for

    async def test_server_errors_have_limited_retries(self):
        collection, error = await self.collect_one(httpx.Response(503))
        self.assertEqual(error.reason, "server")
        self.assertEqual(len(self.fake.requests), 2)
        collection, error = await self.collect_one([httpx.Response(502), FEEDS[TECH.url]])
        self.assertEqual(len(collection.articles), 2)

    async def test_client_errors_are_not_retried(self):
        for status in (403, 404, 410):
            collection, error = await self.collect_one(httpx.Response(status))
            self.assertEqual(error.reason, "rejected")
            self.assertEqual(len(self.fake.requests), 1)

    async def test_transport_errors(self):
        for failure, reason in ((dns_error(), "dns"), (httpx.ConnectError("refused"), "network"),
                                (httpx.ConnectTimeout("slow"), "timeout"), (httpx.ReadTimeout("slow"), "timeout"),
                                (httpx.RemoteProtocolError("bye"), "network")):
            with self.subTest(reason=reason):
                collection, error = await self.collect_one(failure)
                self.assertEqual(error.reason, reason)
                self.assertEqual(len(self.fake.requests), 2)

    async def test_resolver_dns_error(self):
        source = Source("x", "X", "https://nxdomain.example.com/rss", "games", frozenset({"nxdomain.example.com"}))
        collection, error = await self.collect_one(FEEDS[TECH.url], source)
        self.assertEqual(error.reason, "dns")
        self.assertEqual(self.fake.requests, [])

    async def test_oversized_responses(self):
        with patch.object(ns, "MAX_FEED_BYTES", 1000):
            collection, error = await self.collect_one(
                httpx.Response(200, content=b"x" * 10, headers={"Content-Length": "5000"}))
            self.assertEqual(error.reason, "too_large")
            body = rss(*(item(f"N{n}", f"https://example.com/{n}") for n in range(50)))

            async def stream():
                for start in range(0, len(body), 100):
                    yield body[start:start + 100]

            collection, error = await self.collect_one(httpx.Response(200, content=stream()))
            self.assertEqual(error.reason, "too_large")

    async def test_redirects_are_checked(self):
        mirror = "https://mirror.example.org/rss.xml"
        collection, error = await self.collect_one(
            httpx.Response(301, headers={"Location": mirror}), GAMES, **{mirror: FEEDS[GAMES.url]})
        self.assertEqual(len(collection.articles), 2)
        self.assertEqual(self.fake.urls(), [GAMES.url, mirror])
        relative = "https://games.example.org/feed.xml"
        collection, error = await self.collect_one(
            httpx.Response(302, headers={"Location": "/feed.xml"}), GAMES, **{relative: FEEDS[GAMES.url]})
        self.assertEqual(len(collection.articles), 2)
        for location in ("https://evil.example.com/rss", "http://127.0.0.1:8080/admin", "http://169.254.169.254/",
                         "file:///etc/passwd", "https://internal.example.com/rss", "gopher://games.example.org/"):
            with self.subTest(location=location):
                collection, error = await self.collect_one(
                    httpx.Response(302, headers={"Location": location}), GAMES,
                    **{location: FEEDS[GAMES.url]})
                self.assertEqual(error.reason, "forbidden_url")
                self.assertEqual(self.fake.urls(), [GAMES.url])  # the target is never requested

    async def test_redirect_loop_is_limited(self):
        collection, error = await self.collect_one(httpx.Response(302, headers={"Location": TECH.url}))
        self.assertEqual(error.reason, "forbidden_url")
        self.assertEqual(len(self.fake.requests), ns.MAX_REDIRECTS + 1)

    async def test_private_addresses_are_refused(self):
        for addresses in (["10.0.0.5"], ["127.0.0.1"], ["::1"], ["93.184.216.34", "192.168.0.10"], []):
            async def resolve(host, port, addresses=addresses):
                return addresses

            with self.subTest(addresses=addresses):
                collection, error = await self.collect_one(FEEDS[TECH.url], resolve=resolve)
                self.assertEqual(error.reason, "forbidden_url")
                self.assertEqual(self.fake.requests, [])

        async def vpn_fake_ip(host, port):
            return ["198.18.0.42"]

        collection, error = await self.collect_one(FEEDS[TECH.url], resolve=vpn_fake_ip)
        self.assertEqual(len(collection.articles), 2)

    async def test_environment_proxy_skips_local_resolution_only(self):
        async def unreachable(host, port):
            raise AssertionError("resolved locally")

        with patch.dict("os.environ", {"https_proxy": "http://127.0.0.1:3128", "no_proxy": ""}):
            self.assertTrue(ns.proxied(TECH.url))
            collection, error = await self.collect_one(FEEDS[TECH.url], resolve=unreachable)
            self.assertEqual(len(collection.articles), 2)
            collection, error = await self.collect_one(
                httpx.Response(302, headers={"Location": "https://evil.example.com/"}), resolve=unreachable)
            self.assertEqual(error.reason, "forbidden_url")


if __name__ == "__main__":
    unittest.main()
