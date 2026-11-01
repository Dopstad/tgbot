"""Задача обычной фразой: «Позвонить Ивану завтра в 15:00». Этот обработчик подключается последним."""
from aiogram import F, Router
from aiogram.types import Message

from app import core, db, ui
from app.parser import parse_task

router = Router()


@router.message(F.text)
async def handle_phrase(message: Message):
    db.ensure_user(message.chat.id)
    result = parse_task(message.text)

    if result is None:
        await message.answer(
            "Не нашёл в сообщении дату. Попробуй так:\n"
            "Позвонить Ивану завтра в 15:00\n\n"
            "Или открой меню: /menu. Все примеры: /help"
        )
        return

    if result["advance_missing"]:
        await message.answer(
            "Заранее — это за сколько? Добавь, например:\n"
            "за 30 минут, за 1 час, за 2 дня, за неделю"
        )
        return

    task, when = result["task"], result["when"]

    if when is None:
        await message.answer("Укажи время в формате 15:00, например: завтра в 15:00")
        return

    if when < core.now_local():
        await message.answer("Это время уже прошло. Укажи время в будущем.")
        return

    if not task:
        task = "Напоминание"

    _, made, skipped = db.add_task(
        message.chat.id, task, when, result["repeat"], result["advance"]
    )
    await message.answer(ui.confirmation_text(task, when, result["repeat"], made, skipped))
