from __future__ import annotations

import re

_GENERIC_LABELS = {
    "",
    "дело",
    "задача",
    "задачу",
    "новая задача",
    "тест",
    "тестовая задача",
    "task",
    "new task",
    "test",
    "test task",
    "напоминание",
    "reminder",
    "активный блок",
    "проблема",
    "problem",
}


def normalize_label(value: str | None) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return text.strip(" \t\n—–-:;,.!?[](){}\"")


def is_meaningful_label(value: str | None) -> bool:
    normalized = normalize_label(value)
    if normalized in _GENERIC_LABELS:
        return False
    if normalized.startswith(("debug", "[debug")):
        return False
    words = re.findall(r"[a-zа-яё0-9]+", normalized, flags=re.IGNORECASE)
    return len("".join(words)) >= 4 and any(word not in _GENERIC_LABELS for word in words)


def is_meaningful_task(task) -> bool:
    return is_meaningful_label(getattr(task, "title", ""))


def task_action(task) -> str:
    title = (getattr(task, "title", "") or "").strip()
    action = (getattr(task, "next_action", "") or "").strip()
    if is_meaningful_label(action) and normalize_label(action) != normalize_label(title):
        return action
    return title if is_meaningful_label(title) else ""


def is_meaningful_problem(block) -> bool:
    return is_meaningful_label(getattr(block, "title", "")) and is_meaningful_label(
        getattr(block, "next_action", "")
    )


def task_title_from_request(text: str | None) -> str:
    """Keep the action itself while moving planning details into their fields."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = re.sub(
        r"^\s*(?:пожалуйста[, ]*)?(?:(?:добавь|создай|поставь|запиши|зафиксируй)\s+)?"
        r"(?:мне\s+)?(?:новую\s+)?задач(?:у|а)?\b\s*[:—–-]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*(?:надо|нужно)\s*[:—–-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:сегодня|завтра|послезавтра)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bна\s+\d{1,3}\s*(?:мин(?:ут[уы]?)?|мин\.?|час(?:а|ов)?|ч\.?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:до|к)\s+(?:понедельник(?:а|у)?|вторник(?:а|у)?|сред[еы]|"
        r"четверг(?:а|у)?|пятниц[еы]|суббот[еы]|воскресень(?:я|ю))\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" \t\n—–-:;,.!")[:255]
