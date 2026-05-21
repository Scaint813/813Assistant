from __future__ import annotations

from datetime import datetime


def render_preview(parsed: dict) -> str:
    """
    Render a structured action preview for ANY combination of intents.
    Supports grouped study packages (exams + schedule + problems in one message).
    """
    intents = parsed.get("intents") or []
    if not intents:
        return "Я понял так:\n\nНичего не зафиксировано.\n\nПодтвердить?"

    # ── Group intents by type ────────────────────────────────────────────────
    exams = [i for i in intents if i.get("type") == "set_exam_date"]
    schedule = [i for i in intents if i.get("type") == "create_study_schedule_item"]
    problems = [i for i in intents if i.get("type") == "create_problem_block"]
    tasks = [i for i in intents if i.get("type") == "create_task"]
    reminders = [i for i in intents if i.get("type") == "create_reminder"]
    rest_days = [i for i in intents if i.get("type") in {"schedule_override", "rest_day"}]

    # ── Study package (exams + schedule + problems) ──────────────────────────
    if exams or (schedule and not tasks) or (problems and _is_study_context(problems)):
        return _render_study_package(exams, schedule, problems, tasks, reminders)

    # ── Single-type previews (legacy, kept for non-study flow) ───────────────
    first = intents[0]
    t = first.get("type")

    if t == "create_reminder":
        when = datetime.fromisoformat(first["remind_at"]).strftime("%Y-%m-%d %H:%M")
        return (
            "Я понял так:\n\n"
            f"1. Создать напоминание:\n{first.get('text', '')}\n"
            f"Время: {when}\n\n"
            "Подтвердить?"
        )

    if t in {"schedule_override", "rest_day"}:
        return (
            "Я понял так:\n\n"
            "1. Поставить день отдыха.\n"
            "2. Не создавать тренировочные задачи.\n"
            "3. Ничего не добавлять в Miro.\n\n"
            "Подтвердить?"
        )

    if t == "create_problem_block":
        return (
            "СИТУАЦИЯ\n"
            f"Проблема: {first.get('problem_text') or first.get('title')}\n\n"
            "ВЫВОД\nНужен активный блок с коротким решением.\n\n"
            "ДЕЙСТВИЕ\n"
            "1. Создать активный блок решения.\n"
            f"2. Следующий шаг: {first.get('next_action')}\n"
            "3. Добавить блок в /next.\n\n"
            "Подтвердить?"
        )

    title = first.get("title") or ""
    return f"Я понял так:\n\n1. Создать задачу:\n{title}\n\nПодтвердить?"


def _is_study_context(problem_blocks: list[dict]) -> bool:
    """Return True if any problem block is exam/study category."""
    return any(p.get("category") in {"exam", "study"} for p in problem_blocks)


_WEEKDAY_RU = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда",
    "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}


def _render_study_package(
    exams: list[dict],
    schedule: list[dict],
    problems: list[dict],
    tasks: list[dict],
    reminders: list[dict],
) -> str:
    lines = ["ПРОВЕРКА\n"]

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
            tutor = f"  ({s['tutor_name']})" if s.get("tutor_name") else ""
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
            lines.append(f"{i}. {p.get('title') or p.get('problem_text') or 'Проблема'}.")
        lines.append("")

    if tasks:
        lines.append("Задачи:")
        for i, task in enumerate(tasks, 1):
            lines.append(f"{i}. {task.get('title') or 'Задача'}.")
        lines.append("")

    if reminders:
        lines.append("Напоминания:")
        for i, r in enumerate(reminders, 1):
            when = ""
            if r.get("remind_at"):
                when = f" — {datetime.fromisoformat(r['remind_at']).strftime('%H:%M')}"
            lines.append(f"{i}. {r.get('text') or 'Напоминание'}{when}.")
        lines.append("")

    lines.append("Действие: сохранить учебный контур подготовки.\n")
    lines.append("Подтвердить?")
    return "\n".join(lines)


def _fmt_date(iso_date: str) -> str:
    """Convert '2026-06-03' → '3 июня'."""
    _MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except Exception:
        return iso_date
