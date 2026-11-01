"""Утренняя сводка: команда /digest и кнопки экрана «Ежедневный дайджест» (dg:)."""
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import db, ui
from app.nav import show_screen
from app.ui import BACK_ROW, btn, grid, markup

router = Router()


@router.message(Command("digest"))
async def digest_command(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    db.ensure_user(chat_id)
    arg = (command.args or "").strip().lower()

    if arg in ("off", "выкл"):
        db.set_digest(chat_id, False)
        await message.answer("Утренняя сводка выключена. Включить: /digest on")
    elif arg in ("on", "вкл"):
        db.set_digest(chat_id, True)
        await message.answer("Утренняя сводка включена.")
    elif re.fullmatch(r"\d{1,2}:\d{2}", arg):
        hour, minute = (int(x) for x in arg.split(":"))
        if hour > 23 or minute > 59:
            await message.answer("Время указано неверно. Пример: /digest 7:30")
            return
        hhmm = f"{hour:02d}:{minute:02d}"
        db.set_digest(chat_id, True, hhmm)
        await message.answer(f"Буду присылать сводку каждое утро в {hhmm}.")
    else:
        text, keyboard = ui.digest_screen(chat_id)
        await show_screen(chat_id, text, keyboard)


@router.callback_query(F.data.startswith("dg:"))
async def on_digest_button(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    action = parts[1]
    chat_id = callback.message.chat.id
    db.ensure_user(chat_id)
    await state.clear()

    if action == "toggle":
        db.set_digest(chat_id, not db.get_digest(chat_id)[0])
        text, keyboard = ui.digest_screen(chat_id)
        try:
            # Переключатель меняем «на месте», без нового сообщения
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    elif action == "time":
        rows = grid([btn(f"{h:02d}", f"dg:h:{h}") for h in range(24)], 6)
        rows += [[btn("↩️ К дайджесту", "m:digest")], BACK_ROW]
        await show_screen(
            chat_id, "🌅 Ежедневный дайджест\n\n🕐 Во сколько присылать? Выбери час.",
            markup(rows), callback.message,
        )
    elif action == "h":
        hour = int(parts[2])
        rows = grid([btn(f"{hour:02d}:{m:02d}", f"dg:m:{hour}:{m}") for m in (0, 15, 30, 45)], 4)
        rows += [[btn("↩️ К дайджесту", "m:digest")], BACK_ROW]
        await show_screen(
            chat_id, "🌅 Ежедневный дайджест\n\n🕐 Выбери минуты.", markup(rows), callback.message
        )
    elif action == "m":
        hhmm = f"{int(parts[2]):02d}:{int(parts[3]):02d}"
        db.set_digest(chat_id, True, hhmm)
        text, keyboard = ui.digest_screen(chat_id, note=f"✅ Время сохранено: {hhmm}")
        await show_screen(chat_id, text, keyboard, callback.message)
    await callback.answer()
