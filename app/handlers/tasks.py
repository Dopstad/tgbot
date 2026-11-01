"""
Список задач, карточка задачи, удаление и запуск изменения.
Кнопки: t:ID (открыть карточку), te:ID:поле (изменить), td:ID (спросить об удалении),
tdc:ID (подтвердить удаление), del:ID (старые кнопки из прежних версий).
"""
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from app import db, ui
from app.handlers import wizard
from app.nav import show_screen

router = Router()


# ---------------------------- команды ----------------------------

@router.message(Command("list"))
@router.message(F.text == ui.BTN_LIST)
async def show_list(message: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(message.chat.id)
    text, keyboard = ui.list_screen(message.chat.id)
    await show_screen(message.chat.id, text, keyboard)


async def delete_by_number(message: Message, raw_number):
    """Удаление по номеру из списка (1, 2, 3...), общее для /del и фразы «удали 3»."""
    if not raw_number or not raw_number.strip().isdigit():
        await message.answer("Укажи номер из списка, например: /del 3\nНомера видно в /list")
        return
    task = db.task_by_position(message.chat.id, int(raw_number))
    if task is None:
        await message.answer("Задачи с таким номером нет. Посмотри номера в /list")
        return
    db.delete_task(message.chat.id, task[0])
    await message.answer(f"Удалена задача: {task[1]} 🗑")


@router.message(Command("del"))
async def delete_by_command(message: Message, command: CommandObject):
    await delete_by_number(message, command.args)


# «удали 3» / «удалить 3» / «удали задачу 3» (только вне мастеров, чтобы не мешать вводу текста)
@router.message(StateFilter(default_state), F.text.regexp(r"(?i)^\s*удал\w*\s+(задач\w*\s+)?(№\s*)?(\d+)\s*$"))
async def delete_by_text(message: Message):
    await delete_by_number(message, re.search(r"(\d+)\s*$", message.text).group(1))


# ---------------------------- кнопки ----------------------------

async def show_list_after(callback: CallbackQuery, note_answer=""):
    """Показывает список вместо карточки, которой уже нет."""
    text, keyboard = ui.list_screen(callback.message.chat.id)
    await show_screen(callback.message.chat.id, text, keyboard, callback.message)
    await callback.answer(note_answer)


@router.callback_query(F.data.startswith("t:"))
async def on_task_card(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = callback.message.chat.id
    screen = ui.task_card_screen(chat_id, int(callback.data[2:]))
    if screen is None:
        await show_list_after(callback, "Эта задача уже удалена")
        return
    await show_screen(chat_id, *screen, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("te:"))
async def on_task_edit(callback: CallbackQuery, state: FSMContext):
    _, task_id, field = callback.data.split(":")
    chat_id = callback.message.chat.id
    task = db.get_task(chat_id, int(task_id))
    if task is None:
        await state.clear()
        await show_list_after(callback, "Эта задача уже удалена")
        return
    await wizard.start_edit(chat_id, state, task, field, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("td:"))
async def on_task_delete_ask(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = callback.message.chat.id
    screen = ui.delete_confirm_screen(chat_id, int(callback.data[3:]))
    if screen is None:
        await show_list_after(callback, "Эта задача уже удалена")
        return
    await show_screen(chat_id, *screen, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("tdc:"))
async def on_task_delete_confirm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    db.delete_task(callback.message.chat.id, int(callback.data[4:]))
    await show_list_after(callback, "Удалено 🗑")


# Кнопка «🗑 №3» из списков прежних версий бота: ещё может висеть в старых сообщениях
@router.callback_query(F.data.startswith("del:"))
async def on_legacy_delete(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    db.delete_task(chat_id, int(callback.data.split(":")[1]))
    text, keyboard = ui.list_screen(chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass
    await callback.answer("Удалено 🗑")
