from datetime import date, datetime, time, timedelta, timezone
import time as clock
import unittest

from reminder_schedule import (
    Schedule, describe, find_timezone, local_status, next_run, parse_date, parse_time, to_utc,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def rule(repeat, day, moment="09:00", zone="Europe/Moscow", days=(), fold=0):
    return Schedule(zone, date.fromisoformat(day), time.fromisoformat(moment), fold, repeat, frozenset(days))


class ParsingTests(unittest.TestCase):
    today = date(2026, 10, 15)

    def test_dates(self):
        cases = {
            "сегодня": date(2026, 10, 15), "Завтра": date(2026, 10, 16), "послезавтра": date(2026, 10, 17),
            "31.12.2026": date(2026, 12, 31), "1/2/2027": date(2027, 2, 1), "2026-11-05": date(2026, 11, 5),
            "20.10": date(2026, 10, 20), "01.01": date(2027, 1, 1), "29.02": date(2028, 2, 29),
            "15.10": date(2026, 10, 15),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_date(text, self.today), expected)

    def test_bad_dates(self):
        for text in ("", "32.01.2026", "29.02.2027", "31.04", "2026-13-01", "завтра утром", "1.1.26"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_date(text, self.today)

    def test_times(self):
        self.assertEqual(parse_time("9:05"), time(9, 5))
        self.assertEqual(parse_time(" 23.59 "), time(23, 59))
        self.assertEqual(parse_time("00 00"), time(0, 0))
        for text in ("24:00", "12:60", "1230", "12:5", "", "полдень"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_time(text)

    def test_timezone_names(self):
        self.assertEqual(find_timezone("europe/moscow"), "Europe/Moscow")
        self.assertEqual(find_timezone(" UTC "), "UTC")
        for name in ("Moscow", "MSK+3", "../../etc/passwd", "", "Europe/Atlantis"):
            self.assertIsNone(find_timezone(name))


class DstTests(unittest.TestCase):
    def test_local_status(self):
        self.assertEqual(local_status(date(2027, 3, 28), time(2, 30), "Europe/Berlin"), "nonexistent")
        self.assertEqual(local_status(date(2026, 10, 25), time(2, 30), "Europe/Berlin"), "ambiguous")
        self.assertEqual(local_status(date(2026, 10, 25), time(2, 30), "Europe/Moscow"), "ok")
        self.assertEqual(local_status(date(2026, 10, 25), time(4, 0), "Europe/Berlin"), "ok")

    def test_fold_selects_offset(self):
        self.assertEqual(to_utc(date(2026, 10, 25), time(2, 30), "Europe/Berlin", 0), utc(2026, 10, 25, 0, 30))
        self.assertEqual(to_utc(date(2026, 10, 25), time(2, 30), "Europe/Berlin", 1), utc(2026, 10, 25, 1, 30))

    def test_daily_keeps_local_time_across_dst(self):
        schedule = rule("daily", "2026-10-20", "09:00", "Europe/Warsaw")
        self.assertEqual(next_run(schedule, utc(2026, 10, 24, 12)), utc(2026, 10, 25, 8))  # CET, UTC+1
        self.assertEqual(next_run(schedule, utc(2026, 10, 23, 12)), utc(2026, 10, 24, 7))  # CEST, UTC+2

    def test_daily_on_skipped_hour_fires_after_the_jump(self):
        schedule = rule("daily", "2027-03-20", "02:30", "Europe/Berlin")
        moment = next_run(schedule, utc(2027, 3, 27, 12))
        self.assertEqual(moment, utc(2027, 3, 28, 1, 30))  # 03:30 CEST
        self.assertEqual(next_run(schedule, moment), utc(2027, 3, 29, 0, 30))

    def test_hourly_counts_real_hours_across_dst(self):
        schedule = rule("hourly", "2026-10-25", "00:15", "Europe/Berlin")
        first = next_run(schedule, utc(2026, 10, 24))
        moments = [first]
        for _ in range(4):
            moments.append(next_run(schedule, moments[-1]))
        self.assertEqual([b - a for a, b in zip(moments, moments[1:])], [timedelta(hours=1)] * 4)


class RepeatTests(unittest.TestCase):
    def test_once(self):
        schedule = rule("once", "2026-10-20", "10:00")
        self.assertEqual(next_run(schedule, utc(2026, 10, 15)), utc(2026, 10, 20, 7))
        self.assertIsNone(next_run(schedule, utc(2026, 10, 20, 7)))

    def test_daily_never_before_anchor(self):
        schedule = rule("daily", "2026-10-20", "10:00")
        self.assertEqual(next_run(schedule, utc(2026, 10, 1)), utc(2026, 10, 20, 7))
        self.assertEqual(next_run(schedule, utc(2026, 10, 20, 7)), utc(2026, 10, 21, 7))

    def test_weekly_keeps_weekday(self):
        schedule = rule("weekly", "2026-10-19", "10:00", days={0})  # Monday
        moment = next_run(schedule, utc(2026, 10, 20))
        self.assertEqual(moment, utc(2026, 10, 26, 7))
        self.assertEqual(moment.weekday(), 0)
        self.assertEqual(next_run(schedule, moment), utc(2026, 11, 2, 7))

    def test_selected_weekdays(self):
        schedule = rule("weekdays", "2026-10-15", "10:00", days={0, 2, 4})  # Mon, Wed, Fri
        moments, after = [], utc(2026, 10, 15)
        for _ in range(4):
            after = next_run(schedule, after)
            moments.append(after)
        self.assertEqual([m.day for m in moments], [16, 19, 21, 23])

    def test_monthly_uses_last_day_of_short_month(self):
        schedule = rule("monthly", "2027-01-31", "10:00")
        moments, after = [], utc(2027, 1, 1)
        for _ in range(4):
            after = next_run(schedule, after)
            moments.append(after.date())
        self.assertEqual(moments, [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)])

    def test_monthly_leap_year(self):
        schedule = rule("monthly", "2028-01-30", "10:00")
        self.assertEqual(next_run(schedule, utc(2028, 2, 1)).date(), date(2028, 2, 29))
        schedule = rule("monthly", "2028-02-29", "10:00")
        self.assertEqual(next_run(schedule, utc(2029, 2, 1)).date(), date(2029, 2, 28))
        self.assertEqual(next_run(schedule, utc(2029, 3, 1)).date(), date(2029, 3, 29))

    def test_hourly_aligned_to_original_minute(self):
        schedule = rule("hourly", "2026-10-15", "10:15", "UTC")
        self.assertEqual(next_run(schedule, utc(2026, 10, 15, 13, 20)), utc(2026, 10, 15, 14, 15))
        self.assertEqual(next_run(schedule, utc(2026, 10, 15, 14, 15)), utc(2026, 10, 15, 15, 15))

    def test_long_downtime_skips_to_nearest_future(self):
        after = utc(2031, 6, 10, 12)
        for schedule in (rule("daily", "2020-01-01"), rule("weekly", "2020-01-06", days={0}),
                         rule("weekdays", "2020-01-01", days={5, 6}), rule("monthly", "2020-01-31"),
                         rule("hourly", "2020-01-01")):
            with self.subTest(repeat=schedule.repeat):
                started = clock.perf_counter()
                moment = next_run(schedule, after)
                self.assertLess(clock.perf_counter() - started, 0.05)
                self.assertGreater(moment, after)
                self.assertLess(moment - after, timedelta(days=32))

    def test_descriptions(self):
        self.assertIn("последний день", describe(rule("monthly", "2027-01-31")))
        self.assertIn("пн, ср", describe(rule("weekdays", "2027-01-01", days={0, 2})))
        self.assertEqual(describe(rule("once", "2027-01-01")), "без повторения")
