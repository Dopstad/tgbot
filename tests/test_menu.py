"""Проверка меню: нажимаем кнопки и пишем сообщения так же, как пользователь."""
from datetime import datetime

from app import core, db, nav
from tests.harness import BotTestCase

TZ = core.TZ


def T(day, hour, minute, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


class MenuTest(BotTestCase):
    async def create_once(self, text="Позвонить Ивану", day_label="Завтра", hour=15, minute=30):
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say(text)
        await self.press(day_label)
        await self.press(data=f"w:h:{hour}")
        await self.press(data=f"w:min:{minute}")
        await self.press("💾 Сохранить")

    # ---------------- навигация и чистота чата ----------------

    async def test_start_shows_menu_with_six_buttons(self):
        await self.say("/start")
        self.assertEqual(self.labels(), [
            "📋 Список задач", "➕ Новая задача", "🌅 Ежедневный дайджест",
            "🔁 Добавить ежедневную задачу", "📅 Повтор по дням недели",
            "📆 Добавить ежемесячную задачу",
        ])

    async def test_old_screens_are_deleted(self):
        await self.say("/menu")
        first = self.screen_id()
        await self.press("➕ Новая задача")
        self.assertIn(first, self.session.deleted)
        await self.say("Тест")  # набранный текст тоже удаляется, остаётся один живой экран
        await self.press("Завтра")
        self.assertEqual(len(self.live_screens()), 1)

    async def test_back_to_menu_cancels_wizard(self):
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Тест")
        await self.press("⬅️ Назад в меню")
        self.assertIn("Главное меню", self.screen_text())
        self.assertEqual(self.tasks(), [])

    async def test_stale_button_after_restart_opens_menu(self):
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Тест")
        core.dp.storage.storage.clear()  # «перезапуск бота»: мастер всё забыл
        await self.press("Завтра")
        self.assertIn("Главное меню", self.screen_text())

    # ---------------- создание ----------------

    async def test_create_one_time_task_with_advance(self):
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Позвонить Ивану")
        await self.press("Завтра")
        await self.press(data="w:h:15")
        await self.press(data="w:min:30")
        await self.press("30 минут")
        await self.press("1 день")
        await self.press("✅ 30 минут")  # снять галочку
        self.assertIn("✅ 1 день", self.labels())
        await self.press("💾 Сохранить")
        (task,) = self.tasks()
        self.assertEqual(task[1], "Позвонить Ивану")
        self.assertEqual(datetime.fromtimestamp(task[2], TZ), T(3, 15, 30))
        self.assertEqual(task[4], "1440")
        self.assertIn("Записал", self.screen_text())

    async def test_create_daily(self):
        await self.say("/menu")
        await self.press("🔁 Добавить ежедневную задачу")
        await self.say("Зарядка")
        await self.press(data="w:h:7")
        await self.press(data="w:min:0")
        await self.press("💾 Сохранить")
        (task,) = self.tasks()
        self.assertEqual((task[3], datetime.fromtimestamp(task[2], TZ)), ("daily", T(3, 7, 0)))

    async def test_create_weekly_with_presets_and_toggles(self):
        await self.say("/menu")
        await self.press("📅 Повтор по дням недели")
        await self.say("Тренировка")
        await self.press("Будни")
        await self.press("✅ Пт")        # снимаем пятницу
        await self.press("Сб")           # добавляем субботу
        await self.press("Дальше ➡️")
        await self.press(data="w:h:19")
        await self.press(data="w:min:0")
        await self.press("💾 Сохранить")
        (task,) = self.tasks()
        self.assertEqual(task[3], "wd:0,1,2,3,5")
        self.assertEqual(datetime.fromtimestamp(task[2], TZ), T(2, 19, 0))  # сегодня в 19:00

    async def test_weekly_requires_at_least_one_day(self):
        await self.say("/menu")
        await self.press("📅 Повтор по дням недели")
        await self.say("Тренировка")
        await self.press("Дальше ➡️")
        self.assertEqual(self.session.answers[-1], ("Отметь хотя бы один день", True))
        self.assertIn("В какие дни", self.screen_text())

    async def test_create_monthly_last_day(self):
        await self.say("/menu")
        await self.press("📆 Добавить ежемесячную задачу")
        await self.say("Оплатить аренду")
        await self.press("🔚 Последний день месяца")
        await self.press(data="w:h:10")
        await self.press(data="w:min:15")
        await self.press("2 дня")
        await self.press("💾 Сохранить")
        (task,) = self.tasks()
        self.assertEqual(task[3], "month:31")
        self.assertEqual(datetime.fromtimestamp(task[2], TZ), T(30, 10, 15))
        self.assertEqual(task[4], "2880")

    async def test_step_back(self):
        await self.say("/menu")
        await self.press("📆 Добавить ежемесячную задачу")
        await self.say("Взнос")
        await self.press(data="w:md:15")
        await self.press("↩️ Предыдущий шаг")
        self.assertIn("Какого числа", self.screen_text())

    async def test_past_time_is_rejected(self):
        self.now = T(2, 12, 30)
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Созвон")
        await self.press("Сегодня")
        self.assertNotIn("11", [l for l in self.labels()])  # прошедшие часы не предлагаем
        await self.press(data="w:h:12")
        await self.press(data="w:min:0")  # 12:00 уже прошло
        self.assertEqual(self.session.answers[-1], ("Это время уже прошло. Выбери другое.", True))
        self.assertEqual(self.tasks(), [])

    async def test_custom_date_and_time(self):
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Конференция")
        await self.press("✍️ Другая дата")
        await self.say("31.02")
        self.assertIn("не существует", self.screen_text())
        await self.say("20.11")
        await self.press("✍️ Ввести время вручную")
        await self.say("25:99")
        self.assertIn("Не понял время", self.screen_text())
        await self.say("10:45")
        await self.press("💾 Сохранить")
        (task,) = self.tasks()
        self.assertEqual(datetime.fromtimestamp(task[2], TZ), T(20, 10, 45))

    async def test_phrase_still_works(self):
        await self.say("Позвонить маме завтра в 9:30, напомни за 30 минут")
        (task,) = self.tasks()
        self.assertEqual((task[1], task[4]), ("Позвонить маме", "30"))
        self.assertIn("Записал", self.last_text())

    async def test_typed_phrase_inside_wizard_does_not_create_task(self):
        """Регрессия: фраза вместо кнопки на шаге мастера раньше создавала лишнюю задачу."""
        await self.say("/menu")
        await self.press("➕ Новая задача")
        await self.say("Тестовая задача")
        await self.say("Купить хлеб завтра в 18:00")  # вместо кнопки дня
        self.assertEqual(self.tasks(), [])
        self.assertIn("нужно нажать кнопку", self.screen_text())
        self.assertIn("На какой день", self.screen_text())
        await self.press("Завтра")  # мастер продолжает работать
        self.assertIn("Выбери час", self.screen_text())

    # ---------------- нумерация ----------------

    async def test_numbers_have_no_gaps(self):
        for i, name in enumerate(["Раз", "Два", "Три"]):
            db.add_task(7, name, T(3, 9 + i, 0), "", [])
        await self.say("/del 2")
        self.assertIn("Удалена задача: Два", self.last_text())
        db.add_task(7, "Четыре", T(3, 20, 0), "", [])
        await self.say("/list")
        text = self.screen_text()
        self.assertIn("№1 — 03.11.2026 09:00 — Раз", text)
        self.assertIn("№2 — 03.11.2026 11:00 — Три", text)
        self.assertIn("№3 — 03.11.2026 20:00 — Четыре", text)
        self.assertNotIn("№4", text)

    async def test_delete_by_phrase(self):
        db.add_task(7, "Раз", T(3, 9, 0), "", [])
        db.add_task(7, "Два", T(3, 10, 0), "", [])
        await self.say("удали 1")
        self.assertEqual([t[1] for t in self.tasks()], ["Два"])
        await self.say("удали 5")
        self.assertIn("нет", self.last_text())

    # ---------------- изменение ----------------

    async def open_card(self, position=1):
        await self.say("/list")
        await self.press(f"№{position}")

    async def test_edit_text(self):
        await self.create_once()
        await self.open_card()
        await self.press("✏️ Текст")
        await self.say("Позвонить Петру")
        self.assertEqual(self.tasks()[0][1], "Позвонить Петру")
        self.assertIn("✅ Текст изменён", self.screen_text())

    async def test_edit_time_of_daily_task(self):
        db.add_task(7, "Зарядка", T(3, 7, 0), "daily", [])
        await self.open_card()
        await self.press("🕐 Время")
        await self.press(data="w:h:8")
        await self.press(data="w:min:15")
        task = self.tasks()[0]
        self.assertEqual(datetime.fromtimestamp(task[2], TZ), T(3, 8, 15))
        self.assertEqual(task[3], "daily")

    async def test_edit_day_of_one_time_task(self):
        await self.create_once()  # завтра 15:30
        await self.open_card()
        await self.press("📅 День")
        await self.press("Пт 06.11")
        self.assertEqual(datetime.fromtimestamp(self.tasks()[0][2], TZ), T(6, 15, 30))

    async def test_edit_weekdays(self):
        db.add_task(7, "Тренировка", T(2, 19, 0), "wd:0,2,4", [])
        await self.open_card()
        await self.press("📅 Дни недели")
        await self.press("Вт")
        await self.press("✅ Пн")
        await self.press("💾 Готово")
        self.assertEqual(self.tasks()[0][3], "wd:1,2,4")

    async def test_edit_monthly_day(self):
        db.add_task(7, "Аренда", T(15, 10, 0), "month:15", [])
        await self.open_card()
        await self.press("📆 Число")
        await self.press(data="w:md:20")
        task = self.tasks()[0]
        self.assertEqual((task[3], datetime.fromtimestamp(task[2], TZ)), ("month:20", T(20, 10, 0)))

    async def test_edit_advance_replans(self):
        db.add_task(7, "Созвон", T(3, 15, 0), "", [60])
        await self.open_card()
        await self.press("🔔 Заранее")
        self.assertIn("✅ 1 час", self.labels())  # текущее значение подставлено
        await self.press("✅ 1 час")                # снять
        await self.press("30 минут")
        await self.press("💾 Сохранить")
        self.assertEqual(self.tasks()[0][4], "30")
        self.assertEqual([r[0] for r in db.run_db("SELECT minutes FROM advances", fetch=True)], [30])

    async def test_edit_one_time_to_past_is_rejected(self):
        self.now = T(2, 10, 30)
        db.add_task(7, "Позже", T(2, 18, 0), "", [])
        await self.open_card()
        await self.press("🕐 Время")
        await self.press(data="w:h:10")
        await self.press(data="w:min:0")  # 10:00 уже прошло
        self.assertEqual(datetime.fromtimestamp(self.tasks()[0][2], TZ), T(2, 18, 0))

    async def test_delete_with_confirmation(self):
        db.add_task(7, "Лишняя", T(3, 9, 0), "", [])
        await self.open_card()
        await self.press("🗑 Удалить")
        self.assertIn("Удалить задачу?", self.screen_text())
        await self.press("↩️ Нет")
        self.assertEqual(len(self.tasks()), 1)
        await self.press("🗑 Удалить")
        await self.press("🗑 Да, удалить")
        self.assertEqual(self.tasks(), [])
        self.assertIn("Активных задач нет", self.screen_text())

    async def test_card_of_deleted_task_returns_to_list(self):
        task_id, _, _ = db.add_task(7, "Исчезнет", T(3, 9, 0), "", [])
        await self.open_card()
        db.delete_task(7, task_id)  # удалили из другого места
        await self.press("✏️ Текст")
        self.assertEqual(self.session.answers[-1][0], "Эта задача уже удалена")
        self.assertIn("Список задач", self.screen_text())

    # ---------------- дайджест ----------------

    async def test_digest_screens(self):
        await self.say("/menu")
        await self.press("🌅 Ежедневный дайджест")
        self.assertIn("включён", self.screen_text())
        await self.press("🔕 Выключить")
        self.assertIn("выключен", self.screen_text())
        self.assertFalse(db.get_digest(7)[0])
        await self.press("🔔 Включить")
        await self.press("🕐 Изменить время")
        await self.press(data="dg:h:7")
        await self.press(data="dg:m:7:30")
        self.assertEqual(db.get_digest(7), (True, "07:30"))
        self.assertIn("07:30", self.screen_text())

    # ---------------- кнопки под напоминанием ----------------

    async def test_done_and_snooze_buttons(self):
        from app import scheduler
        db.add_task(7, "Позвонить", T(2, 10, 0), "", [])
        await scheduler.process_reminders(self.now)
        self.assertEqual(self.labels(), ["✅ Выполнено", "⏰ +10 минут", "⏰ +1 час"])
        first = self.screen_id()
        await self.press("⏰ +10 минут")
        self.assertIn("Отложено на 10 минут (до 10:10)", self.session.store[first]["text"])
        self.assertEqual(len(self.tasks()), 1)
        self.now = T(2, 10, 11)
        await scheduler.process_reminders(self.now)
        second = self.screen_id()
        await self.press("✅ Выполнено")
        self.assertIn("✅ Выполнено", self.session.store[second]["text"])
