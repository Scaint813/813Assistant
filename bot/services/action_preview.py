from __future__ import annotations

from datetime import datetime


def render_preview(parsed: dict) -> str:
    """
    Render a structured action preview for ANY combination of intents.
    Supports grouped study packages (exams + schedule + problems in one message).
    Handles update/delete exam and schedule intents.
    """
    intents = parsed.get("intents") or []
    if not intents:
        return "Я понял так:\n\nНичего не зафиксировано.\n\nПодтвердить?"

    # Group intents by type
    exams          = [i for i in intents if i.get("type") == "set_exam_date"]
    schedule       = [i for i in intents if i.get("type") == "create_study_schedule_item"]
    problems       = [i for i in intents if i.get("type") == "create_problem_block"]
    tasks          = [i for i in intents if i.get("type") == "create_task"]
    reminders      = [i for i in intents if i.get("type") == "create_reminder"]
    plans          = [i for i in intents if i.get("type") == "decompose_project"]
    update_exams   = [i for i in intents if i.get("type") == "update_exam_date"]
    delete_exams   = [i for i in intents if i.get("type") == "delete_exam_date"]
    delete_sched   = [i for i in intents if i.get("type") == "delete_study_schedule_item"]
    task_changes   = [i for i in intents if i.get("type") in {"update_task", "complete_task", "archive_task"}]
    reminder_changes = [i for i in intents if i.get("type") in {"update_reminder", "cancel_reminder"}]

    if task_changes or reminder_changes:
        return _render_natural_changes(task_changes, reminder_changes)

    # Edit operations take priority
    if update_exams or delete_exams or delete_sched:
        return _render_edit_package(update_exams, delete_exams, delete_sched, exams, problems)

    if plans:
        plan = plans[0]
        lines = [
            "Проверка плана",
            "",
            f"Проект: {plan.get('project') or 'Без названия'}",
            f"Результат: {plan.get('objective') or '—'}",
            "",
            "Шаги:",
        ]
        for index, step in enumerate(plan.get("steps") or [], 1):
            lines.append(f"{index}. {step.get('title')}")
            action = step.get("next_action") or step.get("title")
            if action and action.casefold() != (step.get("title") or "").casefold():
                lines.append(f"   Начать с: {action}")
            if step.get("dependencies"):
                lines.append(f"   Зависит от: {', '.join(step['dependencies'])}")
        lines += ["", "Сохранить эти задачи?"]
        return "\n".join(lines)

    general_actions = tasks + reminders
    if len(general_actions) > 1:
        return _render_action_batch(tasks, reminders)

    # Study create package
    if exams or (schedule and not tasks) or (problems and _is_study_context(problems)):
        return _render_study_package(exams, schedule, problems, tasks, reminders)

    # Single-intent previews
    first = intents[0]
    t = first.get("type")

    if t == "create_reminder":
        remind_at = datetime.fromisoformat(first["remind_at"])
        when = remind_at.strftime("%d.%m.%Y %H:%M")
        timezone = first.get("timezone") or _offset_label(remind_at)
        recurrence = {
            "daily": "каждый день",
            "weekdays": "по будням",
            "weekly": "раз в неделю",
            "monthly": "раз в месяц",
        }.get(first.get("recurrence"), "нет")
        return (
            "Проверка напоминания\n\n"
            f"Что: {first.get('text', '')}\n"
            f"Когда: {when}\n"
            f"Часовой пояс: {timezone}\n"
            f"Повтор: {recurrence}\n\n"
            "Сохранить?"
        )

    if t in {"schedule_override", "rest_day"}:
        return (
            "Проверка режима дня\n\n"
            "Режим: день отдыха.\n"
            "Тренировки не планировать.\n"
            "Оставить только действительно срочные дела.\n\n"
            "Сохранить?"
        )

    if t == "create_problem_block":
        return (
            "Проверка проблемы\n\n"
            f"Что мешает: {first.get('problem_text') or first.get('title')}\n"
            f"Первый конкретный шаг: {first.get('next_action') or 'уточнить вместе'}\n\n"
            "Сохранить как активную проблему?"
        )

    title = first.get("title") or ""
    lines = ["Сохранить задачу?", "", title]
    action = (first.get("next_action") or "").strip()
    if action and action.casefold() != title.casefold():
        lines += ["", f"Начать с: {action}"]
    if first.get("deadline"):
        try:
            deadline = datetime.fromisoformat(first["deadline"]).strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            deadline = str(first["deadline"])
        lines.append(f"Срок: {deadline}")
    if first.get("project"):
        lines.append(f"Проект: {first['project']}")
    if first.get("planning_state") == "inbox":
        missing = []
        if not first.get("duration_confirmed"):
            missing.append("длительность")
        if not first.get("deadline_confirmed"):
            missing.append("срок")
        lines += [
            "",
            "После сохранения: Входящие.",
            f"Для плана нужно уточнить: {', '.join(missing)}.",
        ]
    else:
        lines += ["", f"Оценка времени: {first.get('estimated_minutes', 30)} мин."]
    return "\n".join(lines)


def _render_action_batch(tasks: list[dict], reminders: list[dict]) -> str:
    lines = [f"Проверка · {len(tasks) + len(reminders)} действия", ""]
    if tasks:
        lines.append("Задачи:")
        for index, task in enumerate(tasks, 1):
            suffixes = []
            if task.get("deadline"):
                try:
                    suffixes.append(datetime.fromisoformat(task["deadline"]).strftime("до %d.%m %H:%M"))
                except (TypeError, ValueError):
                    pass
            if task.get("duration_confirmed"):
                suffixes.append(f"{task.get('estimated_minutes')} мин")
            suffix = f" · {' · '.join(suffixes)}" if suffixes else " · во Входящие"
            lines.append(f"{index}. {task.get('title') or 'Без названия'}{suffix}")
        lines.append("")
    if reminders:
        lines.append("Напоминания:")
        for index, reminder in enumerate(reminders, 1):
            remind_at = datetime.fromisoformat(reminder["remind_at"])
            when = remind_at.strftime("%d.%m в %H:%M")
            timezone = reminder.get("timezone") or _offset_label(remind_at)
            lines.append(
                f"{index}. {reminder.get('text') or 'Без текста'} · {when} · {timezone}"
            )
        lines.append("")
    lines.append("Сохранить всё одним действием?")
    return "\n".join(lines)


def _render_natural_changes(task_changes: list[dict], reminder_changes: list[dict]) -> str:
    lines = ["Проверка изменений", ""]
    for item in task_changes:
        title = item.get("current_title") or item.get("target_title") or "задача"
        operation = item.get("type")
        if operation == "complete_task":
            lines.append(f"Завершить задачу: {title}")
        elif operation == "archive_task":
            lines.append(f"Убрать задачу в архив: {title}")
        else:
            lines.append(f"Изменить задачу: {title}")
            if item.get("new_title"):
                lines.append(f"Новое название: {item['new_title']}")
            if item.get("deadline"):
                lines.append(f"Новый срок: {datetime.fromisoformat(item['deadline']).strftime('%d.%m.%Y %H:%M')}")
            if item.get("duration_confirmed"):
                lines.append(f"Длительность: {item['estimated_minutes']} мин")
            if item.get("next_action"):
                lines.append(f"Начать с: {item['next_action']}")
    for item in reminder_changes:
        text = item.get("current_text") or item.get("target_text") or "напоминание"
        if item.get("type") == "cancel_reminder":
            lines.append(f"Отменить напоминание: {text}")
        else:
            lines.append(f"Изменить напоминание: {text}")
            if item.get("new_text"):
                lines.append(f"Новый текст: {item['new_text']}")
            if item.get("remind_at"):
                remind_at = datetime.fromisoformat(item["remind_at"])
                timezone = item.get("timezone") or _offset_label(remind_at)
                lines.append(
                    f"Новое время: {remind_at.strftime('%d.%m.%Y %H:%M')} · {timezone}"
                )
    lines += ["", "Применить?"]
    return "\n".join(lines)


def _is_study_context(problem_blocks: list) -> bool:
    return any(p.get("category") in {"exam", "study"} for p in problem_blocks)


def _offset_label(value: datetime) -> str:
    offset = value.strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"


_WEEKDAY_RU = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда",
    "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}


def _render_edit_package(
    update_exams, delete_exams, delete_sched, also_create_exams, also_create_problems,
) -> str:
    lines = ["ПРОВЕРКА\n"]

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
            lines.append(f"• {e.get('subject')} — {_fmt_date(e.get('exam_date') or '')}")
        lines.append("")

    if also_create_problems:
        for p in also_create_problems:
            lines.append(f"Проблема: {p.get('title') or p.get('problem_text')}")
            lines.append(f"Следующий шаг: {p.get('next_action') or '—'}")
        lines.append("")

    lines.append("Подтвердить?")
    return "\n".join(lines)


def _render_study_package(
    exams, schedule, problems, tasks, reminders,
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
    _MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except Exception:
        return iso_date
