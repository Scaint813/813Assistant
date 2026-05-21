from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_archived_problem_blocks,
    get_archived_tasks,
    get_done_reminders_count,
    get_or_create_runtime_state,
    get_problem_blocks_by_categories,
    get_reminders_by_keywords,
    get_tasks_by_keywords,
    get_upcoming_overrides,
    get_reminders_for_date,
    get_tasks_for_date,
)
from bot.keyboards.inline import problem_block_keyboard

router = Router()

# ── Shared inline keyboards for sections ──────────────────────────────────────

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
        [InlineKeyboardButton(text="Штаб", callback_data="go_hq"),
         InlineKeyboardButton(text="Проблемы", callback_data="nav_problems")],
    ])


def _body_overload_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Экстренный отдых", callback_data="overload_rest"),
         InlineKeyboardButton(text="Лёгкий план", callback_data="overload_light_plan")],
        [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
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
            bot=bot, session=session, user_id=callback.from_user.id,
            chat_id=callback.message.chat.id, text=text, reply_markup=kb,
        )
        await session.commit()


@router.callback_query(F.data == "nav_next")
async def nav_next_cb(callback, session_factory, time_service, screen_service, bot, next_step_service):
    await callback.answer()
    async with session_factory() as session:
        payload = await next_step_service.build_next_step(
            callback.from_user.id, session, time_service.now()
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
async def protocol_overload_cb(callback):
    await callback.answer()
    text = (
        "ПРОТОКОЛ: ПЕРЕГРУЗ\n\n"
        "Ресурс просел.\n\n"
        "1. Вода.\n"
        "2. Еда.\n"
        "3. 20–40 минут без телефона.\n"
        "4. Проверить тело/боль.\n"
        "5. Убрать одну необязательную задачу.\n"
        "6. Сон в приоритет.\n\n"
        "После стабилизации — открыть Штаб."
    )
    from bot.keyboards.inline import overload_keyboard
    await callback.message.answer(text, reply_markup=overload_keyboard())


# ── HQ (Штаб) ─────────────────────────────────────────────────────────────────

async def _build_hq_text(message: Message, session_factory, time_service) -> str:
    now = time_service.now()
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    day_end = day_start + timedelta(days=1)
    async with session_factory() as session:
        tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
        all_tasks = await get_active_tasks(session, message.from_user.id)
        reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
        all_reminders = await get_active_reminders(session, message.from_user.id)
        blocks = await get_active_problem_blocks(session, message.from_user.id, now)
        overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        state = await get_or_create_runtime_state(session, message.from_user.id)
        await session.commit()

    mode = "обычный"
    if overrides and str(overrides[0].date) == str(now.date()):
        mode = overrides[0].mode
    if state.quiet_until and state.quiet_until > now:
        mode = "тихий"

    overdue = [t for t in all_tasks if t.deadline and t.deadline < now]
    risks = []
    if overdue:
        risks.append(f"просрочка ({len(overdue)})")
    if mode in {"rest_day", "тихий"}:
        risks.append("ресурс")
    if len(blocks) > 3:
        risks.append(f"блоков ({len(blocks)})")

    focus = all_tasks[0].title if all_tasks else None
    dow = now.strftime("%A")

    lines = [
        "ШТАБ",
        "",
        f"Сегодня: {now.strftime('%Y-%m-%d')} {dow}",
        f"Режим: {mode}",
        "",
    ]
    if focus:
        lines += [f"Фокус: {focus}", ""]
    else:
        lines += ["Фокус: не задан", ""]

    lines += [
        f"Задачи: {len(all_tasks)}",
        f"Напоминания: {len(all_reminders)}",
        f"Проблемы: {len(blocks)}",
        f"Риски: {', '.join(risks) if risks else 'нет'}",
    ]

    if not all_tasks and not all_reminders and not blocks:
        lines += ["", "Следующий шаг:", "зафиксировать первую задачу."]

    return "\n".join(lines)


async def render_hq(message: Message, session_factory, time_service, screen_service, bot):
    text = await _build_hq_text(message, session_factory, time_service)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text, reply_markup=None,
        )
        await session.commit()


@router.message(Command("today"))
@router.message(F.text == "Штаб")
async def today(message: Message, session_factory, time_service, navigation_service, screen_service, bot):
    navigation_service.push(message.from_user.id, "hq")
    await render_hq(message, session_factory, time_service, screen_service, bot)
    await screen_service.delete_user_input(message)


# ── Следующий шаг (/next) ─────────────────────────────────────────────────────

@router.message(Command("next"))
@router.message(F.text == "Следующий шаг")
async def next_step(message: Message, session_factory, time_service, navigation_service, screen_service, bot, next_step_service):
    navigation_service.push(message.from_user.id, "next")
    async with session_factory() as session:
        payload = await next_step_service.build_next_step(
            message.from_user.id, session, time_service.now()
        )

    picks = [f"{i+1}. {p}" for i, p in enumerate(payload.get("actions", [])[:3])]
    if picks:
        body = "\n".join(picks)
    else:
        # Check for rest_day mode
        async with session_factory() as session:
            state = await get_or_create_runtime_state(session, message.from_user.id)
            overrides = await get_upcoming_overrides(session, message.from_user.id, time_service.now().date())
            await session.commit()
        is_rest = any(str(o.date) == str(time_service.now().date()) and o.mode == "rest_day" for o in overrides)
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
async def tasks_cmd(message, session_factory, screen_service, bot):
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
    await screen_service.delete_user_input(message)


# ── /reminders ────────────────────────────────────────────────────────────────

@router.message(Command("reminders"))
async def reminders_list(message, session_factory, time_service, screen_service, bot):
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
    await screen_service.delete_user_input(message)


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
async def help_cmd(message, screen_service):
    await message.answer(
        "Пиши задачу обычным текстом.\n\n"
        "Команды: /today /tasks /reminders /schedule /next /problems "
        "/archive /cleanup /sync_miro /health /settings"
    )
    await screen_service.delete_user_input(message)


# ── /archive ──────────────────────────────────────────────────────────────────

@router.message(Command("archive"))
@router.message(F.text == "Архив")
async def archive_cmd(message, session_factory, time_service, screen_service, bot):
    async with session_factory() as session:
        tasks = await get_archived_tasks(session, message.from_user.id)
        blocks = await get_archived_problem_blocks(session, message.from_user.id)
        done_count = await get_done_reminders_count(session, message.from_user.id)

    if not tasks and not blocks and done_count == 0:
        lines = [
            "АРХИВ",
            "",
            "Пока пусто.",
            "",
            "Сюда будут уходить:",
            "1. закрытые задачи;",
            "2. завершённые блоки;",
            "3. истёкшие дедлайны.",
        ]
    else:
        lines = [
            "АРХИВ",
            "",
            f"Задачи: {len(tasks)}",
            f"Напоминания (завершено): {done_count}",
            f"Проблемные блоки: {len(blocks)}",
        ]
        recent = list(tasks[:5]) + list(blocks[:3])
        if recent:
            lines += ["", "Последнее:"]
            for i, item in enumerate(recent[:8], 1):
                title = getattr(item, "title", "")
                lines.append(f"{i}. {title}")

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines),
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
async def section_study(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    async with session_factory() as session:
        tasks = await get_tasks_by_keywords(session, message.from_user.id, STUDY_KEYWORDS)
        reminders = await get_reminders_by_keywords(session, message.from_user.id, STUDY_KEYWORDS)
        blocks = await get_problem_blocks_by_categories(
            session, message.from_user.id, ["study", "exam", "learning", "education"], now
        )
    lines = [
        "УЧЁБА",
        "",
        "Фокус:",
        "ошибки, повторение, дедлайны, подготовка.",
        "",
        "Сейчас:",
        f"— учебных задач: {len(tasks)}",
        f"— активных блоков: {len(blocks)}",
        f"— напоминаний: {len(reminders)}",
        "",
        "Доступно:",
        "1. Записать ошибку.",
        "2. Создать активный блок подготовки.",
        "3. Поставить повторение.",
        "4. Получить следующий шаг.",
        "",
        "Быстрый ввод:",
        "\"ошибка по обществу 24: план слишком общий\"",
        "\"по русскому проблема с комментарием\"",
        "\"завтра напомни повторить английский\"",
    ]
    if tasks:
        lines += ["", "Активные:"] + [f"— {t.title}" for t in tasks[:3]]
    kb = _study_kb(bool(blocks))
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=kb,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


@router.message(F.text == "Тело")
async def section_body(message, session_factory, time_service, screen_service, bot):
    now = time_service.now()
    async with session_factory() as session:
        state = await get_or_create_runtime_state(session, message.from_user.id)
        overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        blocks = await get_problem_blocks_by_categories(
            session, message.from_user.id, ["body", "health", "sleep", "recovery"], now
        )
        reminders = await get_reminders_by_keywords(session, message.from_user.id, BODY_KEYWORDS)
        await session.commit()

    mode = "обычный"
    is_overload = False
    if overrides and str(overrides[0].date) == str(now.date()):
        mode = overrides[0].mode
        if mode == "rest_day":
            mode = "отдых"
    if state.quiet_until and state.quiet_until > now:
        mode = "тихий"

    # Detect overload from recent activity (simple heuristic)
    if any(kw in ["перегруз", "не вывожу"] for kw in BODY_KEYWORDS if False):  # placeholder
        is_overload = True

    if is_overload:
        lines = [
            "ТЕЛО", "",
            "Режим: перегруз", "",
            "Ресурс просел.\nСначала стабилизация.", "",
            "1. Вода.",
            "2. Еда.",
            "3. 20–40 минут без телефона.",
        ]
        kb = _body_overload_kb()
    elif mode == "отдых":
        lines = [
            "ТЕЛО", "",
            "Режим: отдых", "",
            "Активный протокол восстановления.", "",
            "Доступно:",
            "1. Закрыть только срочное.",
            "2. Остальное оставить.",
            "3. Сон в приоритет.", "",
            "Быстрый ввод:",
            "\"мне лучше, возвращаюсь к плану\"",
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Лёгкий план", callback_data="overload_light_plan"),
             InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])
    else:
        lines = [
            "ТЕЛО", "",
            f"Режим: {mode}", "",
            f"— проблемных блоков: {len(blocks)}",
            f"— напоминаний: {len(reminders)}", "",
            "Доступно:",
            "1. День отдыха.",
            "2. Лёгкий план.",
            "3. Блок по сну/перегрузу.", "",
            "Быстрый ввод:",
            "\"мне плохо, я не вывожу\"",
            "\"сегодня отдыхаю, ничего не ставь\"",
            "\"не спал, собери лёгкий день\"",
        ]
        if blocks:
            lines += ["", "Активные блоки:"] + [f"— {b.title}" for b in blocks[:2]]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Экстренный отдых", callback_data="overload_rest"),
             InlineKeyboardButton(text="Лёгкий план", callback_data="overload_light_plan")],
            [InlineKeyboardButton(text="Тихий режим", callback_data="quiet_mode")],
        ])

    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot, session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text="\n".join(lines), reply_markup=kb,
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
