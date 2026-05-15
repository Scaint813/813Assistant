from __future__ import annotations

from datetime import datetime


def render_preview(parsed: dict) -> str:

    intent = (parsed.get("intents") or [{}])[0]
    t = intent.get("type")

    if t == "create_reminder":
        when = datetime.fromisoformat(intent["remind_at"]).strftime("%Y-%m-%d %H:%M")
        return f"Я понял так:\n\n1. Создать напоминание:\n{intent.get('text', '')}\nВремя: {when}\n\nПодтвердить?"

    if t in {"schedule_override", "rest_day"}:
        return "Я понял так:\n\n1. Поставить день отдыха.\n2. Не создавать тренировочные задачи.\n3. Ничего не добавлять в Miro.\n\nПодтвердить?"

    title = intent.get("title") or ""

    title = ""
    for intent in parsed.get("intents", []):
        if intent.get("type") == "create_task":
            title = intent.get("title") or ""
            break
    return f"Я понял так:\n\n1. Создать задачу:\n{title}\n\nПодтвердить?"
