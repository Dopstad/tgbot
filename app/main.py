"""Сборка бота: подключаем все файлы с обработчиками и запускаем."""
import asyncio

from app import core, db, scheduler
from app.handlers import basic, digest, menu, quick, reminders, tasks, wizard


def setup():
    """
    Порядок важен: Telegram отдаёт сообщение первому подходящему обработчику.
    Поэтому команды и кнопки идут раньше, мастера в середине, а обычные фразы последними.
    """
    core.dp.include_routers(
        basic.router, digest.router, tasks.router, wizard.router,
        menu.router, reminders.router, quick.router,
    )


async def main():
    db.init_db()  # создаём базу, если её ещё нет
    setup()
    # Цикл напоминаний работает в фоне, параллельно с приёмом сообщений
    reminder_task = asyncio.create_task(scheduler.reminder_loop())
    try:
        await core.dp.start_polling(core.bot)
    finally:
        reminder_task.cancel()
