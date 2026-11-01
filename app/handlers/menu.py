"""Кнопки главного меню (в callback_data они начинаются с «m:»)."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app import db, ui
from app.handlers import wizard
from app.nav import open_main_menu, show_screen

router = Router()

# какая кнопка меню запускает какой мастер
WIZARD_BUTTONS = {"new": "once", "daily": "daily", "weekly": "weekly", "monthly": "monthly"}


@router.callback_query(F.data.startswith("m:"))
async def on_menu_button(callback: CallbackQuery, state: FSMContext):
    action = callback.data[2:]
    chat_id = callback.message.chat.id
    db.ensure_user(chat_id)

    if action in WIZARD_BUTTONS:
        await wizard.start_wizard(chat_id, state, WIZARD_BUTTONS[action], callback.message)
    else:
        await state.clear()
        if action == "list":
            text, keyboard = ui.list_screen(chat_id)
            await show_screen(chat_id, text, keyboard, callback.message)
        elif action == "digest":
            text, keyboard = ui.digest_screen(chat_id)
            await show_screen(chat_id, text, keyboard, callback.message)
        else:
            await open_main_menu(chat_id, callback.message)
    await callback.answer()
