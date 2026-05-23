#!/usr/bin/env python3
"""Overwrite action_preview.py with full updated version."""

CONTENT = '''from __future__ import annotations

from datetime import datetime


def render_preview(parsed: dict) -> str:
    """
    Render a structured action preview for ANY combination of intents.
    Supports grouped study packages (exams + schedule + problems in one message).
    Handles update/delete exam and schedule intents.
    """
    intents = parsed.get("intents") or []
    if not intents:
        return "Я понял так:\\n\\nНичего не зафиксировано.\\n\\nПодтвердить?"

    # Group intents by type
    exams          = [i for i in intents if i.get("type") == "set_exam_date"]
    schedule       = [i for i in intents if i.get("type") == "create_study_schedule_item"]
    problems       = [i for i in intents if i.get("type") == "create_problem_block"]
    tasks          = [i for i in intents if i.get("type") == "create_task"]
    reminders      = [i for i in intents if i.get("type") == "create_reminder"]
    update_exams   = [i for i in intents if i.get("type") == "update_exam_date"]
    delete_exams   = [i for i in intents if i.get("type") == "delete_exam_date"]
    delete_sched   = [i for i in intents if i.get("type") == "delete_study_schedule_item"]

    # Edit operations take priority
    if update_exams or delete_exams or delete_sched:
        return _render_edit_package(update_exams, delete_exams, delete_sched, exams, problems)

    # Study create package
    if exams or (schedule and not tasks) or (problems and _is_study_context(problems)):
        return _render_study_package(exams, schedule, problems, tasks, reminders)

    # Single-intent previews
    first = intents[0]
    t = first.get("type")

    if t == "create_reminder":
        when = datetime.fromisoformat(first["remind_at"]).strftime("%Y-%m-%d %H:%M")
        return (
            "Я понял так:\\n\\n"
            f"1. Создать напоминание:\\n{first.get(\'text\', \'\')}\\n"
            f"Время: {when}\\n\\n"
            "Подтвердить?"
        )

    if t in {"schedule_override", "rest_day"}:
        return (
            "Я понял так:\\n\\n"
            "1. Поставить день отдыха.\\n"
            "2. Не создавать тренировочные задачи.\\n"
            "3. Ничего не добавлять в Miro.\\n\\n"
            "Подтвердить?"
        )

    if t == "create_problem_block":
        return (
            "СИТУАЦИЯ\\n"
            f"Проблема: {first.get(\'problem_text\') or first.get(\'title\')}\\n\\n"
            "ВЫВОД\\nНужен активный блок с коротким решением.\\n\\n"
            "ДЕЙСТВИЕ\\n"
            "1. Создать активный блок решения.\\n"
            f"2. Следующий шаг: {first.get(\'next_action\')}\\n"
            "3. Добавить блок в /next.\\n\\n"
            "Подтвердить?"
        )

    title = first.get("title") or ""
    return f"Я понял так:\\n\\n1. Создать задачу:\\n{title}\\n\\nПодтвердить?"


def _is_study_context(problem_blocks: list) -> bool:
    return any(p.get("category") in {"exam", "study"} for p in problem_blocks)


_WEEKDAY_RU = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда",
    "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}


def _render_edit_package(
    update_exams, delete_exams, delete_sched, also_create_exams, also_create_problems,
) -> str:
    lines = ["ПРОВЕРКА\\n"]

    if update_exams:
        lines.append("Обновить даты экзаменов:")
        for i, e in enumerate(update_exams, 1):
            subject = e.get("target_subject") or e.get("subject") or "Предмет"
            new_date = _fmt_date(e.get("new_date") or e.get("exam_date") or "")
            lines.append(f"{i}. {subject} → {new_date}")
        lines.append("")

    if delete_exams:
        lines.append("Убрать лишние экзамены:")
        for i, e in enumerate(delete_exams, 1):
            subject = e.get("target_subject") or e.get("subject") or "Предмет"
            lines.append(f"{i}. {subject}")
        lines.append("")

    if delete_sched:
        lines.append("Убрать занятия:")
        for i, s in enumerate(delete_sched, 1):
            subject = s.get("target_subject") or s.get("subject") or "Предмет"
            wd = _WEEKDAY_RU.get(s.get("weekday") or "", "")
            lines.append(f"{i}. {subject}" + (f" ({wd})" if wd else ""))
        lines.append("")

    if also_create_exams:
        lines.append("Также добавить:")
        for e in also_create_exams:
            lines.append(f"• {e.get(\'subject\')} — {_fmt_date(e.get(\'exam_date\') or \'\')}")
        lines.append("")

    if also_create_problems:
        for p in also_create_problems:
            lines.append(f"Проблема: {p.get(\'title\') or p.get(\'problem_text\')}")
            lines.append(f"Следующий шаг: {p.get(\'next_action\') or \'—\'}")
        lines.append("")

    lines.append("Подтвердить?")
    return "\\n".join(lines)


def _render_study_package(
    exams, schedule, problems, tasks, reminders,
) -> str:
    lines = ["ПРОВЕРКА\\n"]

    if exams:
        lines.append("Экзамены:")
        for i, e in enumerate(exams, 1):
            subject = e.get("subject") or "Предмет"
            exam_date = _fmt_date(e.get("exam_date") or "")
            lines.append(f"{i}. {subject} — {exam_date}.")
        lines.append("")

    if schedule:
        lines.append("Занятия:")
        for i, s in enumerate(schedule, 1):
            subject = s.get("subject") or "Предмет"
            wd = _WEEKDAY_RU.get(s.get("weekday") or "mon", s.get("weekday") or "")
            t = s.get("time_str") or ""
            tutor = f"  ({s[\'tutor_name\']})" if s.get("tutor_name") else ""
            lines.append(f"{i}. {subject} — {wd}, {t}.{tutor}")
        lines.append("")

    study_problems = [p for p in problems if p.get("category") in {"exam", "study"}]
    other_problems = [p for p in problems if p.get("category") not in {"exam", "study"}]

    if study_problems:
        lines.append("Учебные проблемы:")
        for i, p in enumerate(study_problems, 1):
            title = p.get("title") or p.get("problem_text") or "Проблема"
            next_action = p.get("next_action") or "—"
            lines.append(f"{i}. {title}.")
            lines.append(f"   Следующий шаг: {next_action}")
        lines.append("")

    if other_problems:
        lines.append("Блоки:")
        for i, p in enumerate(other_problems, 1):
            lines.append(f"{i}. {p.get(\'title\') or p.get(\'problem_text\') or \'Проблема\'}.")
        lines.append("")

    if tasks:
        lines.append("Задачи:")
        for i, task in enumerate(tasks, 1):
            lines.append(f"{i}. {task.get(\'title\') or \'Задача\'}.")
        lines.append("")

    if reminders:
        lines.append("Напоминания:")
        for i, r in enumerate(reminders, 1):
            when = ""
            if r.get("remind_at"):
                when = f" — {datetime.fromisoformat(r[\'remind_at\']).strftime(\'%H:%M\')}"
            lines.append(f"{i}. {r.get(\'text\') or \'Напоминание\'}{when}.")
        lines.append("")

    lines.append("Действие: сохранить учебный контур подготовки.\\n")
    lines.append("Подтвердить?")
    return "\\n".join(lines)


def _fmt_date(iso_date: str) -> str:
    _MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except Exception:
        return iso_date
'''

path = "bot/services/action_preview.py"
with open(path, "w", encoding="utf-8") as f:
    f.write(CONTENT)
print("OK: action_preview.py rewritten")
