from datetime import datetime, timedelta



from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_tasks, get_reminders_for_date, get_tasks_for_date, get_upcoming_overrides


router = Router()


@router.message(Command("today"))

async def today(message: Message, session_factory, time_service):
    now = time_service.now()
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    day_end = day_start + timedelta(days=1)
    async with session_factory() as session:
        tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
        reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
        overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
    text = [f"Сегодня: {now.strftime('%Y-%m-%d %A')}", "", "Задачи:"]
    text.extend([f"- {t.title}" for t in tasks] or ["- нет"])
    text.append("\nНапоминания:")
    text.extend([f"- {r.remind_at.strftime('%H:%M')} {r.text}" for r in reminders] or ["- нет"])
    text.append("\nOverrides:")
    text.extend([f"- {o.date} {o.mode}" for o in overrides[:5]] or ["- нет"])
    await message.answer("\n".join(text))


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


async def today(message: Message):
    await message.answer("План на сегодня (MVP): пока пусто, добавьте быструю запись или голос.")


@router.message(Command("tomorrow"))
async def tomorrow(message: Message):
    await message.answer("План на завтра (MVP): будет построен из задач, напоминаний и расписания.")


@router.message(Command("week"))
async def week(message: Message):
    await message.answer("Неделя (MVP): обзор будет доступен после накопления данных.")


@router.message(Command("tasks"))
async def tasks(message: Message):
    await message.answer("Активные задачи (MVP): модуль чтения из БД подключается следующим шагом.")


@router.message(Command("schedule"))
async def schedule(message: Message):
    await message.answer("Расписание (MVP): базовые правила и исключения будут показаны здесь.")



@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Отправь текст/голос: бот покажет Action Preview, затем подтверждение. Команды: /today /tasks /reminders /schedule /cleanup /sync_miro")


    await message.answer(
        "Команды: /today /tomorrow /week /quick /tasks /reminders /schedule /rest /finance /workout /study /goals /sync_miro /rebuild_miro /archive /cleanup /settings /help"
    )

