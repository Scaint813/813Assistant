from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_keyboard(preview_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm_preview:{preview_id}")],
            [InlineKeyboardButton(text="Изменить", callback_data=f"edit_preview:{preview_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"cancel_preview:{preview_id}")],
        ]
    )


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Готово", callback_data=f"reminder_done:{reminder_id}")],
            [InlineKeyboardButton(text="Перенести", callback_data=f"reminder_snooze:{reminder_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"reminder_cancel:{reminder_id}")],
        ]
    )


def reminder_snooze_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Через 1 час", callback_data=f"snooze_1h:{reminder_id}")],
            [InlineKeyboardButton(text="Сегодня вечером", callback_data=f"snooze_evening:{reminder_id}")],
            [InlineKeyboardButton(text="Завтра утром", callback_data=f"snooze_tomorrow_morning:{reminder_id}")],
        ]
    )
