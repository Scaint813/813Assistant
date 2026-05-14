from __future__ import annotations


def render_preview(parsed: dict) -> str:
    lines = ["Я понял так:", ""]
    for idx, intent in enumerate(parsed.get("intents", []), start=1):
        itype = intent.get("type")
        if itype == "schedule_override":
            lines.append(f"{idx}. День отдыха на {intent.get('date')} (без задач/MIRO).")
        elif itype == "create_reminder":
            lines.append(f"{idx}. Напоминание: {intent.get('date')} {intent.get('time')} — {intent.get('text')}.")
        else:
            lines.append(f"{idx}. {itype}")
    return "\n".join(lines)
