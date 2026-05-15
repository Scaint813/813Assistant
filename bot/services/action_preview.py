from __future__ import annotations


def render_preview(parsed: dict) -> str:
    title = ""
    for intent in parsed.get("intents", []):
        if intent.get("type") == "create_task":
            title = intent.get("title") or ""
            break
    return f"Я понял так:\n\n1. Создать задачу:\n{title}\n\nПодтвердить?"
