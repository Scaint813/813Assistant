from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_tasks, get_archived_tasks, get_reminders_for_date, get_tasks_for_date, get_upcoming_overrides
from bot.keyboards.inline import nav_keyboard
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.keyboards.main_menu import main_menu

router = Router()


async def render_hq(message: Message, session_factory, time_service):
    now = time_service.now()
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    day_end = day_start + timedelta(days=1)
    async with session_factory() as session:
        tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
        reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
        overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
    mode = overrides[0].mode if overrides and str(overrides[0].date) == str(now.date()) else "обычный"
    overdue = [t for t in tasks if t.deadline and t.deadline < now]
    focus = tasks[0].title if tasks else "нет"
    risks = []
    if overdue:
        risks.append("просрочка")
    if mode == "rest_day":
        risks.append("ресурс")
    text = (
        "ШТАБ\n\n"
        f"Сегодня: {now.strftime('%Y-%m-%d %A')}\n"
        f"Режим: {mode}\n\n"
        f"Фокус: {focus}\n"
        f"Задачи: {len(tasks)}\n"
        f"Напоминания: {len(reminders)}\n"
        f"Риски: {', '.join(risks) if risks else 'нет'}"
    )
    await message.answer(text, reply_markup=nav_keyboard(show_add=True))


@router.message(Command("today"))
@router.message(F.text == "Штаб")
async def today(message: Message, session_factory, time_service, navigation_service):
    navigation_service.push(message.from_user.id, "hq")
    await render_hq(message, session_factory, time_service)


@router.message(Command("next"))
@router.message(F.text == "Следующий шаг")
async def next_step(message: Message, session_factory, time_service, navigation_service, next_step_service):
    navigation_service.push(message.from_user.id, "next")
    async with session_factory() as session:
        payload = await next_step_service.build_next_step(message.from_user.id, session, time_service.now())
    picks = [f"{i+1}. {p}" for i, p in enumerate(payload.get("actions", [])[:3])]
    body = "\n".join(picks) if picks else "1. Закрыть один мелкий хвост.\n2. Подготовить следующий фокус."
    await message.answer(f"Следующий шаг:\n\n{body}\n\nОграничение: без лишних задач.", reply_markup=nav_keyboard())


@router.message(Command("problems"))
@router.message(Command("blocks"))
async def problems(message: Message, session_factory, time_service, problem_block_service):
    async with session_factory() as session:
        await problem_block_service.archive_expired_problem_blocks(message.from_user.id, session, time_service.now())
        blocks = await problem_block_service.get_active_problem_blocks(message.from_user.id, session, time_service.now())
        await session.commit()
    if not blocks:
        await message.answer("АКТИВНЫЕ БЛОКИ\n\n- нет активных.")
        return
    lines = ["АКТИВНЫЕ БЛОКИ", ""]
    for i, b in enumerate(blocks[:10]):
        lines.append(f"{i+1}. {b.title}")
        lines.append(f"   Категория: {b.category}")
        lines.append(f"   Следующий шаг: {b.next_action}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Следующий шаг", callback_data="next_step")],
        [InlineKeyboardButton(text="Архив", callback_data=f"problem_archive:{blocks[0].id}")],
        [InlineKeyboardButton(text="Главное меню", callback_data="main_menu")],
    ])
    await message.answer("\n".join(lines), reply_markup=kb)


async def _send_problem_checkin(message: Message, session_factory, time_service, problem_block_service, checkin_type: str):
    async with session_factory() as session:
        block = await problem_block_service.pick_checkin_problem_block(message.from_user.id, session, checkin_type, time_service.now())
    if not block:
        await message.answer("Штаб на связи.\nКритичных блоков сейчас нет.")
        return
    if checkin_type == "morning":
        text = f"Штаб на связи.\n\nАктивный блок:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Начать", callback_data="next_step")],
            [InlineKeyboardButton(text="Развернуть", callback_data=f"problem_expand:{block.id}")],
            [InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    elif checkin_type == "day":
        text = f"Промежуточная проверка.\n\nБлок ещё открыт:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Следующий шаг", callback_data="next_step")],
            [InlineKeyboardButton(text="Отложить", callback_data=f"problem_snooze:{block.id}")],
            [InlineKeyboardButton(text="Ресурсы", callback_data=f"problem_resources:{block.id}")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    else:
        text = f"Закрываем день.\n\nОткрытый блок:\n{block.title}\n\nЧто делаем?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Закрыть", callback_data=f"problem_done:{block.id}")],
            [InlineKeyboardButton(text="Отложить до завтра", callback_data=f"problem_snooze_tomorrow:{block.id}")],
            [InlineKeyboardButton(text="Оставить активным", callback_data=f"problem_keep_active:{block.id}")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    await message.answer(text, reply_markup=kb)


@router.message(Command("checkin_morning"))
async def checkin_morning(message: Message, session_factory, time_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "morning")


@router.message(Command("checkin_day"))
async def checkin_day(message: Message, session_factory, time_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "day")


@router.message(Command("checkin_evening"))
async def checkin_evening(message: Message, session_factory, time_service, problem_block_service):
    await _send_problem_checkin(message, session_factory, time_service, problem_block_service, "evening")


@router.message(Command("tasks"))
async def tasks(message: Message, session_factory):
    async with session_factory() as session:
        items = await get_active_tasks(session, message.from_user.id)
    await message.answer("Активные задачи:\n" + ("\n".join([f"- {t.title} [{t.priority}]" for t in items[:20]]) if items else "- нет"))


@router.message(Command("archive"))
async def archive_cmd(message: Message, session_factory):
    async with session_factory() as session:
        items = await get_archived_tasks(session, message.from_user.id)
    if not items:
        await message.answer("Архив пуст.")
        return
    lines = ["Архив:\n"]
    for i, t in enumerate(items[:20], start=1):
        reason = f" ({t.cleanup_reason})" if t.cleanup_reason else ""
        lines.append(f"{i}. {t.title}{reason}")
    await message.answer("\n".join(lines))


@router.message(Command("schedule"))
async def schedule(message: Message, session_factory, time_service):
    async with session_factory() as session:
        overrides = await get_upcoming_overrides(session, message.from_user.id, time_service.today())
    body = "\n".join([f"- {o.date}: {o.mode}" for o in overrides[:10]]) or "- нет"
    await message.answer(f"Ближайшие исключения расписания:\n{body}")


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Пиши обычным текстом. Команды запасные: /today /tasks /reminders /schedule /next /problems /archive /cleanup /sync_miro")


@router.message(F.text.in_({"Деньги", "Заказы", "Учёба", "Тело", "Протоколы", "Архив", "Настройки"}))
async def sections_stub(message: Message):
    await message.answer("Принял. Раздел в работе. Пиши задачу свободным текстом.", reply_markup=main_menu())
