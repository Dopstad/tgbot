"""Проверка «часов»: предупреждения заранее, повторы, откладывание, сводка, старая база."""
import sqlite3
from datetime import datetime

from app import core, db, scheduler
from tests.harness import BotTestCase

TZ = core.TZ


def T(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class SchedulerTest(BotTestCase):
    async def tick(self, now):
        """Перематываем часы и делаем один проход бота."""
        self.now = now
        await scheduler.process_reminders(now)
        await scheduler.send_digests(now)

    def texts(self):
        """Тексты всех отправленных сообщений (и очищаем журнал)."""
        result = [self.session.store[m]["text"] for m in self.session.sent]
        self.session.sent.clear()
        return result

    async def test_advance_reminders_fire_once_in_order(self):
        _, made, skipped = db.add_task(7, "Созвон", T(3, 15, 0), "", [2880, 120, 30])
        self.assertEqual(made, [120, 30])
        self.assertEqual(skipped, [2880])  # «за 2 дня» уже прошло
        await self.tick(T(3, 12, 59)); self.assertEqual(self.texts(), [])
        await self.tick(T(3, 13, 0))
        self.assertIn("за 2 часа", self.texts()[0])
        await self.tick(T(3, 13, 1)); self.assertEqual(self.texts(), [])  # повторно не придёт
        await self.tick(T(3, 14, 30)); self.assertIn("за 30 минут", self.texts()[0])
        await self.tick(T(3, 15, 0))
        main = self.texts()
        self.assertEqual(len(main), 1)
        self.assertIn("⏰ Напоминание", main[0])
        await self.tick(T(3, 15, 1)); self.assertEqual(self.texts(), [])

    async def test_no_stale_advance_after_downtime(self):
        db.add_task(7, "Событие", T(3, 15, 0), "", [30])
        await self.tick(T(3, 16, 0))  # бот «проспал» и событие, и предупреждение
        texts = self.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("⏰ Напоминание", texts[0])

    async def test_weekly_repeat_moves_to_next_and_replans_advance(self):
        tid, _, _ = db.add_task(7, "Планёрка", T(4, 9, 0), "wd:2,4", [30])  # ср и пт
        await self.tick(T(4, 8, 30)); self.assertIn("за 30 минут", self.texts()[0])
        await self.tick(T(4, 9, 0))
        self.assertIn("🔁 Следующее: пт 06.11 в 09:00", self.texts()[0])
        row = db.get_task(7, tid)
        self.assertEqual(datetime.fromtimestamp(row[2], TZ), T(6, 9, 0))
        await self.tick(T(6, 8, 30)); self.assertIn("за 30 минут", self.texts()[0])  # снова предупредил

    async def test_monthly_last_day_clamps(self):
        tid, _, _ = db.add_task(7, "Отчёт", T(30, 18, 0), "month:31", [])
        await self.tick(T(30, 18, 0))
        row = db.get_task(7, tid)
        self.assertEqual(datetime.fromtimestamp(row[2], TZ), datetime(2026, 12, 31, 18, 0, tzinfo=TZ))

    async def test_downtime_does_not_spam_repeats(self):
        db.add_task(7, "Планёрка", T(4, 9, 0), "wd:2", [])
        await self.tick(T(20, 12, 0))
        self.assertEqual(len(self.texts()), 1)  # одно напоминание, а не пачка за все пропущенные среды

    async def test_snooze_one_time_and_recurring(self):
        once, _, _ = db.add_task(7, "Разовая", T(2, 10, 5), "", [])
        series, _, _ = db.add_task(7, "Серия", T(2, 10, 5), "daily", [])
        await self.tick(T(2, 10, 5)); self.texts()
        db.snooze_task(7, once, 10)
        db.snooze_task(7, series, 10)
        # у серии расписание не тронуто (завтра), плюс появилась разовая копия
        self.assertEqual(datetime.fromtimestamp(db.get_task(7, series)[2], TZ), T(3, 10, 5))
        self.assertEqual(len(db.get_tasks(7)), 3)
        await self.tick(T(2, 10, 16))
        self.assertEqual(sorted(self.texts()), ["⏰ Напоминание:\nРазовая", "⏰ Напоминание:\nСерия"])

    async def test_delete_removes_advances(self):
        tid, _, _ = db.add_task(7, "Удалить", T(5, 10, 0), "", [60, 30])
        self.assertEqual(len(db.run_db("SELECT * FROM advances", fetch=True)), 2)
        self.assertEqual(db.delete_task(7, tid), 1)
        self.assertEqual(db.delete_task(8, tid), 0)
        self.assertEqual(db.run_db("SELECT * FROM advances", fetch=True), [])

    async def test_digest(self):
        self.now = T(23, 5, 0)
        db.ensure_user(50)
        db.add_task(50, "Позвонить", T(23, 15, 0), "", [])
        db.add_task(50, "Завтра", T(24, 9, 0), "", [])
        await self.tick(T(23, 7, 59)); self.assertEqual(self.texts(), [])
        await self.tick(T(23, 8, 0))
        digest = self.texts()
        self.assertEqual(len(digest), 1)
        self.assertIn("Позвонить", digest[0]); self.assertNotIn("Завтра", digest[0])
        await self.tick(T(23, 8, 5)); self.assertEqual(self.texts(), [])  # второй раз не придёт

    async def test_late_user_gets_digest_only_from_next_morning(self):
        self.now = T(24, 14, 0)
        db.ensure_user(51)
        db.add_task(51, "Сегодня вечером", T(24, 18, 0), "", [])
        await self.tick(T(24, 14, 1)); self.assertEqual(self.texts(), [])

    async def test_old_database_is_migrated(self):
        path = core.DB_PATH + ".old"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                     "text TEXT NOT NULL, remind_at INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO tasks (chat_id, text, remind_at) VALUES (1, 'Старая', ?)",
                     (int(T(5, 12, 0).timestamp()),))
        conn.commit(); conn.close()
        core.DB_PATH = path
        db.init_db(); db.init_db()  # дважды: не должно падать
        self.assertEqual([t[1] for t in db.get_tasks(1)], ["Старая"])

    async def test_cleanup_old_sent_tasks(self):
        tid, _, _ = db.add_task(7, "Давно была", T(2, 9, 0), "", [])
        db.run_db("UPDATE tasks SET sent = 1 WHERE id = ?", (tid,))
        db.cleanup(T(2, 10, 0))
        self.assertEqual(len(db.run_db("SELECT * FROM tasks", fetch=True)), 1)  # ещё свежая
        db.cleanup(datetime(2026, 11, 20, 10, 0, tzinfo=TZ))
        self.assertEqual(db.run_db("SELECT * FROM tasks", fetch=True), [])
