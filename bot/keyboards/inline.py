from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.intent_quality import confirmation_label


def confirm_keyboard(preview_id: int, parsed: dict | None = None) -> InlineKeyboardMarkup:
    intents = list((parsed or {}).get("intents") or [])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=confirmation_label(intents),
                    callback_data=f"confirm_preview:{preview_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="Изменить", callback_data=f"edit_preview:{preview_id}"),
                InlineKeyboardButton(
                    text="Это не поручение",
                    callback_data=f"not_action_preview:{preview_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="Отмена", callback_data=f"cancel_preview:{preview_id}"),
            ],
        ]
    )


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выполнено", callback_data=f"reminder_done:{reminder_id}")],
            [InlineKeyboardButton(text="Перенести", callback_data=f"reminder_snooze_menu:{reminder_id}")],
            [InlineKeyboardButton(text="Отменить", callback_data=f"reminder_cancel:{reminder_id}")],
        ]
    )


def reminder_snooze_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Через 1 час", callback_data=f"reminder_snooze_1h:{reminder_id}")],
            [InlineKeyboardButton(text="Сегодня вечером", callback_data=f"reminder_snooze_evening:{reminder_id}")],
            [InlineKeyboardButton(text="Завтра утром", callback_data=f"reminder_snooze_tomorrow_morning:{reminder_id}")],
            [InlineKeyboardButton(text="Другое время", callback_data=f"reminder_snooze_custom:{reminder_id}")],
            [InlineKeyboardButton(text="Отмена переноса", callback_data=f"reminder_snooze_cancel:{reminder_id}")],
        ]
    )


def workout_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Начать тренировку",
                callback_data=f"workout_start:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Перенести",
                callback_data=f"workout_move_menu:{workout_id}",
            ),
            InlineKeyboardButton(
                text="Пропустить",
                callback_data=f"workout_skip_menu:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Выполнено",
                callback_data=f"workout_done_menu:{workout_id}",
            ),
        ],
    ])


def training_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Составить тренировочную неделю",
                callback_data="training_generate_plan",
            ),
        ],
    ])


def workout_move_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="На завтра",
                callback_data=f"workout_move_days:{workout_id}:1",
            ),
            InlineKeyboardButton(
                text="На 2 дня",
                callback_data=f"workout_move_days:{workout_id}:2",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Другое время",
                callback_data=f"workout_move_custom:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"workout_actions:{workout_id}",
            ),
        ],
    ])


def workout_move_confirm_keyboard(
    workout_id: int,
    *,
    has_conflicts: bool = False,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Всё равно перенести одну" if has_conflicts else "Перенести только эту",
                callback_data=f"workout_move_confirm:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Пересобрать остаток недели",
                callback_data=f"workout_move_replan:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"workout_actions:{workout_id}",
            ),
        ],
    ])


def training_week_keyboard(
    items: list[tuple[int, str]],
    selected_id: int | None,
    action_keyboard: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("› " if workout_id == selected_id else "") + label,
                callback_data=f"training_workout:{workout_id}",
            )
        ]
        for workout_id, label in items
    ]
    if action_keyboard is not None:
        rows.extend(action_keyboard.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_exercise_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Записать подходы",
                callback_data=f"workout_log_sets:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Пропустить упражнение",
                callback_data=f"workout_skip_exercise:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Завершить тренировку",
                callback_data=f"workout_done_menu:{workout_id}",
            ),
        ],
    ])


def workout_skip_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Да, пропустить",
                callback_data=f"workout_skip_confirm:{workout_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Не пропускать",
                callback_data=f"workout_actions:{workout_id}",
            ),
        ],
    ])


def workout_rpe_keyboard(workout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"RPE {rpe}",
                callback_data=f"workout_done_rpe:{workout_id}:{rpe}",
            )
            for rpe in (6, 7, 8, 9)
        ],
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"workout_actions:{workout_id}",
            ),
        ],
    ])


def undo_keyboard(batch_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить последнее действие", callback_data=f"undo_batch:{batch_key}")],
    ])


def result_feedback_keyboard(preview_id: int, batch_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Всё верно", callback_data=f"result_ok:{preview_id}"),
            InlineKeyboardButton(text="Исправить", callback_data=f"result_fix:{preview_id}"),
        ],
        [
            InlineKeyboardButton(
                text="Отменить сохранение",
                callback_data=f"undo_batch:{batch_key}",
            )
        ],
    ])


def overload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пауза на 30 минут", callback_data="overload_rest")],
            [InlineKeyboardButton(text="Показать облегчённый план", callback_data="overload_light_plan")],
            [InlineKeyboardButton(text="Не менять план", callback_data="overload_skip")],
        ]
    )


def next_step_entity_keyboard(entity: dict | None = None) -> InlineKeyboardMarkup | None:
    """Inline actions only if there's a concrete entity to act on."""
    rows = []
    if entity and entity.get("type") == "task":
        rows.append([InlineKeyboardButton(text="Выполнено", callback_data=f"task_done:{entity['id']}")])
    elif entity and entity.get("type") == "reminder":
        rows.append([InlineKeyboardButton(text="Готово", callback_data=f"reminder_done:{entity['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def day_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗓 Разнести остаток по дням",
            callback_data="plan_overflow_preview",
        )],
        [InlineKeyboardButton(
            text="⚙️ Изменить вместимость",
            callback_data="settings_planning",
        ), InlineKeyboardButton(
            text="👤 Профиль",
            callback_data="settings_back",
        )],
    ])


def planner_keyboard(period: str = "today") -> InlineKeyboardMarkup:
    labels = (
        ("Сегодня", "today"),
        ("Завтра", "tomorrow"),
        ("7 дней", "week"),
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=("• " if value == period else "") + label,
                callback_data=f"planner:{value}",
            )
            for label, value in labels
        ],
        [
            InlineKeyboardButton(text="Сводка дня", callback_data="daily_brief"),
            InlineKeyboardButton(text="Конфликты", callback_data="daily_conflicts"),
        ],
        [
            InlineKeyboardButton(text="Все задачи", callback_data="nav_tasks"),
            InlineKeyboardButton(text="Напоминания", callback_data="nav_reminders"),
        ],
    ])


def daily_brief_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Планнер", callback_data="planner:today"),
            InlineKeyboardButton(text="Конфликты", callback_data="daily_conflicts"),
        ],
        [
            InlineKeyboardButton(text="Обновить", callback_data="daily_brief"),
            InlineKeyboardButton(text="Итоги дня", callback_data="daily_review"),
        ],
    ])


def overflow_proposal_keyboard(has_proposal: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if has_proposal:
        rows.append([InlineKeyboardButton(
            text="✅ Подходит — сохранить",
            callback_data="plan_overflow_confirm",
        )])
    rows.append([
        InlineKeyboardButton(
            text="⚙️ Изменить нагрузку",
            callback_data="settings_planning",
        ),
        InlineKeyboardButton(text="Не переносить", callback_data="plan_overflow_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkin_entity_keyboard(entity: dict) -> InlineKeyboardMarkup:
    rows = []
    if entity.get("type") == "task":
        rows.append([InlineKeyboardButton(text="Готово", callback_data=f"task_done:{entity['id']}")])
    elif entity.get("type") == "problem_block":
        rows.append([InlineKeyboardButton(text="Решено", callback_data=f"problem_done:{entity['id']}")])
    rows.append([
        InlineKeyboardButton(
            text="Не присылать такие подсказки",
            callback_data="checkins_disable",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quiet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="На 2 часа", callback_data="quiet_2h")],
        [InlineKeyboardButton(text="До завтра", callback_data="quiet_until_tomorrow")],
        [InlineKeyboardButton(text="Выключить автоподсказки", callback_data="checkins_disable")],
        [InlineKeyboardButton(text="Отмена", callback_data="quiet_cancel")],
    ])


def problem_block_keyboard(block_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ресурсы", callback_data=f"problem_resources:{block_id}"),
         InlineKeyboardButton(text="Развернуть", callback_data=f"problem_expand:{block_id}")],
        [InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block_id}"),
         InlineKeyboardButton(text="Закрыть", callback_data=f"problem_done:{block_id}")],
        [InlineKeyboardButton(text="Архив", callback_data=f"problem_archive:{block_id}")],
    ])


def problem_snooze_keyboard(block_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="До завтра", callback_data=f"problem_snooze_tomorrow:{block_id}")],
        [InlineKeyboardButton(text="На 3 дня", callback_data=f"problem_snooze_3d:{block_id}")],
        [InlineKeyboardButton(text="На неделю", callback_data=f"problem_snooze_week:{block_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"problem_snooze_cancel:{block_id}")],
    ])
