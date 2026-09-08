from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database.queries import get_or_create_user_profile
from bot.keyboards.inline import daily_brief_keyboard, planner_keyboard

logger = logging.getLogger(__name__)
router = Router()


async def _user_now(session, user_id: int, time_service):
    profile = await get_or_create_user_profile(
        session, user_id, "", str(time_service.tz)
    )
    return time_service.in_timezone(profile.timezone).now()


async def _render_brief(
    user_id,
    chat_id,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        text = await daily_brief_service.build(session, user_id, now)
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            reply_markup=daily_brief_keyboard(),
        )
        await session.commit()


async def _render_conflicts(
    user_id,
    chat_id,
    session_factory,
    time_service,
    screen_service,
    bot,
    conflict_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
        conflicts = await conflict_service.detect_between(
            session, user_id, start, start + timedelta(days=1)
        )
        text = conflict_service.render(conflicts, now.tzinfo)
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            reply_markup=planner_keyboard("today"),
        )
        await session.commit()


@router.message(F.document)
async def import_hse_ical(
    message: Message,
    bot,
    hse_calendar_service,
):
    document = message.document
    file_name = (document.file_name or "").casefold()
    mime_type = (document.mime_type or "").casefold()
    if not file_name.endswith(".ics") and mime_type != "text/calendar":
        await message.answer(
            "Для расписания пришли файл .ics, экспортированный из расписания ВШЭ. "
            "Другие документы я пока не разбираю автоматически."
        )
        return
    if document.file_size and document.file_size > hse_calendar_service.MAX_ICAL_BYTES:
        await message.answer("Файл расписания больше 5 МБ — импорт остановлен.")
        return

    temp_path: Path | None = None
    try:
        file = await bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ics", delete=False) as tmp:
            temp_path = Path(tmp.name)
        await bot.download_file(file.file_path, str(temp_path))
        result = await hse_calendar_service.import_bytes(
            temp_path.read_bytes(), user_id=message.from_user.id
        )
    except ValueError as exc:
        await message.answer(f"Не удалось импортировать расписание: {escape(str(exc))}.")
        return
    except Exception as exc:
        logger.error("HSE iCal upload failed: %s", type(exc).__name__)
        await message.answer("Не удалось обработать .ics. Файл сохранён в чате — попробуй ещё раз.")
        return
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)

    period = ""
    if result.first_start and result.last_end:
        period = (
            f"\nПериод: {result.first_start.strftime('%d.%m.%Y')}–"
            f"{result.last_end.strftime('%d.%m.%Y')}."
        )
    await message.answer(
        f"Расписание ВШЭ обновлено: {result.imported} событий.{period}\n\n"
        "Теперь доступны «Сводка дня», «Покажи расписание на неделю» и «Конфликты»."
    )


@router.message(Command("hse_sync"))
async def sync_hse_feed(message: Message, hse_calendar_service):
    if message.from_user.id != hse_calendar_service.user_id:
        await message.answer("Для личного расписания пришли свой файл .ics.")
        return
    if not hse_calendar_service.is_configured:
        await message.answer(
            "Периодическая ссылка ВШЭ не настроена. Пришли файл .ics — он "
            "импортируется сразу; приватную ссылку можно задать в HSE_ICAL_URL."
        )
        return
    try:
        result = await hse_calendar_service.sync_configured_feed()
    except Exception as exc:
        logger.error("Manual HSE iCal sync failed: %s", type(exc).__name__)
        await message.answer("Синхронизация ВШЭ не удалась. Приватная ссылка не показана.")
        return
    await message.answer(f"Расписание ВШЭ обновлено: {result.imported} событий.")


@router.message(Command("hse_status"))
async def hse_status(message: Message, session_factory, time_service, hse_calendar_service):
    async with session_factory() as session:
        now = await _user_now(session, message.from_user.id, time_service)
        status = await hse_calendar_service.status(
            session, now, user_id=message.from_user.id
        )
    sync_label = "включена" if status["configured"] else "выключена"
    last_sync = status["synced_at"]
    last_sync_label = last_sync.strftime("%d.%m.%Y %H:%M") if last_sync else "ещё не было"
    await message.answer(
        "Расписание ВШЭ\n\n"
        f"Будущих событий: {status['events']}.\n"
        f"Последнее обновление: {last_sync_label}.\n"
        f"Периодическая синхронизация: {sync_label}."
    )


@router.message(Command("brief"))
async def daily_brief(
    message,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    await _render_brief(
        message.from_user.id,
        message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        daily_brief_service,
    )
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "daily_brief")
async def daily_brief_callback(
    callback: CallbackQuery,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    await callback.answer()
    await _render_brief(
        callback.from_user.id,
        callback.message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        daily_brief_service,
    )


async def _render_review(
    user_id,
    chat_id,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        text = await daily_brief_service.build_review(session, user_id, now)
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            reply_markup=daily_brief_keyboard(),
        )
        await session.commit()


@router.message(Command("day_review"))
async def daily_review(
    message,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    await _render_review(
        message.from_user.id,
        message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        daily_brief_service,
    )
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "daily_review")
async def daily_review_callback(
    callback: CallbackQuery,
    session_factory,
    time_service,
    screen_service,
    bot,
    daily_brief_service,
):
    await callback.answer()
    await _render_review(
        callback.from_user.id,
        callback.message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        daily_brief_service,
    )


@router.message(Command("conflicts"))
async def daily_conflicts(
    message,
    session_factory,
    time_service,
    screen_service,
    bot,
    conflict_service,
):
    await _render_conflicts(
        message.from_user.id,
        message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        conflict_service,
    )
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "daily_conflicts")
async def daily_conflicts_callback(
    callback: CallbackQuery,
    session_factory,
    time_service,
    screen_service,
    bot,
    conflict_service,
):
    await callback.answer()
    await _render_conflicts(
        callback.from_user.id,
        callback.message.chat.id,
        session_factory,
        time_service,
        screen_service,
        bot,
        conflict_service,
    )


@router.message(Command("home"))
async def save_home(message: Message, session_factory, time_service):
    value = (message.text or "").partition(" ")[2].strip()
    if not value:
        await message.answer("Формат: /home Дубки")
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(
            session, message.from_user.id, "", str(time_service.tz)
        )
        context = _profile_context(profile.preferences_json)
        context["home"] = value[:255]
        profile.preferences_json = json.dumps(context, ensure_ascii=False)
        await session.commit()
    await message.answer(f"Домашняя точка сохранена: {escape(value[:255])}.")


@router.message(Command("route_fact"))
async def save_route_fact(message: Message, session_factory, time_service):
    raw = (message.text or "").partition(" ")[2]
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 3:
        await message.answer("Формат: /route_fact Дубки | ВШЭ на Покровке | 75")
        return
    origin, destination, minutes_raw = parts
    try:
        minutes = int(minutes_raw)
    except ValueError:
        minutes = 0
    if not origin or not destination or not 1 <= minutes <= 24 * 60:
        await message.answer("Укажи две точки и время от 1 до 1440 минут.")
        return
    async with session_factory() as session:
        profile = await get_or_create_user_profile(
            session, message.from_user.id, "", str(time_service.tz)
        )
        context = _profile_context(profile.preferences_json)
        routes = context.get("route_minutes")
        if not isinstance(routes, dict):
            routes = {}
        key = f"{_location_key(origin)}->{_location_key(destination)}"
        routes[key] = minutes
        context["route_minutes"] = routes
        profile.preferences_json = json.dumps(context, ensure_ascii=False)
        await session.commit()
    await message.answer(
        f"Маршрут сохранён: {escape(origin)} → {escape(destination)} · {minutes} мин."
    )


def _profile_context(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _location_key(value: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", value.casefold()).strip()
