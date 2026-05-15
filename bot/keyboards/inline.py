from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_keyboard(preview_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm_preview:{preview_id}")],
            [InlineKeyboardButton(text="Изменить", callback_data=f"edit_preview:{preview_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"cancel_preview:{preview_id}")],
        ]
    )
