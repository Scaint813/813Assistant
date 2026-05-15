from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_tasks, get_reminders_for_date, get_tasks_for_date, get_upcoming_overrides
from bot.keyboards.inline import nav_keyboard
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
async def next_step(message: Message, session_factory, time_service, navigation_service):
    navigation_service.push(message.from_user.id, "next")
    async with session_factory() as session:
        payload = await message.bot.dispatcher["next_step_service"].build_next_step(message.from_user.id, session, time_service.now())
    picks = [f"{i+1}. {p}" for i, p in enumerate(payload.get("actions", [])[:3])]
    body = "\n".join(picks) if picks else "1. Закрыть один мелкий хвост.\n2. Подготовить следующий фокус."
    await message.answer(f"Следующий шаг:\n\n{body}\n\nОграничение: без лишних задач.", reply_markup=nav_keyboard())


@router.message(Command("problems"))
@router.message(Command("blocks"))
async def problems(message: Message, session_factory, time_service):
    async with session_factory() as session:
        service = message.bot.dispatcher["problem_block_service"]
        await service.archive_expired_problem_blocks(message.from_user.id, session, time_service.now())
        blocks = await service.get_active_problem_blocks(message.from_user.id, session, time_service.now())
        await session.commit()
    if not blocks:
        await message.answer("АКТИВНЫЕ БЛОКИ\n\n- нет активных.")
        return
    lines = ["АКТИВНЫЕ БЛОКИ", ""]
    for i, b in enumerate(blocks[:10]):
        lines.append(f"{i+1}. {b.title}")
        lines.append(f"   Категория: {b.category}")
        lines.append(f"   Следующий шаг: {b.next_action}")
    await message.answer("\n".join(lines))


@router.message(Command("tasks"))
async def tasks(message: Message, session_factory):
    async with session_factory() as session:
        items = await get_active_tasks(session, message.from_user.id)
    await message.answer("Активные задачи:\n" + ("\n".join([f"- {t.title} [{t.priority}]" for t in items[:20]]) if items else "- нет"))


@router.message(Command("schedule"))
async def schedule(message: Message, session_factory, time_service):
    async with session_factory() as session:
        overrides = await get_upcoming_overrides(session, message.from_user.id, time_service.today())
    body = "\n".join([f"- {o.date}: {o.mode}" for o in overrides[:10]]) or "- нет"
    await message.answer(f"Ближайшие исключения расписания:\n{body}")


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Пиши обычным текстом. Команды запасные: /today /tasks /reminders /schedule /next")


@router.message(F.text.in_({"Деньги", "Заказы", "Учёба", "Тело", "Протоколы", "Архив", "Настройки"}))
async def sections_stub(message: Message):
    await message.answer("Принял. Раздел в работе. Пиши задачу свободным текстом.", reply_markup=main_menu())
