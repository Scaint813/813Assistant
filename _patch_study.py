#!/usr/bin/env python3
"""
Patch bot/handlers/menu.py:
1. Add Command("study") decorator to section_study
2. Add try/except with logging
3. Use full weekday names
4. Limit study_blocks to 3
"""
import re

path = r"bot/handlers/menu.py"

with open(path, encoding="utf-8") as f:
    content = f.read()

# ── 1. Add Command("study") decorator ──────────────────────────────────────────
OLD_DEC = '@router.message(F.text == "Учёба")\nasync def section_study('
NEW_DEC = '@router.message(F.text == "Учёба")\n@router.message(Command("study"))\nasync def section_study('

if OLD_DEC in content:
    content = content.replace(OLD_DEC, NEW_DEC, 1)
    print("OK: added Command('study') decorator")
else:
    print("WARN: decorator pattern not found")

# ── 2. Replace DB block with try/except ────────────────────────────────────────
OLD_DB = (
    "    now = time_service.now()\n"
    "    today = now.date()\n"
    "    async with session_factory() as session:\n"
    "        exams = await get_active_exam_dates(session, message.from_user.id)\n"
    "        schedule = await get_active_study_schedule(session, message.from_user.id)\n"
    "        study_blocks = await get_problem_blocks_by_categories(\n"
    '            session, message.from_user.id, ["exam", "study", "learning", "education"], now\n'
    "        )\n"
)
NEW_DB = (
    "    now = time_service.now()\n"
    "    today = now.date()\n"
    "    exams, schedule, study_blocks = [], [], []\n"
    "    try:\n"
    "        async with session_factory() as session:\n"
    "            exams = await get_active_exam_dates(session, message.from_user.id)\n"
    "            schedule = await get_active_study_schedule(session, message.from_user.id)\n"
    "            study_blocks = await get_problem_blocks_by_categories(\n"
    '                session, message.from_user.id, ["exam", "study", "learning", "education"], now\n'
    "            )\n"
    "    except Exception as exc:\n"
    "        import logging as _sl; _sl.getLogger(__name__).exception(\n"
    '            "/study DB error user_id=%s", message.from_user.id)\n'
    '        await message.answer("Учёба временно не открылась. Ошибка записана в лог.")\n'
    "        await screen_service.delete_user_input(message)\n"
    "        return\n"
    "    import logging as _sl2\n"
    '    _sl2.getLogger(__name__).info("/study user_id=%s exams=%d schedule=%d blocks=%d",\n'
    "        message.from_user.id, len(exams), len(schedule), len(study_blocks))\n"
)

if OLD_DB in content:
    content = content.replace(OLD_DB, NEW_DB, 1)
    print("OK: added try/except to DB block")
else:
    print("WARN: DB block not found for replacement")

# ── 3. Replace abbreviated weekday dict with full names ────────────────────────
OLD_WDAY = (
    '        _WDAY = {"mon": "пн", "tue": "вт", "wed": "ср", "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}\n'
    '        lines.append("Занятия:")\n'
    "        for i, s in enumerate(schedule, 1):\n"
    "            wd = _WDAY.get(s.weekday or \"\", s.weekday or \"\")\n"
    "            t = s.time_str or \"\"\n"
    '            tutor = f" ({s.tutor_name})" if s.tutor_name else ""\n'
    '            lines.append(f"{i}. {s.subject} — {wd}, {t}{tutor}")\n'
)
NEW_WDAY = (
    '        _WD = {"mon": "понедельник", "tue": "вторник", "wed": "среда",\n'
    '               "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье"}\n'
    '        lines.append("Занятия:")\n'
    "        for i, s in enumerate(schedule, 1):\n"
    "            wd = _WD.get(s.weekday or \"\", s.weekday or \"\")\n"
    "            t = s.time_str or \"\"\n"
    '            tutor = f", {s.tutor_name}" if s.tutor_name else ""\n'
    '            lines.append(f"{i}. {s.subject} — {wd}, {t}{tutor}")\n'
)
if OLD_WDAY in content:
    content = content.replace(OLD_WDAY, NEW_WDAY, 1)
    print("OK: full weekday names")
else:
    print("WARN: weekday block not found")

# ── 4. Limit study_blocks to 3 ────────────────────────────────────────────────
OLD_BLOCKS = "        for i, b in enumerate(study_blocks, 1):\n"
NEW_BLOCKS = "        for i, b in enumerate(study_blocks[:3], 1):\n"
if OLD_BLOCKS in content:
    content = content.replace(OLD_BLOCKS, NEW_BLOCKS, 1)
    print("OK: limited study_blocks to 3")
else:
    print("WARN: study_blocks enumerate not found")

# ── 5. Add try/except to render_screen block ──────────────────────────────────
OLD_RENDER = (
    "    kb = _study_kb(bool(study_blocks))\n"
    "    async with session_factory() as session:\n"
    "        await screen_service.render_screen(\n"
    "            bot=bot, session=session,\n"
    "            user_id=message.from_user.id,\n"
    "            chat_id=message.chat.id,\n"
    '            text="\\n".join(lines), reply_markup=kb,\n'
    "        )\n"
    "        await session.commit()\n"
    "    await screen_service.delete_user_input(message)\n"
    "\n"
    "\n"
    "\n"
    "@router.message(F.text"  # next handler starts here
)
NEW_RENDER = (
    "    kb = _study_kb(bool(study_blocks))\n"
    "    try:\n"
    "        async with session_factory() as session:\n"
    "            await screen_service.render_screen(\n"
    "                bot=bot, session=session,\n"
    "                user_id=message.from_user.id,\n"
    "                chat_id=message.chat.id,\n"
    '                text="\\n".join(lines), reply_markup=kb,\n'
    "            )\n"
    "            await session.commit()\n"
    "    except Exception as exc:\n"
    "        import logging as _sl3; _sl3.getLogger(__name__).exception(\n"
    '            "/study render error user_id=%s", message.from_user.id)\n'
    '        await message.answer("\\n".join(lines))\n'
    "    await screen_service.delete_user_input(message)\n"
    "\n"
    "\n"
    "\n"
    "@router.message(F.text"
)
if OLD_RENDER in content:
    content = content.replace(OLD_RENDER, NEW_RENDER, 1)
    print("OK: render_screen wrapped in try/except")
else:
    print("WARN: render_screen block not found – patching without context")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("Patch complete.")
