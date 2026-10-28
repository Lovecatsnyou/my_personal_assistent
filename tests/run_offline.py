"""Test runner for sandboxes that block asyncio's socket-based thread wakeups."""

import asyncio
from pathlib import Path
import unittest

from tests.test_bot import BotTests
from tests.test_news_bot import NewsBotTests
from tests.test_notes_bot import NoteBotTests
from tests.test_reminders_bot import ReminderBotTests
from tests.test_weather_bot import WeatherBotTests


def polling_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    def tick() -> None:
        # Keep real thread/SQLite calls; only replace the blocked wakeup mechanism.
        loop.call_later(0.01, tick)

    loop.call_soon(tick)
    return loop


def main() -> None:
    for case in (BotTests, NewsBotTests, NoteBotTests, ReminderBotTests, WeatherBotTests):
        case.loop_factory = staticmethod(polling_loop)
    project = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(
        str(project / "tests"), top_level_dir=str(project),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
