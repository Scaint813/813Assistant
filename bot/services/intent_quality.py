from __future__ import annotations

import re

SAFE_INTENT_CLARIFICATION = (
    "Не уверен, что ты хочешь: сохранить дело, поставить напоминание "
    "или получить информацию. Уточни одним предложением — ничего не сохранено."
)

_CONVERSATIONAL_EXACT = {
    "да",
    "нет",
    "ок",
    "окей",
    "понял",
    "понятно",
    "спасибо",
    "привет",
    "здравствуй",
    "доброе утро",
    "добрый день",
    "добрый вечер",
    "давай",
    "хорошо",
    "плохо",
    "бред",
}

_QUESTION_STARTS = (
    "что ",
    "как ",
    "почему ",
    "зачем ",
    "когда ",
    "где ",
    "кто ",
    "какой ",
    "какая ",
    "какие ",
    "можно ли ",
    "умеешь ли ",
)

_TASK_MARKERS = (
    "добавь",
    "создай",
    "запиши",
    "задача",
    "дело",
    "нужно",
    "надо",
    "необходимо",
    "требуется",
    "не забыть",
    "мне надо",
    "мне нужно",
)

_IMPERATIVE_VERBS = {
    "купи",
    "позвони",
    "отправь",
    "подготовь",
    "сходи",
    "проверь",
    "забери",
    "закажи",
    "оплати",
    "прочитай",
    "напиши",
    "собери",
    "закончи",
    "начни",
    "доделай",
    "разбери",
    "выучи",
    "повтори",
    "помой",
    "убери",
}

_HIGH_RISK_INTENTS = {
    "archive_task",
    "cancel_reminder",
    "archive_problem_block",
    "delete_exam_date",
    "delete_study_schedule_item",
}

_TIME_SENSITIVE_INTENTS = {
    "create_reminder",
    "update_reminder",
    "schedule_override",
    "rest_day",
}


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", value.casefold()))


def is_assistant_feedback(value: str) -> bool:
    text = normalized_text(value)
    subject = any(word in text.split() for word in ("бот", "ассистент", "ты"))
    complaint = any(
        phrase in text
        for phrase in (
            "что за бред",
            "не работает",
            "не понял",
            "не поняла",
            "понял неправильно",
            "зачем это",
            "плохой ответ",
            "не то",
        )
    )
    return subject and complaint


def looks_like_task_request(value: str) -> bool:
    """Return true only when mutating task data is a defensible interpretation."""
    text = normalized_text(value)
    if not text or text in _CONVERSATIONAL_EXACT:
        return False
    if is_assistant_feedback(value) or "?" in value:
        return False
    if text.startswith(_QUESTION_STARTS):
        return False
    if any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in _TASK_MARKERS):
        return True

    first = text.split()[0]
    if first in _IMPERATIVE_VERBS:
        return True
    # Russian infinitive task labels: «купить лекарства», «сходить к врачу».
    return len(first) >= 5 and first.endswith(("ть", "ться", "ти", "чь"))


def confirmation_risk(intents: list[dict]) -> str:
    """Queries bypass previews; mutations use an action-specific confirmation level."""
    types = {str(item.get("type") or "") for item in intents}
    if types & _HIGH_RISK_INTENTS:
        return "high"
    if types & _TIME_SENSITIVE_INTENTS or len(intents) > 1:
        return "medium"
    return "low"


def confirmation_label(intents: list[dict]) -> str:
    types = {str(item.get("type") or "") for item in intents}
    risk = confirmation_risk(intents)
    if risk == "high":
        return "Подтвердить изменение"
    if types == {"create_reminder"}:
        return "Поставить напоминание"
    if types == {"create_task"}:
        return "Добавить задачу"
    return "Сохранить действия" if len(intents) > 1 else "Сохранить"
