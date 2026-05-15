from __future__ import annotations


def render_preview(parsed: dict) -> str:
    lines = ["Я понял так:", ""]
    intents = parsed.get("intents", [])
    for idx, intent in enumerate(intents, start=1):
        t = intent.get("type")
        if t == "create_task":
            lines.append(f"{idx}. Создать задачу: {intent.get('title') or intent.get('text')}")
        elif t == "create_reminder":
            lines.append(f"{idx}. Напоминание: {intent.get('text')} в {intent.get('remind_at')}")
        elif t in {"schedule_override", "rest_day"}:
            lines.append(f"{idx}. День/режим: {intent.get('mode', 'rest_day')} на {intent.get('override_date')}")
        else:
            lines.append(f"{idx}. Ничего не записывать")
    lines.extend(["", "Подтвердить?"])
    return "\n".join(lines)
