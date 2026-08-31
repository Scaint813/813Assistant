from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.queries import (
    get_active_exam_dates,
    get_active_problem_blocks,
    get_active_study_schedule,
    get_archived_problem_blocks,
    get_archived_tasks,
    get_done_reminders_count,
    get_or_create_user_profile,
    get_problem_blocks_by_categories,
    get_reminders_by_keywords,
    get_tasks_by_keywords,
    get_upcoming_overrides,
    remember_entity,
)
from bot.keyboards.inline import problem_block_keyboard

router = Router()
logger = logging.getLogger(__name__)


async def _user_now(session, user_id: int, time_service):
    profile = await get_or_create_user_profile(
        session, user_id, "", str(time_service.tz)
    )
    return time_service.in_timezone(profile.timezone).now()

# ── Shared inline keyboards ───────────────────────────────────────────────────

def _section_kb(*extra_rows) -> InlineKeyboardMarkup | None:
    rows = list(extra_rows)
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _problems_next_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проблемные блоки", callback_data="nav_problems"),
         InlineKeyboardButton(text="Следующий шаг", callback_data="nav_next")],
    ])


def _archive_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="План на сегодня", callback_data="nav_today"),
         InlineKeyboardButton(text="Проблемы", callback_data="nav_problems")],
    ])


def _protocols_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перегруз", callback_data="protocol_overload"),
         InlineKeyboardButton(text="Проблемные блоки", callback_data="nav_problems")],
        [InlineKeyboardButton(text="Следующий шаг", callback_data="nav_next")],
    ])


def _study_kb(has_blocks: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Следующий шаг", callback_data="nav_next"),
         InlineKeyboardButton(text="Проблемные блоки", callback_data="nav_problems")],
    ]
    if has_blocks:
        rows.append([InlineKeyboardButton(text="Ресурсы", callback_data="study_resources")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _today_kb(screen) -> InlineKeyboardMarkup:
    rows = []
    entity = screen.primary_entity
    if entity and entity.get("type") == "task":
        rows.append([
            InlineKeyboardButton(
                text="Отметить главную задачу выполненной",
                callback_data=f"task_done:{entity['id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="Показать все задачи", callback_data="nav_tasks"),
        InlineKeyboardButton(text="Показать напоминания", callback_data="nav_reminders"),
    ])
    rows.append([
        InlineKeyboardButton(text="Разнести остаток", callback_data="plan_overflow_preview"),
        InlineKeyboardButton(text="Профиль", callback_data="settings_back"),
    ])
    rows.append([
        InlineKeyboardButton(text="Начать фокус", callback_data="nav_focus"),
        InlineKeyboardButton(text="Разобрать входящие", callback_data="nav_inbox"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tasks_kb(screen) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(text=f"Завершить №{index}", callback_data=f"task_done:{entity['id']}")]
        for index, entity in enumerate(screen.entities[:5], 1)
        if entity.get("type") == "task"
    ]
    rows.append([InlineKeyboardButton(text="План на сегодня", callback_data="nav_today")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reminders_kb(screen) -> InlineKeyboardMarkup:
    rows = []
    for entity in screen.entities[:5]:
        reminder_id = entity["id"]
        index = entity["index"]
        rows.append([
            InlineKeyboardButton(text=f"Выполнить №{index}", callback_data=f"reminder_done:{reminder_id}"),
            InlineKeyboardButton(text=f"Перенести №{index}", callback_data=f"reminder_snooze_menu:{reminder_id}"),
        ])
    rows.append([InlineKeyboardButton(text="План на сегодня", callback_data="nav_today")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Показать проекты", callback_data="nav_projects"),
         InlineKeyboardButton(text="Настроить подсказки", callback_data="nav_automations")],
        [InlineKeyboardButton(text="Показать архив", callback_data="nav_archive"),
         InlineKeyboardButton(text="Открыть настройки", callback_data="settings_back")],
    ])


async def _render_info_screen(
    message: Message,
    session_factory,
    screen_service,
    bot,
    text: str,
    reply_markup=None,
) -> None:
    """Render static navigation content without leaving another chat message."""
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=reply_markup,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


async def render_today_for_user(
    user_id,
    chat_id,
    session_factory,
    time_service,
    screen_service,
    bot,
    assistant_ux_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        screen = await assistant_ux_service.today(session, user_id, now)
        if screen.primary_entity:
            await remember_entity(
                session, user_id, screen.primary_entity["type"], screen.primary_entity["id"]
            )
        await session.commit()
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=user_id,
            chat_id=chat_id,
            text=screen.text,
            reply_markup=_today_kb(screen),
        )
        await session.commit()


async def render_tasks_for_user(
    user_id, chat_id, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        screen = await assistant_ux_service.tasks(session, user_id, now)
        if screen.entities:
            await remember_entity(session, user_id, "task", screen.entities[0]["id"])
        await session.commit()
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session, user_id=user_id, chat_id=chat_id,
            text=screen.text, reply_markup=_tasks_kb(screen),
        )
        await session.commit()


async def render_reminders_for_user(
    user_id, chat_id, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    async with session_factory() as session:
        now = await _user_now(session, user_id, time_service)
        screen = await assistant_ux_service.reminders(session, user_id, now)
        if screen.entities:
            await remember_entity(session, user_id, "reminder", screen.entities[0]["id"])
        await session.commit()
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session, user_id=user_id, chat_id=chat_id,
            text=screen.text, reply_markup=_reminders_kb(screen),
        )
        await session.commit()


async def build_archive_text(user_id, session_factory) -> str:
    async with session_factory() as session:
        tasks = await get_archived_tasks(session, user_id)
        blocks = await get_archived_problem_blocks(session, user_id)
        done_count = await get_done_reminders_count(session, user_id)
    if not tasks and not blocks and done_count == 0:
        return "Архив\n\nПока пусто. Здесь появятся завершённые задачи, напоминания и проблемы."
    lines = [
        "Архив",
        "",
        f"Завершённых задач: {len(tasks)}.",
        f"Завершённых напоминаний: {done_count}.",
        f"Закрытых проблем: {len(blocks)}.",
    ]
    recent = list(tasks[:5]) + list(blocks[:3])
    if recent:
        lines += ["", "Последние:"]
        lines.extend(f"• {item.title}" for item in recent[:8])
    return "\n".join(lines)


# ── Navigation callbacks (from inline buttons in sections) ─────────────────────

@router.callback_query(F.data == "nav_problems")
async def nav_problems_cb(callback, session_factory, time_service, screen_service, bot, problem_block_service):
    await callback.answer()
    # Delegate to problems handler logic inline
    async with session_factory() as session:
        await problem_block_service.archive_expired_problem_blocks(
            callback.from_user.id, session, time_service.now()
        )
        blocks = await problem_block_service.get_active_problem_blocks(
            callback.from_user.id, session, time_service.now()
        )
        await session.commit()
    if not blocks:
        text = "Активных проблем нет."
        kb = None
    else:
        lines = ["Проблемы, которые требуют решения", ""]
        for i, block in enumerate(blocks[:10], 1):
            lines.append(f"{i}. {block.title}")
            if block.next_action:
                lines.append(f"   Первый шаг: {block.next_action}")
        text = "\n".join(lines)
        kb = problem_block_keyboard(blocks[0].id)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session, user_id=callback.from_user.id,
            chat_id=callback.message.chat.id, text=text, reply_markup=kb,
        )
        await session.commit()


@router.callback_query(F.data == "nav_today")
async def nav_today_cb(
    callback, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    await callback.answer()
    await render_today_for_user(
        callback.from_user.id, callback.message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )


@router.callback_query(F.data == "nav_tasks")
async def nav_tasks_cb(
    callback, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    await callback.answer()
    await render_tasks_for_user(
        callback.from_user.id, callback.message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )


@router.callback_query(F.data == "nav_reminders")
async def nav_reminders_cb(
    callback, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    await callback.answer()
    await render_reminders_for_user(
        callback.from_user.id, callback.message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )


@router.callback_query(F.data == "nav_health")
async def nav_health_cb(
    callback, session_factory, time_service, assistant_ux_service,
):
    await callback.answer()
    async with session_factory() as session:
        screen = await assistant_ux_service.wellbeing(
            session, callback.from_user.id, time_service.now()
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Настроить Apple Health", callback_data="settings_health")],
        [InlineKeyboardButton(text="План на сегодня", callback_data="nav_today")],
    ])
    await callback.message.edit_text(screen.text, reply_markup=keyboard)


@router.callback_query(F.data == "nav_help")
async def nav_help_cb(callback):
    await callback.answer()
    await callback.message.edit_text(
        "Что можно написать ассистенту\n\n"
        "Задачи:\n"
        "«подготовить документы к пятнице, высокий приоритет»\n\n"
        "Напоминания:\n"
        "«напомни завтра в 18:20 сходить к врачу»\n\n"
        "Проекты:\n"
        "«разбей запуск магазина на конкретные шаги»\n\n"
        "Изменения:\n"
        "«перенеси задачу про документы на завтра»\n"
        "«отмени напоминание про врача»\n\n"
        "Сводки:\n"
        "«покажи проекты», «разберём входящие», «подведи итоги недели»\n\n"
        "Перед сохранением ассистент показывает понятную проверку."
    )


@router.callback_query(F.data == "nav_archive")
async def nav_archive_cb(callback, session_factory):
    await callback.answer()
    text = await build_archive_text(callback.from_user.id, session_factory)
    await callback.message.edit_text(text, reply_markup=_archive_kb())


@router.callback_query(F.data == "nav_next")
async def nav_next_cb(callback, session_factory, time_service, screen_service, bot, next_step_service):
    await callback.answer()
    async with session_factory() as session:
        now = await _user_now(session, callback.from_user.id, time_service)
        payload = await next_step_service.build_next_step(
            callback.from_user.id, session, now
        )
    picks = [f"{i+1}. {a}" for i, a in enumerate(payload.get("actions", [])[:3])]
    body = "\n".join(picks) if picks else "1. Зафиксировать новую задачу.\n2. Открыть Штаб и сверить состояние."
    text = f"СЛЕДУЮЩИЙ ШАГ\n\n{body}"
    from bot.keyboards.inline import next_step_entity_keyboard
    entities = payload.get("related_entities", [])
    kb = next_step_entity_keyboard(entities[0] if entities else None)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session, user_id=callback.from_user.id,
            chat_id=callback.message.chat.id, text=text, reply_markup=kb,
        )
        await session.commit()


@router.callback_query(F.data == "study_resources")
async def study_resources_cb(callback, session_factory, time_service, problem_resources_service):
    await callback.answer()
    async with session_factory() as session:
        blocks = await get_problem_blocks_by_categories(
            session, callback.from_user.id,
            ["study", "exam", "learning", "education"],
            time_service.now()
        )
    if not blocks:
        await callback.message.answer("Готовых ресурсов нет. Можно уточнить проблему текстом.")
        return
    block = blocks[0]
    import json
    resources = json.loads(block.resources_json or "[]")
    if not resources:
        await callback.message.answer("Для текущих блоков ресурсов нет. Можно уточнить проблему текстом.")
        return
    lines = ["Ресурсы:"]
    for r in resources[:5]:
        lines.append(f"— {r.get('title', '')}: {r.get('note', '')}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "protocol_overload")
async def protocol_overload_cb(
    callback, session_factory, time_service, assistant_ux_service,
):
    await callback.answer()
    async with session_factory() as session:
        screen = await assistant_ux_service.recovery_plan(
            session, callback.from_user.id, time_service.now()
        )
    from bot.keyboards.inline import overload_keyboard
    await callback.message.edit_text(screen.text, reply_markup=overload_keyboard())


@router.message(Command("today"))
@router.message(F.text.in_({"Сегодня", "План на сегодня", "Штаб", "Что у меня сегодня?"}))
async def today(
    message: Message, session_factory, time_service, navigation_service,
    screen_service, bot, assistant_ux_service,
):
    navigation_service.push(message.from_user.id, "today")
    await render_today_for_user(
        message.from_user.id, message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )
    await screen_service.delete_user_input(message)


# ── Следующий шаг (/next) ─────────────────────────────────────────────────────

@router.message(Command("next"))
@router.message(F.text == "Следующий шаг")
async def next_step(message: Message, session_factory, time_service, navigation_service, screen_service, bot, next_step_service):
    navigation_service.push(message.from_user.id, "next")
    async with session_factory() as session:
        now = await _user_now(session, message.from_user.id, time_service)
        payload = await next_step_service.build_next_step(
            message.from_user.id, session, now
        )

    picks = [f"{i+1}. {p}" for i, p in enumerate(payload.get("actions", [])[:3])]
    if picks:
        body = "\n".join(picks)
    else:
        # Check for rest_day mode
        async with session_factory() as session:
            now = await _user_now(session, message.from_user.id, time_service)
            overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
            await session.commit()
        is_rest = any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides)
        if is_rest:
            body = "Режим: отдых.\n\n1. Закрыть только срочное.\n2. Остальное оставить."
        else:
            body = "1. Зафиксировать новую задачу.\n2. Открыть Штаб и сверить состояние."

    text = f"СЛЕДУЮЩИЙ ШАГ\n\n{body}"

    from bot.keyboards.inline import next_step_entity_keyboard
    entities = payload.get("related_entities", [])
    kb = next_step_entity_keyboard(entities[0] if entities else None)

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text, reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── /problems ─────────────────────────────────────────────────────────────────

@router.message(Command("problems"))
@router.message(Command("blocks"))
async def problems(message: Message, session_factory, time_service, screen_service, bot, problem_block_service):
    async with session_factory() as session:
        await problem_block_service.archive_expired_problem_blocks(
            message.from_user.id, session, time_service.now()
        )
        blocks = await problem_block_service.get_active_problem_blocks(
            message.from_user.id, session, time_service.now()
        )
        await session.commit()

    if not blocks:
        text = "АКТИВНЫЕ БЛОКИ\n\n— нет активных."
        kb = None
    else:
        lines = ["АКТИВНЫЕ БЛОКИ\n"]
        for i, b in enumerate(blocks[:10]):
            lines.append(f"{i+1}. {b.title}")
            if b.next_action:
                lines.append(f"   → {b.next_action}")
        text = "\n".join(lines)
        kb = problem_block_keyboard(blocks[0].id)

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text, reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── Check-in commands ─────────────────────────────────────────────────────────

async def _send_problem_checkin(message, session_factory, time_service, problem_block_service, checkin_type):
    async with session_factory() as session:
        block = await problem_block_service.pick_checkin_problem_block(
            message.from_user.id, session, checkin_type, time_service.now()
        )
    if not block:
        await message.answer("Проблем, требующих отдельного решения, сейчас нет.")
        return

    if checkin_type == "morning":
        text = f"Проблема на сегодня:\n{block.title}\n\nПервое действие:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Начать", callback_data="checkin_next_step"),
             InlineKeyboardButton(text="Развернуть", callback_data=f"problem_expand:{block.id}")],
            [InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}"),
             InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    elif checkin_type == "day":
        text = f"Эта проблема ещё открыта:\n{block.title}\n\nПервое действие:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ресурсы", callback_data=f"problem_resources:{block.id}"),
             InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    else:
        text = f"Проблема осталась открытой:\n{block.title}\n\nПервое действие:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Закрыть", callback_data=f"problem_done:{block.id}"),
             InlineKeyboardButton(text="Отложить до завтра", callback_data=f"problem_snooze_tomorrow:{block.id}")],
            [InlineKeyboardButton(text="Оставить активным", callback_data=f"problem_keep_active:{block.id}"),
             InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    await message.answer(text, reply_markup=kb)


@router.message(Command("checkin_morning"))
async def checkin_morning(message, session_factory, time_service, screen_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "morning")
    await screen_service.delete_user_input(message)


@router.message(Command("checkin_day"))
async def checkin_day(message, session_factory, time_service, screen_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "day")
    await screen_service.delete_user_input(message)


@router.message(Command("checkin_evening"))
async def checkin_evening(message, session_factory, time_service, screen_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "evening")
    await screen_service.delete_user_input(message)


# ── /tasks ────────────────────────────────────────────────────────────────────

@router.message(Command("tasks"))
@router.message(F.text.in_({"Задачи", "Задачи и проекты", "Покажи все дела"}))
async def tasks_cmd(
    message, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    await render_tasks_for_user(
        message.from_user.id, message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )
    await screen_service.delete_user_input(message)


# ── /reminders ────────────────────────────────────────────────────────────────

@router.message(Command("reminders"))
@router.message(F.text == "Напоминания")
async def reminders_list(
    message, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    await render_reminders_for_user(
        message.from_user.id, message.chat.id, session_factory, time_service,
        screen_service, bot, assistant_ux_service,
    )
    await screen_service.delete_user_input(message)


@router.message(F.text == "Добавить задачу")
async def add_task_hint(message, session_factory, screen_service, bot):
    await _render_info_screen(
        message,
        session_factory,
        screen_service,
        bot,
        "Напиши задачу одним сообщением — форму заполнять не нужно.\n\n"
        "Хороший пример:\n"
        "«подготовить документы к врачу до пятницы, 30 минут; "
        "начать с проверки списка анализов».\n\n"
        "Если срок или длительность неизвестны, задача сохранится во Входящие — "
        "бот не станет придумывать их.\n\n"
        "Если это большой проект, напиши:\n"
        "«разбей запуск сайта на конкретные шаги и оцени время».\n\n"
        "Я покажу результат перед сохранением.",
    )


@router.message(F.text == "Ещё")
async def more_menu(message, session_factory, screen_service, bot):
    await _render_info_screen(
        message,
        session_factory,
        screen_service,
        bot,
        "Что открыть?",
        reply_markup=_more_kb(),
    )


# ── /schedule ─────────────────────────────────────────────────────────────────

@router.message(Command("schedule"))
async def schedule(message, session_factory, time_service, screen_service, bot):
    async with session_factory() as session:
        overrides = await get_upcoming_overrides(session, message.from_user.id, time_service.today())
    body = "\n".join([f"- {o.date}: {o.mode}" for o in overrides[:10]]) or "— нет"
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=f"Ближайшие исключения расписания:\n\n{body}",
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == "Что ты умеешь?")
async def help_cmd(message, session_factory, screen_service, bot):
    await _render_info_screen(
        message,
        session_factory,
        screen_service,
        bot,
        "Пиши обычным текстом — команды запоминать не нужно.\n\n"
        "Примеры:\n"
        "• «напомни завтра в 18:20 сходить к врачу»;\n"
        "• «разбей ремонт на конкретные задачи»;\n"
        "• «перенеси задачу про документы на завтра»;\n"
        "• «задача с документами готова»;\n"
        "• «покажи проекты»;\n"
        "• «подведи итоги недели».\n\n"
        "Перед изменением данных ассистент покажет проверку.",
    )


# ── /archive ──────────────────────────────────────────────────────────────────

@router.message(Command("archive"))
@router.message(F.text == "Архив")
async def archive_cmd(message, session_factory, time_service, screen_service, bot):
    text = await build_archive_text(message.from_user.id, session_factory)

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=_archive_kb(),
        )
        await session.commit()
    await screen_service.delete_user_input(message)


# ── Section screens ───────────────────────────────────────────────────────────

MONEY_KEYWORDS = ["оплата", "деньги", "платёж", "расчёт", "finance", "money", "payment", "прибыль", "долг"]
ORDERS_KEYWORDS = ["заказ", "клиент", "доставка", "выкуп", "order", "статус заказа", "артём"]
STUDY_KEYWORDS = ["учёба", "экзамен", "ошибка", "егэ", "study", "exam", "повторение", "подготовка"]
BODY_KEYWORDS = ["сон", "тело", "боль", "перегруз", "восстановление", "усталость", "здоровье"]


@router.message(F.text == "Деньги")
async def section_money(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    async with session_factory() as session:
        tasks = await get_tasks_by_keywords(session, message.from_user.id, MONEY_KEYWORDS)
        reminders = await get_reminders_by_keywords(session, message.from_user.id, MONEY_KEYWORDS)
        blocks = await get_problem_blocks_by_categories(
            session, message.from_user.id, ["money", "finance", "orders"], now
        )
    lines = [
        "ДЕНЬГИ",
        "",
        "Фокус:",
        "платежи, расчёты, прибыль, контроль обязательств.",
        "",
        "Сейчас:",
        f"— задач по деньгам: {len(tasks)}",
        f"— напоминаний по оплатам: {len(reminders)}",
        f"— проблемных блоков: {len(blocks)}",
        "",
        "Доступно:",
        "1. Зафиксировать задачу по платежу.",
        "2. Поставить напоминание по оплате.",
        "3. Создать проблемный блок по расчётам.",
        "4. Расчёт можно прислать обычным текстом.",
        "   Бот соберёт preview и предложит следующий шаг.",
        "",
        "Быстрый ввод:",
        "\"завтра напомни проверить оплату Артёма\"",
        "\"путаюсь в расчётах заказов\"",
    ]
    if tasks:
        lines += ["", "Активные:"] + [f"— {t.title}" for t in tasks[:3]]
    kb = _problems_next_kb()
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


@router.message(F.text == "Заказы")
async def section_orders(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    async with session_factory() as session:
        tasks = await get_tasks_by_keywords(session, message.from_user.id, ORDERS_KEYWORDS)
        reminders = await get_reminders_by_keywords(session, message.from_user.id, ORDERS_KEYWORDS)
        blocks = await get_problem_blocks_by_categories(
            session, message.from_user.id, ["orders", "clients", "delivery"], now
        )
    lines = [
        "ЗАКАЗЫ",
        "",
        "Фокус:",
        "клиенты, оплаты, выкуп, доставка, статусы.",
        "",
        "Сейчас:",
        f"— задач по заказам: {len(tasks)}",
        f"— напоминаний: {len(reminders)}",
        f"— проблемных блоков: {len(blocks)}",
        "",
        "Доступно:",
        "1. Зафиксировать задачу по заказу.",
        "2. Поставить напоминание по клиенту/оплате.",
        "3. Создать проблемный блок по зависшему процессу.",
        "4. Вынести следующий шаг по заказам.",
        "",
        "Быстрый ввод:",
        "\"надо ответить клиенту Артёму\"",
        "\"завтра напомни проверить оплату\"",
        "\"клиенты зависают на оплате\"",
    ]
    if tasks:
        lines += ["", "Активные:"] + [f"— {t.title}" for t in tasks[:3]]
    kb = _problems_next_kb()
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


@router.message(F.text == "Учёба")
@router.message(Command("study"))
async def section_study(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    today = now.date()
    exams, schedule, study_blocks = [], [], []
    try:
        async with session_factory() as session:
            exams = await get_active_exam_dates(session, message.from_user.id)
            schedule = await get_active_study_schedule(session, message.from_user.id)
            study_blocks = await get_problem_blocks_by_categories(
                session, message.from_user.id, ["exam", "study", "learning", "education"], now
            )
    except Exception:
        logger.exception("/study DB error user_id=%s", message.from_user.id)
        await message.answer("Учёба временно не открылась. Ошибка записана в лог.")
        await screen_service.delete_user_input(message)
        return
    logger.info("/study user_id=%s exams=%d schedule=%d blocks=%d",
        message.from_user.id, len(exams), len(schedule), len(study_blocks))

    # ── Compact /study format ────────────────────────────────────────────────

    # Short subject abbreviations
    def _short_subj(s: str) -> str:
        MAP = {
            "Русский язык": "Русский",
            "Обществознание": "Общество",
            "Базовая математика": "База",
            "Профильная математика": "Профиль",
            "Письменный английский": "Англ. письм.",
            "Устный английский": "Англ. устный",
            "Английский": "Английский",
        }
        return MAP.get(s, s[:15])

    lines = ["УЧЁБА", ""]

    if exams:
        lines.append("Экзамены:")
        for i, e in enumerate(exams, 1):
            days_left = (e.exam_date - today).days
            days_str = f", {days_left} дн." if days_left > 0 else (" (сегодня!)" if days_left == 0 else " (прошёл)")
            lines.append(f"{i}. {_short_subj(e.subject)} — {e.exam_date.strftime('%d.%m')}{days_str}")
        lines.append("")
    else:
        lines += ["Экзамены: не зафиксированы.", ""]

    if schedule:
        _WD_SHORT = {"mon": "пн", "tue": "вт", "wed": "ср",
                     "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}
        _WD_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_idx = today.weekday()  # 0=mon
        # Sort schedule by days from today
        def _days_until(item_wd: str) -> int:
            wd_idx = _WD_ORDER.index(item_wd) if item_wd in _WD_ORDER else 0
            d = (wd_idx - today_idx) % 7
            return d if d > 0 else 7
        sorted_sched = sorted(schedule, key=lambda s: _days_until(s.weekday or "mon"))
        show_sched = sorted_sched[:4]
        hidden = len(sorted_sched) - len(show_sched)
        lines.append("Занятия (ближайшие):")
        for s in show_sched:
            wd = _WD_SHORT.get(s.weekday or "", s.weekday or "")
            t = s.time_str or ""
            tutor = f", {s.tutor_name}" if s.tutor_name else ""
            lines.append(f"{wd} — {_short_subj(s.subject)} {t}{tutor}")
        if hidden > 0:
            lines.append(f"+ ещё {hidden}")
        lines.append("")
    else:
        lines += ["Занятия: не зафиксированы.", ""]

    if study_blocks:
        lines.append("Блоки:")
        for b in study_blocks[:3]:
            lines.append(f"• {b.title[:50]}")
        lines.append("")
    else:
        lines += ["Блоки: нет.", ""]

    if not exams and not schedule and not study_blocks:
        lines = [
            "УЧЁБА", "",
            "Данные ещё не зафиксированы.", "",
            "Напиши:",
            '"ЕГЭ по обществу 11 июня"',
            '"репетитор по англ по вторникам 18:00"',
            '"проблема с заданием 24 по обществу"',
        ]
    else:
        lines += ["Для следующего действия спроси: «что делать сейчас?»"]

    kb = _study_kb(bool(study_blocks))
    try:
        async with session_factory() as session:
            await screen_service.render_screen(
                bot=bot, session=session,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                text="\n".join(lines), reply_markup=kb,
            )
            await session.commit()
    except Exception:
        logger.exception("/study render error user_id=%s", message.from_user.id)
        await message.answer("\n".join(lines))
    await screen_service.delete_user_input(message)



@router.message(F.text.in_({"Здоровье и тренировки", "Тело"}))
async def section_body(
    message, session_factory, time_service, screen_service, bot, assistant_ux_service,
):
    async with session_factory() as session:
        screen = await assistant_ux_service.wellbeing(
            session, message.from_user.id, time_service.now()
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Тренировочный план", callback_data="training_generate_plan")],
        [InlineKeyboardButton(text="Данные Apple Health", callback_data="settings_health")],
        [InlineKeyboardButton(text="Мне тяжело — упростить день", callback_data="protocol_overload")],
    ])
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session, user_id=message.from_user.id,
            chat_id=message.chat.id, text=screen.text, reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


@router.message(F.text == "Протоколы")
async def section_protocols(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    async with session_factory() as session:
        blocks = await get_active_problem_blocks(session, message.from_user.id, now)
    lines = [
        "ПРОТОКОЛЫ", "",
        "Фокус:",
        "готовые сценарии поведения.", "",
        "Сейчас:",
        f"— активных проблемных блоков: {len(blocks)}",
        f"— протоколов из блоков: {len(blocks)}", "",
        "Доступно:",
        "1. Перегруз.",
        "2. Конфликт.",
        "3. Расчёт заказа.",
        "4. Учебная ошибка.",
        "5. Тихий режим.",
        "6. Следующий шаг.", "",
        "Быстрый ввод:",
        "\"собери протокол перегруза\"",
        "\"как ответить без эмоций\"",
        "\"сделай жёсткий план на 40 минут\"",
    ]
    if blocks:
        lines += ["", "Активные блоки:"] + [f"— {b.title}" for b in blocks[:3]]
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=_protocols_kb(),
        )
        await session.commit()
    await screen_service.delete_user_input(message)
