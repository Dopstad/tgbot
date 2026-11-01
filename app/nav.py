"""
Показ экранов. Главное правило: чат не должен захламляться.
Когда показываем новый экран, старый удаляется.
"""
from app import core, ui

# Последний «экран» меню в каждом чате (номер сообщения).
LAST_SCREEN = {}


async def delete_quietly(chat_id, message_id):
    """Удаляет сообщение. Если не вышло (например, оно старше 48 часов), молча идёт дальше."""
    try:
        await core.bot.delete_message(chat_id, message_id)
        return True
    except Exception:
        return False


async def show_screen(chat_id, text, keyboard, old_message=None):
    """
    Показывает новый экран и удаляет старый: то сообщение, с кнопки которого
    пришло нажатие, и последний экран меню в этом чате.
    """
    old_ids = []
    if old_message is not None:
        old_ids.append(old_message.message_id)
    last = LAST_SCREEN.get(chat_id)
    if last and last not in old_ids:
        old_ids.append(last)

    sent = await core.bot.send_message(chat_id, text, reply_markup=keyboard)
    LAST_SCREEN[chat_id] = sent.message_id

    for message_id in old_ids:
        if not await delete_quietly(chat_id, message_id):
            # Удалить не вышло: хотя бы убираем кнопки, чтобы они не вводили в заблуждение
            try:
                await core.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                pass


async def open_main_menu(chat_id, old_message=None):
    text, keyboard = ui.main_menu_screen()
    await show_screen(chat_id, text, keyboard, old_message)
