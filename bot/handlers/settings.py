from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_or_create_runtime_state,
    get_or_create_user_profile,
)
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)
router = Router()

# ── Keyboards ──────────────────────────────────────────────────────────────────

def _settings_kb(
    *, checkins_enabled: bool = False, quiet_active: bool = False,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Часовой пояс", callback_data="settings_timezone"),
         InlineKeyboardButton(text="🗓 Планирование", callback_data="settings_planning")],
        [InlineKeyboardButton(
            text=f"🔔 Check-ins: {'ВКЛ' if checkins_enabled else 'ВЫКЛ'}",
            callback_data="profile_toggle_checkins",
        ), InlineKeyboardButton(
            text="🌙 Тихо: 2 часа" if not quiet_active else "🌙 Тихий режим активен",
            callback_data="profile_quiet_2h",
        )],
        [InlineKeyboardButton(text="💬 Стиль ответа", callback_data="settings_style"),
         InlineKeyboardButton(text="🧠 Режим ИИ", callback_data="settings_ai")],
        [InlineKeyboardButton(text="⚙️ Расход контекста", callback_data="settings_tokens"),
         InlineKeyboardButton(text="🧹 Очистить чат", callback_data="settings_cleanup")],
    ])


def _cleanup_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Очистить недавние сообщения",
            callback_data="settings_cleanup_apply",
        )],
        [InlineKeyboardButton(text="Отмена", callback_data="settings_back")],
    ])


def _timezone_kb(current: str, user_profile_service, mismatched: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for option in user_profile_service.options():
        selected = "✓ " if current == option["timezone"] else ""
        rows.append([InlineKeyboardButton(
            text=f"{selected}{option['label']} · {option['offset']}",
            callback_data=f"set_timezone:{option['timezone']}",
        )])
    if mismatched:
        rows.append([InlineKeyboardButton(
            text=f"Исправить будущие напоминания · {mismatched}",
            callback_data="timezone_fix_existing",
        )])
    rows.append([InlineKeyboardButton(text="← Профиль", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _planning_kb(planning: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['daily_focus_minutes'] == 120 else ''}2ч",
                callback_data="set_plan_focus:120",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['daily_focus_minutes'] == 180 else ''}3ч",
                callback_data="set_plan_focus:180",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['daily_focus_minutes'] == 240 else ''}4ч",
                callback_data="set_plan_focus:240",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['daily_focus_minutes'] == 360 else ''}6ч",
                callback_data="set_plan_focus:360",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_start_hour'] == 8 else ''}с 8:00",
                callback_data="set_plan_start:8",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_start_hour'] == 9 else ''}с 9:00",
                callback_data="set_plan_start:9",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_start_hour'] == 10 else ''}с 10:00",
                callback_data="set_plan_start:10",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_end_hour'] == 18 else ''}до 18:00",
                callback_data="set_plan_end:18",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_end_hour'] == 21 else ''}до 21:00",
                callback_data="set_plan_end:21",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['workday_end_hour'] == 23 else ''}до 23:00",
                callback_data="set_plan_end:23",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['large_task_block_minutes'] == 45 else ''}блок 45м",
                callback_data="set_plan_block:45",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['large_task_block_minutes'] == 60 else ''}блок 60м",
                callback_data="set_plan_block:60",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['large_task_block_minutes'] == 90 else ''}блок 90м",
                callback_data="set_plan_block:90",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['large_task_block_minutes'] == 120 else ''}блок 120м",
                callback_data="set_plan_block:120",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if not planning['planning_weekends'] else ''}Пн–Пт",
                callback_data="set_plan_days:weekdays",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if planning['planning_weekends'] else ''}Каждый день",
                callback_data="set_plan_days:everyday",
            ),
        ],
        [InlineKeyboardButton(text="← Профиль", callback_data="settings_back")],
    ])


def _ai_kb(current: str) -> InlineKeyboardMarkup:
    opts = [("auto", "Auto"), ("economy", "Economy"), ("smart", "Smart"), ("strict", "Strict")]
    rows = [[InlineKeyboardButton(
        text=f"{'✓ ' if current == k else ''}{label}",
        callback_data=f"set_ai_mode:{k}"
    ) for k, label in opts]]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tokens_kb(current: str) -> InlineKeyboardMarkup:
    opts = [("economy", "Economy"), ("balanced", "Balanced"), ("maximum", "Maximum")]
    rows = [[InlineKeyboardButton(
        text=f"{'✓ ' if current == k else ''}{label}",
        callback_data=f"set_token_mode:{k}"
    ) for k, label in opts]]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _style_kb(detail: str, style: str) -> InlineKeyboardMarkup:
    detail_opts = [("short", "Коротко"), ("standard", "Стандарт"), ("detailed", "Подробно")]
    style_opts = [("dry", "Сухо"), ("balanced", "Сбалансировано"), ("soft", "Мягче")]
    rows = [
        [InlineKeyboardButton(
            text=f"{'✓ ' if detail == k else ''}{label}",
            callback_data=f"set_detail:{k}"
        ) for k, label in detail_opts],
        [InlineKeyboardButton(
            text=f"{'✓ ' if style == k else ''}{label}",
            callback_data=f"set_style:{k}"
        ) for k, label in style_opts],
        [InlineKeyboardButton(text="← Назад", callback_data="settings_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _checkins_kb(enabled: bool, quiet_active: bool) -> InlineKeyboardMarkup:
    toggle = InlineKeyboardButton(
        text="✓ Включены" if enabled else "Включить", callback_data="checkins_enable"
    ) if not enabled else InlineKeyboardButton(text="Выключить", callback_data="checkins_disable")
    rows = [
        [toggle],
        [InlineKeyboardButton(text="Тихий режим 2ч", callback_data="quiet_2h"),
         InlineKeyboardButton(text="До завтра", callback_data="quiet_until_tomorrow")],
        [InlineKeyboardButton(text="← Назад", callback_data="settings_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _miro_kb(configured: bool) -> InlineKeyboardMarkup:
    rows = []
    if configured:
        rows.append([InlineKeyboardButton(text="Sync Miro", callback_data="do_sync_miro")])
    rows.append([InlineKeyboardButton(text="Health", callback_data="settings_health")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _health_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить", callback_data="settings_health")],
        [InlineKeyboardButton(text="← Назад", callback_data="settings_back")],
    ])


# ── Screen builders ────────────────────────────────────────────────────────────

async def _build_settings_text(
    user_id: int, session_factory, config, miro_service, prefs_service,
    user_profile_service=None,
) -> tuple[str, dict]:
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, user_id, "", str(config.timezone))
        state = await get_or_create_runtime_state(session, user_id)
        await session.commit()

    prefs = prefs_service.get_all(profile)
    planning = prefs_service.planning(profile)
    now_quiet = state.quiet_until
    timezone = profile.timezone or str(config.timezone)
    local_now = datetime.now(tz=ZoneInfo(timezone))
    quiet_until = ensure_aware(now_quiet, ZoneInfo(timezone))
    quiet_active = bool(
        quiet_until
        and quiet_until > local_now
    )
    quiet_str = f"до {quiet_until.strftime('%H:%M')}" if quiet_active else "нет"
    checkin_str = "включены" if state.checkin_enabled else "выключены"

    if user_profile_service:
        timezone_label = user_profile_service.display_timezone(timezone)
    else:
        now = datetime.now(tz=ZoneInfo(timezone))
        timezone_label = f"{timezone} · UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}"
    text = (
        "👤 ПРОФИЛЬ\n\n"
        f"🌍 Местное время\n{timezone_label}\n\n"
        "🗓 Планирование\n"
        f"День: {planning['workday_start_hour']:02d}:00–{planning['workday_end_hour']:02d}:00\n"
        f"Реальный фокус: до {planning['daily_focus_minutes'] // 60} ч в день\n"
        f"Большая задача: блоками по {planning['large_task_block_minutes']} мин\n"
        f"Дни для задач: {'каждый день' if planning['planning_weekends'] else 'Пн–Пт'}\n\n"
        "🔔 Check-ins\n"
        f"Статус: {checkin_str}\n"
        f"Тихий режим: {quiet_str}\n\n"
        "💬 Ответы\n"
        f"Стиль: {prefs['answer_style']} · детализация: {prefs['response_detail']}\n"
        f"ИИ: {prefs['ai_mode']} · контекст: {prefs['token_mode']}\n\n"
        "Новые экраны заменяют предыдущие.\n"
        "Недавнюю историю можно удалить отдельной кнопкой ниже."
    )
    prefs["checkins_enabled"] = state.checkin_enabled
    prefs["quiet_active"] = quiet_active
    prefs["timezone"] = timezone
    return text, prefs


# ── Main settings screen ───────────────────────────────────────────────────────

@router.message(Command("settings"))
@router.message(F.text.in_({"Настройки", "Профиль"}))
async def settings_cmd(
    message: Message, session_factory, config, miro_service, screen_service, bot,
    preferences_service, user_profile_service,
):
    text, values = await _build_settings_text(
        message.from_user.id, session_factory, config, miro_service, preferences_service,
        user_profile_service,
    )
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=_settings_kb(
                checkins_enabled=values["checkins_enabled"],
                quiet_active=values["quiet_active"],
            ),
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── Settings sub-screens via callbacks ─────────────────────────────────────────

@router.callback_query(F.data == "settings_back")
async def settings_back(
    callback: CallbackQuery, session_factory, config, miro_service,
    preferences_service, user_profile_service,
):
    await callback.answer()
    text, values = await _build_settings_text(
        callback.from_user.id, session_factory, config, miro_service,
        preferences_service, user_profile_service,
    )
    await callback.message.edit_text(text, reply_markup=_settings_kb(
        checkins_enabled=values["checkins_enabled"],
        quiet_active=values["quiet_active"],
    ))


async def _timezone_screen(
    callback: CallbackQuery, session_factory, config, user_profile_service,
    *, notice: str = "",
):
    async with session_factory() as session:
        profile = await user_profile_service.get(session, callback.from_user.id)
        reminders = await get_active_reminders(session, callback.from_user.id)
        mismatched = 0
        for reminder in reminders:
            old_timezone = reminder.timezone or str(config.timezone)
            if old_timezone == profile.timezone:
                continue
            old_tz = ZoneInfo(old_timezone)
            old_at = ensure_aware(reminder.remind_at, old_tz)
            if old_at > datetime.now(tz=old_tz):
                mismatched += 1
        await session.commit()
    now = datetime.now(tz=ZoneInfo(profile.timezone))
    lines = [
        "🌍 ЧАСОВОЙ ПОЯС",
        "",
        f"Сейчас: {now.strftime('%H:%M · %d.%m.%Y')}",
        user_profile_service.display_timezone(profile.timezone, now),
        "",
        "Все новые сроки, планы и напоминания будут пониматься в этом времени.",
    ]
    if notice:
        lines += ["", notice]
    if mismatched:
        lines += [
            "",
            f"Будущих напоминаний в другом часовом поясе: {mismatched}.",
            "Я не меняю их молча — ниже можно явно исправить местное время.",
        ]
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_timezone_kb(profile.timezone, user_profile_service, mismatched),
    )


@router.callback_query(F.data == "settings_timezone")
async def settings_timezone(
    callback: CallbackQuery, session_factory, config, user_profile_service,
):
    await callback.answer()
    await _timezone_screen(
        callback, session_factory, config, user_profile_service
    )


@router.callback_query(F.data.startswith("set_timezone:"))
async def set_timezone(
    callback: CallbackQuery, session_factory, config, user_profile_service,
    checkin_service,
):
    timezone = callback.data.split(":", 1)[1]
    validated = user_profile_service.valid_timezone(timezone)
    await callback.answer("Местное время обновлено")
    async with session_factory() as session:
        await user_profile_service.set_timezone(
            session, callback.from_user.id, validated
        )
        await session.commit()
    checkin_service.reschedule_user_checkins(
        callback.from_user.id, callback.bot, validated
    )
    await _timezone_screen(
        callback,
        session_factory,
        config,
        user_profile_service,
        notice="Готово. Новые напоминания будут создаваться в выбранном местном времени.",
    )


@router.callback_query(F.data == "timezone_fix_existing")
async def timezone_fix_existing(
    callback: CallbackQuery, session_factory, config, user_profile_service,
    reminder_scheduler,
):
    await callback.answer("Исправляю будущие напоминания")
    changed = []
    async with session_factory() as session:
        profile = await user_profile_service.get(session, callback.from_user.id)
        timezone = ZoneInfo(profile.timezone)
        reminders = await get_active_reminders(session, callback.from_user.id)
        now = datetime.now(tz=timezone)
        for reminder in reminders:
            old_timezone = reminder.timezone or str(config.timezone)
            if old_timezone == profile.timezone:
                continue
            old_tz = ZoneInfo(old_timezone)
            old_at = ensure_aware(reminder.remind_at, old_tz)
            if old_at <= datetime.now(tz=old_tz):
                continue
            # Explicit correction keeps the clock/date the user typed and changes
            # which city that wall-clock belongs to.
            reminder.remind_at = old_at.replace(tzinfo=timezone)
            reminder.timezone = profile.timezone
            if reminder.remind_at > now:
                changed.append(reminder)
        await session.commit()
    for reminder in changed:
        reminder_scheduler.cancel_reminder_job(reminder.id)
        reminder_scheduler.schedule_reminder(reminder)
    await _timezone_screen(
        callback,
        session_factory,
        config,
        user_profile_service,
        notice=f"Исправлено будущих напоминаний: {len(changed)}.",
    )


async def _planning_screen(callback, session_factory, config, preferences_service):
    async with session_factory() as session:
        profile = await get_or_create_user_profile(
            session, callback.from_user.id, "", str(config.timezone)
        )
        planning = preferences_service.planning(profile)
        await session.commit()
    text = (
        "🗓 ПЛАНИРОВАНИЕ\n\n"
        f"Рабочее окно: {planning['workday_start_hour']:02d}:00–"
        f"{planning['workday_end_hour']:02d}:00\n"
        f"Лимит реального фокуса: {planning['daily_focus_minutes'] // 60} ч в день\n"
        f"Один блок большой задачи: {planning['large_task_block_minutes']} мин\n"
        f"Дни для задач: {'каждый день' if planning['planning_weekends'] else 'Пн–Пт'}\n\n"
        "Бот сначала вычитает занятое время календаря, затем ограничивает план "
        "дневным фокусом. Всё сверх лимита предлагает разнести по следующим дням."
    )
    await callback.message.edit_text(text, reply_markup=_planning_kb(planning))


@router.callback_query(F.data == "settings_planning")
async def settings_planning(
    callback: CallbackQuery, session_factory, config, preferences_service,
):
    await callback.answer()
    await _planning_screen(callback, session_factory, config, preferences_service)


@router.callback_query(F.data == "settings_cleanup")
async def settings_cleanup(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🧹 ОЧИСТКА ЧАТА\n\n"
        "Удалю недавние сообщения этого диалога, включая старые экраны, "
        "подсказки и уже показанные напоминания. Данные задач и напоминаний в базе "
        "останутся.\n\n"
        "Telegram разрешает боту удалять только сообщения младше 48 часов. "
        "Более старые можно удалить только вручную.",
        reply_markup=_cleanup_kb(),
    )


@router.callback_query(F.data == "settings_cleanup_apply")
async def settings_cleanup_apply(
    callback: CallbackQuery, session_factory, config, miro_service,
    preferences_service, user_profile_service, screen_service, bot,
):
    await callback.answer("Очищаю недавнюю историю")
    await screen_service.cleanup_recent_chat(
        bot,
        callback.message.chat.id,
        callback.message.message_id,
    )
    text, values = await _build_settings_text(
        callback.from_user.id,
        session_factory,
        config,
        miro_service,
        preferences_service,
        user_profile_service,
    )
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            text=(
                "Очистка завершена. Удалено всё, что Telegram разрешил удалить; "
                "сообщения старше 48 часов могут остаться.\n\n" + text
            ),
            reply_markup=_settings_kb(
                checkins_enabled=values["checkins_enabled"],
                quiet_active=values["quiet_active"],
            ),
        )
        await session.commit()


@router.callback_query(F.data.startswith("set_plan_days:"))
async def set_planning_days(
    callback: CallbackQuery, session_factory, config, preferences_service,
):
    value = callback.data.split(":", 1)[1]
    if value not in {"weekdays", "everyday"}:
        await callback.answer("Недоступное значение", show_alert=True)
        return
    await callback.answer("Дни планирования сохранены")
    async with session_factory() as session:
        profile = await get_or_create_user_profile(
            session, callback.from_user.id, "", str(config.timezone)
        )
        preferences_service.set(
            profile, "planning_weekends", value == "everyday"
        )
        await session.commit()
    await _planning_screen(callback, session_factory, config, preferences_service)


@router.callback_query(F.data.startswith("set_plan_focus:"))
@router.callback_query(F.data.startswith("set_plan_start:"))
@router.callback_query(F.data.startswith("set_plan_end:"))
@router.callback_query(F.data.startswith("set_plan_block:"))
async def set_planning_value(
    callback: CallbackQuery, session_factory, config, preferences_service,
):
    prefix, raw_value = callback.data.split(":", 1)
    value = int(raw_value)
    settings = {
        "set_plan_focus": ("daily_focus_minutes", {120, 180, 240, 360}),
        "set_plan_start": ("workday_start_hour", {8, 9, 10}),
        "set_plan_end": ("workday_end_hour", {18, 21, 23}),
        "set_plan_block": ("large_task_block_minutes", {45, 60, 90, 120}),
    }
    key, allowed = settings[prefix]
    if value not in allowed:
        await callback.answer("Недоступное значение", show_alert=True)
        return
    await callback.answer("Настройка сохранена")
    async with session_factory() as session:
        profile = await get_or_create_user_profile(
            session, callback.from_user.id, "", str(config.timezone)
        )
        preferences_service.set(profile, key, value)
        await session.commit()
    await _planning_screen(callback, session_factory, config, preferences_service)


@router.callback_query(F.data == "profile_toggle_checkins")
async def profile_toggle_checkins(
    callback: CallbackQuery, session_factory, config, miro_service,
    preferences_service, user_profile_service,
):
    async with session_factory() as session:
        state = await get_or_create_runtime_state(session, callback.from_user.id)
        state.checkin_enabled = not state.checkin_enabled
        enabled = state.checkin_enabled
        await session.commit()
    await callback.answer("Check-ins включены" if enabled else "Check-ins выключены")
    text, values = await _build_settings_text(
        callback.from_user.id, session_factory, config, miro_service,
        preferences_service, user_profile_service,
    )
    await callback.message.edit_text(text, reply_markup=_settings_kb(
        checkins_enabled=values["checkins_enabled"],
        quiet_active=values["quiet_active"],
    ))


@router.callback_query(F.data == "profile_quiet_2h")
async def profile_quiet_2h(
    callback: CallbackQuery, session_factory, config, miro_service,
    preferences_service, user_profile_service, checkin_service,
):
    async with session_factory() as session:
        await checkin_service.set_quiet_2h(callback.from_user.id, session)
        await session.commit()
    await callback.answer("Тихий режим включён на 2 часа")
    text, values = await _build_settings_text(
        callback.from_user.id, session_factory, config, miro_service,
        preferences_service, user_profile_service,
    )
    await callback.message.edit_text(text, reply_markup=_settings_kb(
        checkins_enabled=values["checkins_enabled"],
        quiet_active=values["quiet_active"],
    ))


@router.callback_query(F.data == "settings_ai")
async def settings_ai(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        await session.commit()
    current = preferences_service.ai_mode(profile)
    text = (
        "ИИ\n\n"
        f"Режим: {current}\n\n"
        "Auto — Luna для рутины, Terra для сложных запросов.\n"
        "Economy — всегда Luna.\n"
        "Smart — всегда Terra.\n"
        "Strict — Auto, но с коротким лимитом ответа."
    )
    await callback.message.edit_text(text, reply_markup=_ai_kb(current))


@router.callback_query(F.data.startswith("set_ai_mode:"))
async def set_ai_mode(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    if mode not in ("auto", "economy", "smart", "strict"):
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        preferences_service.set(profile, "ai_mode", mode)
        await session.commit()
    text = (
        "ИИ\n\n"
        f"Режим: {mode}\n\n"
        "Auto — Luna для рутины, Terra для сложных запросов.\n"
        "Economy — всегда Luna.\n"
        "Smart — всегда Terra.\n"
        "Strict — Auto, но с коротким лимитом ответа."
    )
    await callback.message.edit_text(text, reply_markup=_ai_kb(mode))


@router.callback_query(F.data == "settings_tokens")
async def settings_tokens(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        await session.commit()
    current = preferences_service.token_mode(profile)
    text = (
        "ТОКЕНЫ\n\n"
        f"Режим: {current}\n\n"
        "Economy — меньше контекста и reasoning.\n"
        "Balanced — оптимальный баланс (рекомендуется).\n"
        "Maximum — больше контекста и глубины.\n\n"
        "Режим влияет на контекст, reasoning и лимит ответа."
    )
    await callback.message.edit_text(text, reply_markup=_tokens_kb(current))


@router.callback_query(F.data.startswith("set_token_mode:"))
async def set_token_mode(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    if mode not in ("economy", "balanced", "maximum"):
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        preferences_service.set(profile, "token_mode", mode)
        await session.commit()
    text = (
        "ТОКЕНЫ\n\n"
        f"Режим: {mode}\n\n"
        "Economy — меньше контекста и reasoning.\n"
        "Balanced — оптимальный баланс (рекомендуется).\n"
        "Maximum — больше контекста и глубины.\n\n"
        "Режим влияет на контекст, reasoning и лимит ответа."
    )
    await callback.message.edit_text(text, reply_markup=_tokens_kb(mode))


@router.callback_query(F.data == "settings_style")
async def settings_style(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        await session.commit()
    detail = preferences_service.response_detail(profile)
    style = preferences_service.answer_style(profile)
    text = f"СТИЛЬ\n\nДетализация: {detail}\nТон: {style}"
    await callback.message.edit_text(text, reply_markup=_style_kb(detail, style))


@router.callback_query(F.data.startswith("set_detail:"))
async def set_detail(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    val = callback.data.split(":", 1)[1]
    if val not in ("short", "standard", "detailed"):
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        preferences_service.set(profile, "response_detail", val)
        style = preferences_service.answer_style(profile)
        await session.commit()
    await callback.message.edit_text(f"СТИЛЬ\n\nДетализация: {val}\nТон: {style}", reply_markup=_style_kb(val, style))


@router.callback_query(F.data.startswith("set_style:"))
async def set_style(callback: CallbackQuery, session_factory, config, preferences_service):
    await callback.answer()
    val = callback.data.split(":", 1)[1]
    if val not in ("dry", "balanced", "soft"):
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
        detail = preferences_service.response_detail(profile)
        preferences_service.set(profile, "answer_style", val)
        await session.commit()
    await callback.message.edit_text(f"СТИЛЬ\n\nДетализация: {detail}\nТон: {val}", reply_markup=_style_kb(detail, val))


@router.callback_query(F.data == "settings_checkins")
async def settings_checkins(callback: CallbackQuery, session_factory, config):
    await callback.answer()
    async with session_factory() as session:
        state = await get_or_create_runtime_state(session, callback.from_user.id)
        await session.commit()
    quiet_str = f"до {state.quiet_until.strftime('%H:%M')}" if state.quiet_until else "нет"
    text = (
        "АВТОПОДСКАЗКИ\n\n"
        f"Статус: {'включены' if state.checkin_enabled else 'выключены'}\n"
        "Не чаще одного сообщения в день.\n"
        "Только по конкретной задаче с близким или прошедшим сроком.\n"
        "Задачи без срока не будут вас тревожить; обычные напоминания работают отдельно.\n\n"
        f"Утро: {config.checkin_morning_time}\n"
        f"День: {config.checkin_day_time}\n"
        f"Вечер: {config.checkin_evening_time}\n\n"
        f"Тихий режим: {quiet_str}"
    )
    await callback.message.edit_text(text, reply_markup=_checkins_kb(state.checkin_enabled, bool(state.quiet_until)))


@router.callback_query(F.data == "checkins_enable")
async def checkins_enable(callback: CallbackQuery, session_factory, config):
    await callback.answer()
    async with session_factory() as session:
        state = await get_or_create_runtime_state(session, callback.from_user.id)
        state.checkin_enabled = True
        await session.commit()
    quiet_str = f"до {state.quiet_until.strftime('%H:%M')}" if state.quiet_until else "нет"
    text = (
        "АВТОПОДСКАЗКИ\n\n"
        "Статус: включены\n"
        "Не чаще одного сообщения в день и только по конкретному близкому сроку.\n"
        "Задачи без срока не будут вас тревожить.\n\n"
        f"Утро: {config.checkin_morning_time}\n"
        f"День: {config.checkin_day_time}\n"
        f"Вечер: {config.checkin_evening_time}\n\n"
        f"Тихий режим: {quiet_str}"
    )
    await callback.message.edit_text(text, reply_markup=_checkins_kb(True, bool(state.quiet_until)))


@router.callback_query(F.data == "settings_miro")
async def settings_miro(callback: CallbackQuery, miro_service, config):
    await callback.answer()
    if miro_service.is_configured():
        board_display = config.miro_board_id[:8] + "..." if config.miro_board_id else "—"
        access = await miro_service.debug_get_items()
        connected = access.get("status_code") == 200
        if connected:
            status = "подключён"
        elif access.get("status_code") in {401, 403}:
            status = "токен недействителен"
        else:
            status = "API недоступен"
        zone = f"X={config.miro_ai_zone_start_x}, Y={config.miro_ai_zone_start_y}"
    else:
        board_display = "не задан"
        status = "не настроен"
        connected = False
        zone = "—"

    zone_warning = ""
    if miro_service.is_configured() and config.miro_ai_zone_start_x < 6000:
        zone_warning = "\n\n⚠ AI-зона близко к ручной зоне.\nРекомендуется: X=12000, Y=3000\n(.env → MIRO_AI_ZONE_START_X)"

    text = (
        "MIRO\n\n"
        f"Статус: {status}\n"
        f"Board: {board_display}\n"
        f"AI-зона: {zone}"
        f"{zone_warning}"
    )
    await callback.message.edit_text(text, reply_markup=_miro_kb(connected))



@router.callback_query(F.data == "do_sync_miro")
async def do_sync_miro(callback: CallbackQuery, session_factory, miro_service, time_service, config, next_step_service):
    await callback.answer("Синхронизирую...")
    if not miro_service.is_configured():
        await callback.message.edit_text("Miro не настроен.", reply_markup=_miro_kb(False))
        return
    access = await miro_service.debug_get_items()
    if access.get("status_code") in {401, 403}:
        await callback.message.edit_text(
            "MIRO\n\nТекущий токен недействителен. Выпусти новый access token в Miro "
            "и замени MIRO_ACCESS_TOKEN на сервере.",
            reply_markup=_miro_kb(False),
        )
        return
    if access.get("status_code") != 200:
        await callback.message.edit_text(
            "MIRO\n\nAPI сейчас недоступен. Данные в базе не затронуты.",
            reply_markup=_miro_kb(False),
        )
        return
    try:
        async with session_factory() as session:
            stats = await miro_service.sync_all(
                callback.from_user.id, session, time_service, config,
                next_step_service=next_step_service,
            )
            await session.commit()
        errors = stats.get("errors", 0)
        total_items = stats.get("created", 0) + stats.get("updated", 0)
        if total_items == 0 and errors > 0:
            status = "Синхронизация не удалась."
        elif errors > 0:
            status = f"Синхронизация завершена частично.\nОшибки: {errors} — /miro_debug"
        else:
            status = "Синхронизация завершена."
        text = (
            f"MIRO\n\n{status}\n\n"
            "Секции: План · Сейчас · После этого · Ждёт решения · Завершено\n\n"
            f"Активные задачи: {stats.get('tasks', 0)}\n"
            f"Напоминания: {stats.get('reminders', 0)}\n"
            f"Проблемы: {stats.get('problems', 0)}\n"
            f"Закрытые элементы: {stats.get('archive', 0)}"
        )
    except Exception:
        text = "MIRO\n\nОшибка синхронизации. Проверь логи.\n/miro_debug — диагностика."
    await callback.message.edit_text(text, reply_markup=_miro_kb(miro_service.is_configured()))


@router.callback_query(F.data == "settings_health")
async def settings_health(callback: CallbackQuery, session_factory, time_service, miro_service, reminder_scheduler, config, preferences_service):
    await callback.answer()
    now = time_service.now()
    db_ok = True
    db_tasks = db_reminders = db_blocks = 0
    try:
        async with session_factory() as session:
            db_tasks = len(await get_active_tasks(session, callback.from_user.id))
            db_reminders = len(await get_active_reminders(session, callback.from_user.id))
            db_blocks = len(await get_active_problem_blocks(session, callback.from_user.id, now))
            profile = await get_or_create_user_profile(session, callback.from_user.id, "", str(config.timezone))
            await session.commit()
    except Exception:
        db_ok = False
        profile = None
    scheduler_running = reminder_scheduler.scheduler.running
    prefs = preferences_service.get_all(profile) if profile else {}
    text = (
        "HEALTH\n\n"
        f"DB: {'OK' if db_ok else 'FAIL'}\n"
        f"Scheduler: {'running' if scheduler_running else 'STOPPED'}\n"
        f"OpenAI: {'set' if config.openai_api_key else 'missing'}\n"
        f"Miro: {'set' if miro_service.is_configured() else 'missing'}\n"
        f"AI mode: {prefs.get('ai_mode', 'auto')}\n"
        f"Token mode: {prefs.get('token_mode', 'balanced')}\n"
        f"Timezone: {config.timezone}\n\n"
        f"Tasks: {db_tasks}\n"
        f"Reminders: {db_reminders}\n"
        f"Problems: {db_blocks}"
    )
    await callback.message.edit_text(text, reply_markup=_health_kb())
