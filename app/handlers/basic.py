"""Основные команды: /start, /menu, /help, /today и кнопка «📱 Меню» внизу экрана."""
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import core, db, ui
from app.nav import delete_quietly, open_main_menu

router = Router()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(message.chat.id)
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "Меню открывается кнопкой «📱 Меню» внизу экрана или командой /menu.\n"
        "Фразами тоже можно, примеры: /help",
        reply_markup=ui.reply_menu(),
    )
    await open_main_menu(message.chat.id)


@router.message(Command("menu"))
@router.message(F.text == ui.BTN_MENU)
async def show_menu(message: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(message.chat.id)
    await delete_quietly(message.chat.id, message.message_id)  # убираем и сообщение «Меню»
    await open_main_menu(message.chat.id)


@router.message(Command("help"))
@router.message(F.text == ui.BTN_HELP)
async def show_help(message: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(message.chat.id)
    await message.answer(ui.HELP_TEXT, reply_markup=ui.reply_menu())


@router.message(Command("today"))
@router.message(F.text == ui.BTN_TODAY)
async def show_today(message: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(message.chat.id)
    body = ui.render_today(message.chat.id, core.now_local())
    if body is None:
        await message.answer("На сегодня задач больше нет.")
    else:
        await message.answer(f"Задачи на сегодня:\n\n{body}")
