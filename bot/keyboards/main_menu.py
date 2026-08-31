from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Texts used as navigation guards in quick_note.py. Legacy labels stay here so
# an old persistent Telegram keyboard cannot accidentally create a task.
MENU_TEXTS = {
    "Сегодня", "Задачи",
    "План на сегодня", "Добавить задачу",
    "Задачи и проекты", "Напоминания",
    "Здоровье и тренировки", "Ещё",
    "Штаб", "Следующий шаг",
    "Деньги", "Заказы",
    "Учёба", "Тело",
    "Протоколы", "Архив",
    "Настройки",
    "Профиль",
    "Что у меня сегодня?", "Покажи все дела", "Что ты умеешь?",
}


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        ["Что у меня сегодня?", "Покажи все дела"],
        ["Профиль", "Что ты умеешь?"],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Напиши или скажи, что нужно…",
    )
