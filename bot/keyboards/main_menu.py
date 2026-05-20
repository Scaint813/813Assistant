from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Texts used as navigation guards in quick_note.py
MENU_TEXTS = {
    "Штаб", "Следующий шаг",
    "Деньги", "Заказы",
    "Учёба", "Тело",
    "Протоколы", "Архив",
    "Настройки",
}


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        ["Штаб", "Следующий шаг"],
        ["Деньги", "Заказы"],
        ["Учёба", "Тело"],
        ["Протоколы", "Архив"],
        ["Настройки"],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )
