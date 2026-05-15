from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_keyboard(preview_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm_preview:{preview_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"cancel_preview:{preview_id}")],
        ]
    )


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Готово", callback_data=f"reminder_done:{reminder_id}")],
            [InlineKeyboardButton(text="Перенести", callback_data=f"reminder_snooze_menu:{reminder_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"reminder_cancel:{reminder_id}")],
        ]
    )


def reminder_snooze_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Через 1 час", callback_data=f"reminder_snooze_1h:{reminder_id}")],
            [InlineKeyboardButton(text="Сегодня вечером", callback_data=f"reminder_snooze_evening:{reminder_id}")],
            [InlineKeyboardButton(text="Завтра утром", callback_data=f"reminder_snooze_tomorrow_morning:{reminder_id}")],
            [InlineKeyboardButton(text="Отмена переноса", callback_data=f"reminder_snooze_cancel:{reminder_id}")],
        ]
    )


def nav_keyboard(show_add: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Следующий шаг", callback_data="next_step")]]
    if show_add:
        rows.append([InlineKeyboardButton(text="Добавить запись", callback_data="add_note")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def overload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Экстренный отдых", callback_data="overload_rest")],
            [InlineKeyboardButton(text="Собрать лёгкий план", callback_data="overload_light_plan")],
            [InlineKeyboardButton(text="Скипнуть и продолжить", callback_data="overload_skip")],
            [InlineKeyboardButton(text="Главное меню", callback_data="main_menu")],
        ]
    )
