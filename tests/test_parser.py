"""Проверка разбора фраз: «завтра в 15:00», «каждый понедельник», «за 30 минут»."""
import os
import unittest
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

from app import core, parser  # noqa: E402

TZ = core.TZ


class ParserTest(unittest.TestCase):
    def setUp(self):
        self._orig = core.now_local
        # Понедельник, 2 ноября 2026, 10:00
        core.now_local = lambda: datetime(2026, 11, 2, 10, 0, tzinfo=TZ)

    def tearDown(self):
        core.now_local = self._orig

    def parse(self, text):
        result = parser.parse_task(text)
        self.assertIsNotNone(result, text)
        return result

    def check(self, text, task, when, repeat="", advance=()):
        r = self.parse(text)
        self.assertEqual(r["task"], task, text)
        self.assertEqual(r["when"].strftime("%d.%m.%Y %H:%M") if r["when"] else None, when, text)
        self.assertEqual(r["repeat"], repeat, text)
        self.assertEqual(r["advance"], list(advance), text)

    def test_day_words(self):
        self.check('Напиши мне "Доброе утро" завтра в 11:00', '"Доброе утро"', "03.11.2026 11:00")
        self.check("Позвонить Ивану послезавтра в 9:30", "Позвонить Ивану", "04.11.2026 09:30")
        self.check("Купить воду сегодня в 23:59", "Купить воду", "02.11.2026 23:59")

    def test_weekdays(self):
        self.check("Тренировка в пятницу в 19:00", "Тренировка", "06.11.2026 19:00")
        self.check("Пятница на следующей неделе в 18:00 созвон", "созвон", "13.11.2026 18:00")
        self.check("Планёрка в понедельник на следующей неделе в 9:00", "Планёрка", "09.11.2026 09:00")
        self.check("Врач во вторник в 7 вечера", "Врач", "03.11.2026 19:00")
        self.check("Зарядка в субботу в 8 утра", "Зарядка", "07.11.2026 08:00")

    def test_no_time_asks(self):
        r = self.parse("Тренировка завтра")
        self.assertIsNone(r["when"])

    def test_advance(self):
        self.check("Позвонить завтра в 15:00, напомни за 30 минут", "Позвонить", "03.11.2026 15:00", advance=[30])
        self.check("Созвон в пятницу в 18:00 заранее за 1 час и за 10 минут", "Созвон",
                   "06.11.2026 18:00", advance=[60, 10])
        self.check("Конференция в субботу в 10:00 за 1 неделю и за 2 дня", "Конференция",
                   "07.11.2026 10:00", advance=[10080, 2880])
        self.check("Позвонить маме завтра в 9:00 за полчаса", "Позвонить маме", "03.11.2026 09:00", advance=[30])

    def test_zaranee_without_amount(self):
        self.assertTrue(self.parse("Напомни заранее встреча завтра в 15:00")["advance_missing"])

    def test_repeats(self):
        self.check("Зарядка каждый день в 7:00", "Зарядка", "03.11.2026 07:00", "daily")
        self.check("Планёрка каждый понедельник в 9:00", "Планёрка", "09.11.2026 09:00", "wd:0")
        self.check("Тренировка по понедельникам, средам и пятницам в 19:00", "Тренировка",
                   "02.11.2026 19:00", "wd:0,2,4")
        self.check("Отчёт по будням в 17:30", "Отчёт", "02.11.2026 17:30", "wd:0,1,2,3,4")
        self.check("Йога по выходным в 10:00", "Йога", "07.11.2026 10:00", "wd:5,6")

    def test_monthly(self):
        self.check("Оплатить аренду каждое 15 число в 10:00 за 1 день", "Оплатить аренду",
                   "15.11.2026 10:00", "month:15", [1440])
        self.check("Отчёт каждый месяц 31 числа в 17:00", "Отчёт", "30.11.2026 17:00", "month:31")

    def test_nothing_found(self):
        self.assertIsNone(parser.parse_task("Привет"))


if __name__ == "__main__":
    unittest.main()
