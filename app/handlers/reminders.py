"""Кнопки под напоминанием: «Выполнено» и «Отложить»."""
from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app import core, db

router = Router()


@router.callback_query(F.data.startswith("done:"))
async def on_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    info = db.get_task_info(callback.message.chat.id, task_id)
    if info is None:
        await callback.answer("Эта задача уже удалена", show_alert=True)
        return
    # Меняем текст сообщения и убираем кнопки, чтобы нельзя было нажать повторно
    await callback.message.edit_text(f"✅ Выполнено:\n{info[0]}")
    await callback.answer()


@router.callback_query(F.data.startswith("snooze:"))
async def on_snooze(callback: CallbackQuery):
    _, task_id, minutes = callback.data.split(":")
    task_id, minutes = int(task_id), int(minutes)
    chat_id = callback.message.chat.id
    info = db.get_task_info(chat_id, task_id)
    if info is None:
        await callback.answer("Эта задача уже удалена", show_alert=True)
        return
    db.snooze_task(chat_id, task_id, minutes)
    new_time = core.now_local() + timedelta(minutes=minutes)
    label = "10 минут" if minutes == 10 else "1 час"
    await callback.message.edit_text(
        f"⏰ Отложено на {label} (до {new_time.strftime('%H:%M')}):\n{info[0]}"
    )
    await callback.answer()
