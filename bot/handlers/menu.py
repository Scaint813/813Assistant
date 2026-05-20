from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import (
    get_active_tasks,
    get_archived_tasks,
    get_active_reminders,
    get_reminders_for_date,
    get_tasks_for_date,
    get_upcoming_overrides,
    get_or_create_runtime_state,
)
from bot.keyboards.inline import problem_block_keyboard, problem_snooze_keyboard
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

router = Router()

# ── HQ helper ─────────────────────────────────────────────────────────────────

async def _build_hq_text(message: Message, session_factory, time_service) -> str:
    now = time_service.now()
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    day_end = day_start + timedelta(days=1)
    async with session_factory() as session:
        tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
        reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
        overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        state = await get_or_create_runtime_state(session, message.from_user.id)
        await session.commit()

    mode = "обычный"
    if overrides and str(overrides[0].date) == str(now.date()):
        mode = overrides[0].mode
    if state.quiet_until and state.quiet_until > now:
        mode = "тихий"

    overdue = [t for t in tasks if t.deadline and t.deadline < now]
    focus = tasks[0].title if tasks else "нет"
    risks = []
    if overdue:
        risks.append("просрочка")
    if mode in {"rest_day", "тихий"}:
        risks.append("ресурс")

    dow = now.strftime("%A")
    return (
        "ШТАБ\n\n"
        f"Сегодня: {now.strftime('%Y-%m-%d')} {dow}\n"
        f"Режим: {mode}\n\n"
        f"Фокус: {focus}\n"
        f"Задачи: {len(tasks)}\n"
        f"Напоминания: {len(reminders)}\n"
        f"Риски: {', '.join(risks) if risks else 'нет'}"
    )


async def render_hq(message: Message, session_factory, time_service, screen_service, bot):
    text = await _build_hq_text(message, session_factory, time_service)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=None,  # No inline — navigation is in bottom ReplyKeyboard
        )
        await session.commit()


# ── Commands / reply buttons ───────────────────────────────────────────────────

@router.message(Command("today"))
@router.message(F.text == "Штаб")
async def today(message: Message, session_factory, time_service, navigation_service, screen_service, bot):
    navigation_service.push(message.from_user.id, "hq")
    await render_hq(message, session_factory, time_service, screen_service, bot)


@router.message(Command("next"))
@router.message(F.text == "Следующий шаг")
async def next_step(message: Message, session_factory, time_service, navigation_service, screen_service, bot, next_step_service):
    navigation_service.push(message.from_user.id, "next")
    async with session_factory() as session:
        payload = await next_step_service.build_next_step(message.from_user.id, session, time_service.now())

    picks = [f"{i+1}. {p}" for i, p in enumerate(payload.get("actions", [])[:3])]
    body = "\n".join(picks) if picks else "1. Закрыть один мелкий хвост.\n2. Подготовить следующий фокус."
    text = f"Следующий шаг:\n\n{body}\n\nОграничение: без лишних задач."

    # Only show inline if there's a concrete entity to act on
    from bot.keyboards.inline import next_step_entity_keyboard
    entities = payload.get("related_entities", [])
    entity = entities[0] if entities else None
    kb = next_step_entity_keyboard(entity)

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text, reply_markup=kb,
        )
        await session.commit()


@router.message(Command("problems"))
@router.message(Command("blocks"))
async def problems(message: Message, session_factory, time_service, screen_service, bot, problem_block_service):
    async with session_factory() as session:
        await problem_block_service.archive_expired_problem_blocks(message.from_user.id, session, time_service.now())
        blocks = await problem_block_service.get_active_problem_blocks(message.from_user.id, session, time_service.now())
        await session.commit()

    if not blocks:
        async with session_factory() as session:
            await screen_service.render_screen(
                bot=bot, session=session,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                text="АКТИВНЫЕ БЛОКИ\n\n— нет активных.",
            )
            await session.commit()
        return

    lines = ["АКТИВНЫЕ БЛОКИ\n"]
    for i, b in enumerate(blocks[:10]):
        lines.append(f"{i+1}. {b.title}")
        lines.append(f"   Категория: {b.category}")
        lines.append(f"   Следующий шаг: {b.next_action}")
    # Show action keyboard only for the top block
    kb = problem_block_keyboard(blocks[0].id)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=kb,
        )
        await session.commit()


async def _send_problem_checkin(
    message: Message, session_factory, time_service, screen_service, bot, problem_block_service, checkin_type: str
):
    async with session_factory() as session:
        block = await problem_block_service.pick_checkin_problem_block(
            message.from_user.id, session, checkin_type, time_service.now()
        )
    if not block:
        await message.answer("Штаб на связи.\nКритичных блоков сейчас нет.")
        return

    if checkin_type == "morning":
        text = f"Штаб на связи.\n\nАктивный блок:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Начать", callback_data="checkin_next_step"),
             InlineKeyboardButton(text="Развернуть", callback_data=f"problem_expand:{block.id}")],
            [InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}"),
             InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    elif checkin_type == "day":
        text = f"Промежуточная проверка.\n\nБлок ещё открыт:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ресурсы", callback_data=f"problem_resources:{block.id}"),
             InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    else:
        text = f"Закрываем день.\n\nОткрытый блок:\n{block.title}\n\nЧто делаем?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Закрыть", callback_data=f"problem_done:{block.id}"),
             InlineKeyboardButton(text="Отложить до завтра", callback_data=f"problem_snooze_tomorrow:{block.id}")],
            [InlineKeyboardButton(text="Оставить активным", callback_data=f"problem_keep_active:{block.id}"),
             InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    await message.answer(text, reply_markup=kb)


@router.message(Command("checkin_morning"))
async def checkin_morning(message: Message, session_factory, time_service, screen_service, bot, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, screen_service, bot, problem_block_service, "morning")


@router.message(Command("checkin_day"))
async def checkin_day(message: Message, session_factory, time_service, screen_service, bot, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, screen_service, bot, problem_block_service, "day")


@router.message(Command("checkin_evening"))
async def checkin_evening(message: Message, session_factory, time_service, screen_service, bot, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, screen_service, bot, problem_block_service, "evening")


@router.message(Command("tasks"))
async def tasks_cmd(message: Message, session_factory, screen_service, bot):
    async with session_factory() as session:
        items = await get_active_tasks(session, message.from_user.id)
    body = "\n".join([f"- {t.title} [{t.priority}]" for t in items[:20]]) if items else "— нет"
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=f"Активные задачи:\n\n{body}",
        )
        await session.commit()


@router.message(Command("archive"))
@router.message(F.text == "Архив")
async def archive_cmd(message: Message, session_factory, screen_service, bot):
    async with session_factory() as session:
        items = await get_archived_tasks(session, message.from_user.id)
    if not items:
        text = "Архив пуст."
    else:
        lines = ["Архив:\n"]
        for i, t in enumerate(items[:20], start=1):
            reason = f" ({t.cleanup_reason})" if t.cleanup_reason else ""
            lines.append(f"{i}. {t.title}{reason}")
        text = "\n".join(lines)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
        )
        await session.commit()


@router.message(Command("reminders"))
async def reminders_list(message: Message, session_factory, time_service, screen_service, bot):
    async with session_factory() as session:
        items = await get_active_reminders(session, message.from_user.id)
    if not items:
        text = "Активных напоминаний нет."
    else:
        now = time_service.now()
        lines = ["Активные напоминания:\n"]
        for idx, r in enumerate(items[:20], start=1):
            overdue = " (просрочено)" if r.remind_at <= now else ""
            lines.append(f"{idx}. {r.text}\n   {r.remind_at.strftime('%Y-%m-%d %H:%M')}{overdue}")
        text = "\n".join(lines)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
        )
        await session.commit()


@router.message(Command("schedule"))
async def schedule(message: Message, session_factory, time_service, screen_service, bot):
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


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Пиши задачу обычным текстом.\n\n"
        "Команды: /today /tasks /reminders /schedule /next /problems "
        "/archive /cleanup /sync_miro /health"
    )


@router.message(F.text.in_({"Деньги", "Заказы", "Учёба", "Тело", "Протоколы", "Настройки"}))
async def sections_stub(message: Message, screen_service, bot, session_factory):
    name = message.text
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=f"{name}\n\nРаздел в работе.\nПиши задачу свободным текстом.",
        )
        await session.commit()
