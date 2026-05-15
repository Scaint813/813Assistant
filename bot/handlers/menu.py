from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("today"))
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
    await message.answer(
        "Команды: /today /tomorrow /week /quick /tasks /reminders /schedule /rest /finance /workout /study /goals /sync_miro /rebuild_miro /archive /cleanup /settings /help"
    )
