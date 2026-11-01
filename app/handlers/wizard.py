"""
Мастера: пошаговое создание задач (кнопки w:...) и изменение существующих.
Состояние мастера (на каком вы шаге, что выбрано) хранится в памяти бота.
Если бот перезапустится, мастер забудет шаг, и бот просто откроет меню.
"""
import re
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import core, db, ui
from app.nav import delete_quietly, open_main_menu, show_screen
from app.ui import btn, markup
from app.wizard_ui import (
    STEPS, WEEKDAY_PRESETS, edit_data, new_data, wizard_screen, wizard_when,
)

router = Router()


class Wizard(StatesGroup):
    text = State()         # ждём текст задачи
    custom_date = State()  # ждём дату, написанную руками
    custom_time = State()  # ждём время, написанное руками
    choosing = State()     # выбор кнопками


# ---------------------------- запуск и показ шагов ----------------------------

async def show_wizard(chat_id, state, old_message=None, note=""):
    """Показывает текущий шаг мастера и выставляет нужное состояние."""
    data = await state.get_data()
    text, keyboard = wizard_screen(data, note)
    step = STEPS[data["mode"]][data["i"]]
    if data.get("custom") == "date":
        await state.set_state(Wizard.custom_date)
    elif data.get("custom") == "time":
        await state.set_state(Wizard.custom_time)
    elif step == "text":
        await state.set_state(Wizard.text)
    else:
        await state.set_state(Wizard.choosing)
    await show_screen(chat_id, text, keyboard, old_message)


async def start_wizard(chat_id, state, mode, old_message=None):
    """Создание новой задачи: once, daily, weekly или monthly."""
    await state.clear()
    await state.update_data(**new_data(mode))
    await show_wizard(chat_id, state, old_message)


async def start_edit(chat_id, state, task, field, old_message=None):
    """Изменение существующей задачи: text, time, when или adv."""
    await state.clear()
    await state.update_data(**edit_data(task, field))
    await show_wizard(chat_id, state, old_message)


async def proceed(chat_id, state, old_message=None):
    """Следующий шаг или, если шаги кончились, сохранение."""
    data = await state.get_data()
    steps = STEPS[data["mode"]]
    if data["i"] < len(steps):
        await show_wizard(chat_id, state, old_message)
        return
    note = await complete(chat_id, state, data, old_message)
    if note:
        # Не получилось сохранить (например, время в прошлом): остаёмся на последнем шаге
        await state.update_data(i=len(steps) - 1, custom=None)
        await show_wizard(chat_id, state, old_message, note=f"⚠️ {note}")


# ---------------------------- сохранение ----------------------------

PAST_NOTE = "Это время уже прошло. Выбери другое."


async def complete(chat_id, state, data, old_message):
    """Сохраняет результат мастера. Возвращает текст ошибки или None, если всё получилось."""
    if data["mode"].startswith("edit_"):
        return await complete_edit(chat_id, state, data, old_message)
    return await complete_create(chat_id, state, data, old_message)


async def complete_create(chat_id, state, data, old_message):
    now = core.now_local()
    when, rule = wizard_when(data, now)
    if when is None or when <= now:
        return PAST_NOTE
    _, made, skipped = db.add_task(chat_id, data["text"], when, rule, data.get("advance", []))
    await state.clear()
    keyboard = markup([[btn("➕ Ещё задачу", "m:new"), btn("📱 Меню", "m:main")]])
    text = ui.confirmation_text(data["text"], when, rule, made, skipped)
    await show_screen(chat_id, text, keyboard, old_message)
    return None


async def complete_edit(chat_id, state, data, old_message):
    task_id, mode = data["task_id"], data["mode"]
    if db.get_task(chat_id, task_id) is None:
        await state.clear()
        text, keyboard = ui.list_screen(chat_id)
        await show_screen(chat_id, text, keyboard, old_message)
        return None

    if mode == "edit_text":
        db.update_task_text(chat_id, task_id, data["text"])
        note = "✅ Текст изменён"
    elif mode == "edit_advance":
        _, skipped = db.update_task_advance(chat_id, task_id, data.get("advance", []))
        note = "✅ Предупреждения изменены"
        if skipped:
            note += "\n(часть не поставлена: это время уже прошло)"
    else:
        when, rule = wizard_when(data, core.now_local())
        if when is None or when <= core.now_local():
            return PAST_NOTE
        db.update_task_schedule(chat_id, task_id, when, rule)
        note = "✅ Изменено"

    await state.clear()
    text, keyboard = ui.task_card_screen(chat_id, task_id, note)
    await show_screen(chat_id, text, keyboard, old_message)
    return None


# ---------------------------- ввод текста руками ----------------------------

async def stale_wizard(chat_id, state):
    """Мастер потерял данные (бот перезапускался): открываем меню."""
    await state.clear()
    await open_main_menu(chat_id)


@router.message(Wizard.text, F.text)
async def wizard_text(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)  # убираем набранное, чтобы не засорять чат
    data = await state.get_data()
    if "mode" not in data:
        await stale_wizard(chat_id, state)
        return
    text = message.text.strip()
    if not text:
        return
    await state.update_data(text=text[:200], i=data["i"] + 1)
    await proceed(chat_id, state)


@router.message(Wizard.custom_date, F.text)
async def wizard_custom_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)
    data = await state.get_data()
    if "mode" not in data:
        await stale_wizard(chat_id, state)
        return

    m = re.fullmatch(r"\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*", message.text)
    today = core.now_local().date()
    note = "⚠️ Не понял дату. Пример: 15.11 или 15.11.2026"
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            chosen = date(year, month, day)
            if not m.group(3) and chosen < today:
                chosen = date(year + 1, month, day)
            if chosen < today:
                note = "⚠️ Эта дата уже прошла. Напиши другую."
            else:
                await state.update_data(day=chosen.isoformat(), i=data["i"] + 1, custom=None)
                await proceed(chat_id, state)
                return
        except ValueError:
            note = "⚠️ Такой даты не существует. Пример: 15.11"
    await show_wizard(chat_id, state, note=note)


@router.message(Wizard.custom_time, F.text)
async def wizard_custom_time(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)
    data = await state.get_data()
    if "mode" not in data:
        await stale_wizard(chat_id, state)
        return

    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", message.text)
    note = "⚠️ Не понял время. Пример: 15:40"
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        hour, minute = int(m.group(1)), int(m.group(2))
        probe = dict(data, hour=hour, minute=minute)
        if data["kind"] == "once" and wizard_when(probe, core.now_local())[0] <= core.now_local():
            note = "⚠️ Это время уже прошло. Напиши другое."
        else:
            steps = STEPS[data["mode"]]
            # Время введено целиком: перескакиваем к «заранее» или, если его нет, к сохранению
            target = steps.index("advance") if "advance" in steps else len(steps)
            await state.update_data(hour=hour, minute=minute, i=target, custom=None)
            await proceed(chat_id, state)
            return
    await show_wizard(chat_id, state, note=note)


# ---------------------------- кнопки мастера ----------------------------

@router.callback_query(F.data.startswith("w:"))
async def on_wizard_button(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    data = await state.get_data()
    if "mode" not in data:
        # Бот перезапускался, и мастер забыл, на каком шаге вы были
        await callback.answer("Эта кнопка устарела, открываю меню")
        await stale_wizard(chat_id, state)
        return

    parts = callback.data.split(":")
    action = parts[1]
    i, kind = data["i"], data["kind"]
    steps = STEPS[data["mode"]]

    if action == "prev":
        await state.update_data(i=max(0, i - 1), custom=None)
    elif action == "cur":
        await state.update_data(custom=None)
    elif action == "day":
        await state.update_data(day=parts[2], i=i + 1)
    elif action == "daycustom":
        await state.update_data(custom="date")
    elif action == "timecustom":
        await state.update_data(custom="time")
    elif action == "h":
        await state.update_data(hour=int(parts[2]), minute=None, i=i + 1)
    elif action == "md":
        await state.update_data(mday=int(parts[2]), i=i + 1)
    elif action == "min":
        minute = int(parts[2])
        if kind == "once" and wizard_when(dict(data, minute=minute), core.now_local())[0] <= core.now_local():
            await callback.answer(PAST_NOTE, show_alert=True)
            return
        await state.update_data(minute=minute, i=i + 1)
    elif action in ("wd", "wdp"):
        # Дни недели: отмечаем «на месте», без нового сообщения
        chosen = set(data.get("wdays", []))
        if action == "wd":
            chosen ^= {int(parts[2])}
        else:
            chosen = set(WEEKDAY_PRESETS[parts[2]])
        await state.update_data(wdays=sorted(chosen))
        _, keyboard = wizard_screen(await state.get_data())
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    elif action == "wnext":
        if not data.get("wdays"):
            await callback.answer("Отметь хотя бы один день", show_alert=True)
            return
        await state.update_data(i=i + 1)
    elif action == "adv":
        # «Заранее»: галочки меняем «на месте»
        chosen = set(data.get("advance", []))
        chosen ^= {int(parts[2])}
        await state.update_data(advance=sorted(chosen, reverse=True))
        _, keyboard = wizard_screen(await state.get_data())
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    elif action == "save":
        await state.update_data(i=len(steps))

    await callback.answer()
    await proceed(chat_id, state, callback.message)
