from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_or_create_runtime_state,
    get_user_profile,
    get_or_create_user_profile,
)

logger = logging.getLogger(__name__)
router = Router()

# ── Keyboards ──────────────────────────────────────────────────────────────────

def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ИИ", callback_data="settings_ai"),
         InlineKeyboardButton(text="Токены", callback_data="settings_tokens")],
        [InlineKeyboardButton(text="Стиль", callback_data="settings_style"),
         InlineKeyboardButton(text="Check-ins", callback_data="settings_checkins")],
        [InlineKeyboardButton(text="Miro", callback_data="settings_miro"),
         InlineKeyboardButton(text="Health", callback_data="settings_health")],
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

async def _build_settings_text(user_id: int, session_factory, config, miro_service, prefs_service) -> tuple[str, dict]:
    async with session_factory() as session:
        profile = await get_or_create_user_profile(session, user_id, "", str(config.timezone))
        state = await get_or_create_runtime_state(session, user_id)
        await session.commit()

    prefs = prefs_service.get_all(profile)
    now_quiet = state.quiet_until
    from bot.services.time_service import TimeService as _TS
    quiet_str = f"до {now_quiet.strftime('%H:%M')}" if now_quiet else "нет"
    checkin_str = "включены" if state.checkin_enabled else "выключены"
    miro_str = "включён" if miro_service.is_configured() else "не настроен"
    cleanup_str = "включена" if prefs["chat_cleanup_enabled"] else "выключена"

    text = (
        "НАСТРОЙКИ\n\n"
        "ИИ:\n"
        f"Режим: {prefs['ai_mode']}\n"
        f"Детализация: {prefs['response_detail']}\n"
        f"Токены: {prefs['token_mode']}\n\n"
        "Система:\n"
        f"Check-ins: {checkin_str}\n"
        f"Тихий режим: {quiet_str}\n"
        f"Очистка чата: {cleanup_str}\n"
        f"Miro: {miro_str}\n\n"
        "Время:\n"
        f"Timezone: {config.timezone}\n"
        f"Утро: {config.checkin_morning_time}\n"
        f"День: {config.checkin_day_time}\n"
        f"Вечер: {config.checkin_evening_time}"
    )
    return text, prefs


# ── Main settings screen ───────────────────────────────────────────────────────

@router.message(Command("settings"))
@router.message(F.text == "Настройки")
async def settings_cmd(message: Message, session_factory, config, miro_service, screen_service, bot, preferences_service):
    text, _ = await _build_settings_text(
        message.from_user.id, session_factory, config, miro_service, preferences_service
    )
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=_settings_kb(),
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── Settings sub-screens via callbacks ─────────────────────────────────────────

@router.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery, session_factory, config, miro_service, preferences_service):
    await callback.answer()
    text, _ = await _build_settings_text(
        callback.from_user.id, session_factory, config, miro_service, preferences_service
    )
    await callback.message.edit_text(text, reply_markup=_settings_kb())


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
        "Auto — бот выбирает fast/smart сам.\n"
        "Economy — чаще дешёвая модель.\n"
        "Smart — чаще умная модель.\n"
        "Strict — коротко, структурно, без воды."
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
        "Auto — бот выбирает fast/smart сам.\n"
        "Economy — чаще дешёвая модель.\n"
        "Smart — чаще умная модель.\n"
        "Strict — коротко, структурно, без воды."
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
        "Economy — меньше контекста, дешевле.\n"
        "Balanced — стандартный режим.\n"
        "Maximum — больше контекста для сложных разборов.\n\n"
        "Точные расходы — в OpenAI Platform.\nЗдесь регулируется экономия контекста."
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
        "Economy — меньше контекста, дешевле.\n"
        "Balanced — стандартный режим.\n"
        "Maximum — больше контекста для сложных разборов.\n\n"
        "Точные расходы — в OpenAI Platform.\nЗдесь регулируется экономия контекста."
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
        "CHECK-INS\n\n"
        f"Статус: {'включены' if state.checkin_enabled else 'выключены'}\n"
        "Лимит: 3 в день\n\n"
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
        "CHECK-INS\n\n"
        "Статус: включены\nЛимит: 3 в день\n\n"
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
        status = "включён"
        zone = f"X={config.miro_ai_zone_start_x}, Y={config.miro_ai_zone_start_y}"
    else:
        board_display = "не задан"
        status = "не настроен"
        zone = "—"
    text = (
        "MIRO\n\n"
        f"Статус: {status}\n"
        f"Board: {board_display}\n"
        f"AI-зона: {zone}"
    )
    await callback.message.edit_text(text, reply_markup=_miro_kb(miro_service.is_configured()))


@router.callback_query(F.data == "do_sync_miro")
async def do_sync_miro(callback: CallbackQuery, session_factory, miro_service, time_service, config):
    await callback.answer("Синхронизирую...")
    if not miro_service.is_configured():
        await callback.message.edit_text("Miro не настроен.", reply_markup=_miro_kb(False))
        return
    try:
        async with session_factory() as session:
            stats = await miro_service.sync_all(callback.from_user.id, session, time_service, config)
            await session.commit()
        text = (
            "MIRO\n\nСинхронизация завершена.\n\n"
            f"Задачи: {stats['tasks']}\nНапоминания: {stats['reminders']}\n"
            f"Проблемы: {stats['problems']}\nАрхив: {stats['archive']}"
        )
    except Exception:
        text = "MIRO\n\nОшибка синхронизации. Проверь логи."
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
