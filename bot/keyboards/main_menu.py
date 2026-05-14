from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        ["Сегодня", "Быстрая запись", "Голосовая запись"],
        ["Задачи", "Напоминания", "Расписание"],
        ["Проекты", "Финансы", "Тренировки"],
        ["Учёба", "Цели", "Miro"],
        ["Архив", "AI-разбор"],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
    )
