from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data="preview:confirm")],
            [InlineKeyboardButton(text="Изменить", callback_data="preview:edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="preview:cancel")],
        ]
    )
