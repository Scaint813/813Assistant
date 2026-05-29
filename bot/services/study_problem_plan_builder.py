from __future__ import annotations

"""
StudyProblemPlanBuilder — строит структурированный учебный план для Miro.

Принципы:
  1. НЕ вставляет raw transcript / длинный problem_text в Miro.
  2. Берёт subject/task_type из block.title и нормализованных полей.
  3. Использует curated templates из study_problem_templates.py.
  4. Возвращает готовые тексты карточек для 6 слотов Miro-кластера.

Функция build_plan(block, exam_dates=None, study_schedule=None) -> dict
"""

import re
from datetime import date as _date, datetime
from typing import Any

from bot.services.study_problem_templates import (
    _extract_subject_key,
    _extract_task_num,
    build_study_problem_plan,
)

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_PROBLEM_SUMMARY_LEN = 180   # max chars for problem_summary shown in Miro
MAX_CARD_CONTENT_LEN = 500      # max chars per Miro card content


# ── Subject-specific "prepare for tutor" hints ────────────────────────────────

_TUTOR_PREPARE: dict[str, list[str]] = {
    "russian": [
        "2 вопроса по слабым темам (задание + правило).",
        "Один разбор ошибки из последней работы.",
        "Ошибки за неделю — выписать и объяснить.",
    ],
    "social": [
        "2 вопроса по слабым темам.",
        "Один спорный план — обсудить структуру.",
        "Ошибки из последнего варианта.",
    ],
    "english": [
        "Essay / writing — готовый абзац или тема.",
        "Ошибки из последнего варианта.",
        "3 вопроса по лексике или грамматике.",
    ],
    "math": [
        "5 задач из слабых блоков — решённых.",
        "Один разбор типовой ошибки.",
        "Вопрос по алгоритму решения задачи.",
    ],
    "physics": [
        "2 задачи из слабой темы.",
        "Вопрос по формуле или закону.",
        "Ошибки из последней работы.",
    ],
    "chemistry": [
        "Вопрос по реакциям из слабой темы.",
        "Ошибки из последней работы.",
        "2 задачи с объяснением решения.",
    ],
    "history": [
        "Ключевые даты и события слабой темы.",
        "Один вопрос по причинно-следственным связям.",
        "Ошибки из последнего теста.",
    ],
    "biology": [
        "Вопрос по слабой теме.",
        "Схема/таблица по изученному.",
        "Ошибки из последнего варианта.",
    ],
}

_GENERIC_TUTOR_PREPARE = [
    "Последние ошибки — выписать и объяснить.",
    "Один вопрос по слабой теме.",
    "Материал для проверки — готовое задание.",
]


# ── Subject-specific exam focus ───────────────────────────────────────────────

_EXAM_FOCUS: dict[str, dict[str, str]] = {
    "russian": {
        "urgent":    "закрыть слабые задания, не распыляться.",
        "high":      "отработать 2–3 слабых задания, повторить правила.",
        "medium":    "стабилизировать базу, добрать баллы на практике.",
        "normal":    "планомерно проходить темы по списку.",
    },
    "social": {
        "urgent":    "планы и теория — сфокусироваться на задании 24.",
        "high":      "отработать планы и эссе, повторить теорию.",
        "medium":    "добрать слабые темы, проверить планы.",
        "normal":    "планомерно проходить блоки по кодификатору.",
    },
    "english": {
        "urgent":    "эссе и устная часть — только практика.",
        "high":      "написать 2 эссе, отработать устную часть.",
        "medium":    "grammar + writing, регулярные занятия.",
        "normal":    "планомерно по умениям: reading, writing, speaking.",
    },
    "math": {
        "urgent":    "стабильная база — не лезть в лишнее.",
        "high":      "добрать стабильные задания, не лезть в лишнее.",
        "medium":    "повторить слабые типы, решить пробный вариант.",
        "normal":    "планомерно по блокам.",
    },
}

_GENERIC_EXAM_FOCUS = {
    "urgent": "сфокусироваться на обязательных темах.",
    "high":   "добрать слабые блоки.",
    "medium": "стабилизировать базу.",
    "normal": "планомерно по плану.",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _days_status(days_left: int) -> str:
    """Return urgency label from days_left."""
    if days_left <= 7:
        return "urgent"
    if days_left <= 14:
        return "high"
    if days_left <= 30:
        return "medium"
    return "normal"


def exam_focus_text(subject_key: str, days_left: int) -> str:
    """Return subject-specific focus text for exam card."""
    status = _days_status(days_left)
    focus_map = _EXAM_FOCUS.get(subject_key, _GENERIC_EXAM_FOCUS)
    return focus_map.get(status, focus_map.get("normal", "планомерно по плану."))


def tutor_prepare_hints(subject_key: str) -> list[str]:
    """Return subject-specific 'what to prepare for tutor' hints."""
    return _TUTOR_PREPARE.get(subject_key, _GENERIC_TUTOR_PREPARE)


def _safe_problem_summary(block) -> str:
    """
    Build a short problem summary (≤ MAX_PROBLEM_SUMMARY_LEN chars) for Miro.

    Rules:
    - NEVER paste raw transcript / long problem_text.
    - Use block.title as primary source (it's already short).
    - If problem_text is short (≤ 120 chars) AND doesn't look like dictation
      → append a trimmed version as "Суть: ..."
    - Otherwise → append "Слабое место: нужна диагностика."
    """
    title = (block.title or "").strip()
    problem_text = (block.problem_text or "").strip()

    # Heuristic: looks like a long dictation transcript
    is_long_transcript = (
        len(problem_text) > 200
        or problem_text.lower().startswith(("задание 2", "в поводу", "у меня", "экзамен", "сегодня"))
        or "..." in problem_text
    )

    if problem_text and not is_long_transcript and len(problem_text) <= 120:
        summary = f"{title}\n\nСуть: {problem_text}"
    else:
        summary = f"{title}\n\nСлабое место:\nнужна диагностика типа ошибки."

    return summary[:MAX_PROBLEM_SUMMARY_LEN]


def _safe_next_action(block, template_next_action: str) -> str:
    """
    Return a safe next_action string for Miro (≤ 120 chars, no raw transcript).

    Priority:
      1. If block.next_action is short and doesn't look like dictation → use it.
      2. Otherwise → use the curated template next_action.
    """
    raw = (block.next_action or "").strip()

    # Heuristics: looks like a long dictation transcript or noisy text
    is_transcript_like = (
        len(raw) > 120
        or raw.lower().startswith(("задание", "у меня", "экзамен", "сегодня", "в поводу", "по поводу"))
        or "..." in raw
        or raw.count(" ") > 20  # many words = likely dictation
    )

    if raw and not is_transcript_like:
        return raw[:120]

    # Fall back to curated template action
    return (template_next_action or "Открыть одно типовое задание и разобрать алгоритм.")[:120]


# ── Main builder ──────────────────────────────────────────────────────────────

def build_plan(
    block,
    exam_dates: list | None = None,
    study_schedule: list | None = None,
) -> dict:
    """
    Build a structured study preparation plan for a Miro cluster.

    Returns dict:
        title         : str          — "Русский язык — задание 21"
        subject       : str          — normalized subject name
        subject_key   : str          — internal key ("russian", "social", ...)
        task_type     : str | None   — "21", "24", "25", or None
        problem_summary: str         — safe short summary (no raw transcript)
        diagnostics   : list[str]    — diagnostic steps
        topics        : list[str]    — theory topics
        practice      : list[str]    — practice tasks
        control       : list[str]    — spaced review schedule
        next_action   : str          — ONE concrete next step
        deadline      : str          — formatted deadline or ""
        miro_cards    : list[dict]   — ready-made card texts for 6 Miro slots
    """
    title = (block.title or "Активный блок").strip()
    problem_text = (block.problem_text or "").strip()
    subject_raw = getattr(block, "subject", "") or ""
    solution_strategy = (block.solution_strategy or "").strip()

    subject_key = _extract_subject_key(subject_raw, title, problem_text)
    task_num = _extract_task_num(title, problem_text)

    # Use curated templates
    try:
        plan = build_study_problem_plan(subject_raw, title, problem_text)
    except Exception:
        plan = {
            "next_action": "Разобрать одно типовое задание и выписать алгоритм.",
            "theory_topics": ["Изучить спецификацию ЕГЭ", "Повторить теорию"],
            "practice_plan": ["Решить 5 заданий", "Разобрать ошибки"],
            "control_plan": ["Через 3 дня: проверить снова"],
            "miro_plan": {},
        }

    # Resolve subject display name
    _SUBJECT_DISPLAY = {
        "russian":   "Русский язык",
        "social":    "Обществознание",
        "english":   "Английский язык",
        "math":      "Математика",
        "physics":   "Физика",
        "chemistry": "Химия",
        "biology":   "Биология",
        "history":   "История",
        "generic":   subject_raw or "Предмет",
    }
    subject_display = _SUBJECT_DISPLAY.get(subject_key, subject_raw or "Предмет")

    task_label = f" — задание {task_num}" if task_num else ""
    full_title = f"{subject_display}{task_label}"

    # Deadline
    deadline_str = ""
    if block.deadline:
        try:
            dl = block.deadline
            if isinstance(dl, datetime):
                dl = dl.date()
            deadline_str = dl.strftime("%d.%m")
        except Exception:
            pass

    # Safe problem summary
    problem_summary = _safe_problem_summary(block)

    topics = plan.get("theory_topics", [])
    practice = plan.get("practice_plan", [])
    control = plan.get("control_plan", [])
    # Safe next_action: never paste raw transcript; fall back to curated template
    next_action = _safe_next_action(block, plan.get("next_action", "Сделать первый шаг."))

    # Diagnostics: specific steps to identify the exact weak point
    diagnostics = _build_diagnostics(subject_key, task_num)

    # ── Build Miro card texts ─────────────────────────────────────────────────
    miro_cards = _build_miro_cards(
        full_title=full_title,
        problem_summary=problem_summary,
        diagnostics=diagnostics,
        topics=topics,
        practice=practice,
        control=control,
        next_action=next_action,
        deadline_str=deadline_str,
    )

    return {
        "title": full_title,
        "subject": subject_display,
        "subject_key": subject_key,
        "task_type": str(task_num) if task_num else None,
        "problem_summary": problem_summary,
        "diagnostics": diagnostics,
        "topics": topics,
        "practice": practice,
        "control": control,
        "next_action": next_action,
        "deadline": deadline_str,
        "miro_cards": miro_cards,
    }


# ── Diagnostics builder ───────────────────────────────────────────────────────

_DIAGNOSTICS: dict[tuple[str, int | None], list[str]] = {
    ("russian", 21): [
        "Открыть 5 заданий 21 из банка ФИПИ.",
        "Выписать каждую ошибку.",
        "Подписать причину: правило / невнимательность / не понял формулировку.",
    ],
    ("russian", 24): [
        "Открыть 3 задания 24.",
        "Обосновать каждый знак препинания.",
        "Проверить по правилу из учебника.",
    ],
    ("russian", 25): [
        "Написать один комментарий по структуре.",
        "Проверить: есть 2 примера, 2 пояснения, связь?",
        "Выписать ошибки по критериям К1–К6.",
    ],
    ("social", 24): [
        "Написать план по одной теме из банка ФИПИ.",
        "Проверить: первый пункт — определение?",
        "Есть ли минимум 3 пункта с подпунктами?",
    ],
    ("english", None): [
        "Написать один параграф эссе.",
        "Проверить: thesis → argument → example → conclusion.",
        "Выписать грамматические ошибки.",
    ],
    ("math", None): [
        "Решить 5 задач из слабого блока без подсказок.",
        "Выписать каждую ошибку: в чём причина?",
        "Определить тип задачи: формула, логика или невнимательность.",
    ],
}

_GENERIC_DIAGNOSTICS = [
    "Открыть 3–5 типовых заданий по этой теме.",
    "Выписать ошибки и их причины.",
    "Определить: теория, алгоритм или невнимательность?",
]


def _build_diagnostics(subject_key: str, task_num: int | None) -> list[str]:
    diag = _DIAGNOSTICS.get((subject_key, task_num))
    if diag is None:
        diag = _DIAGNOSTICS.get((subject_key, None))
    return diag or _GENERIC_DIAGNOSTICS


# ── Miro card content builder ─────────────────────────────────────────────────

def _fmt_list(items: list[str], numbered: bool = True) -> str:
    if not items:
        return "—"
    if numbered:
        return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
    return "\n".join(f"• {item}" for item in items)


def _build_miro_cards(
    full_title: str,
    problem_summary: str,
    diagnostics: list[str],
    topics: list[str],
    practice: list[str],
    control: list[str],
    next_action: str,
    deadline_str: str,
) -> list[dict]:
    """Build list of 6 card dicts for Miro cluster render."""
    deadline_line = f"\nДедлайн: {deadline_str}" if deadline_str else ""

    cards = [
        {
            "slot": "problem",
            "title": "ПРОБЛЕМА",
            "content": f"ПРОБЛЕМА\n{problem_summary}{deadline_line}",
        },
        {
            "slot": "diagnostics",
            "title": "ДИАГНОСТИКА",
            "content": "ДИАГНОСТИКА\n" + _fmt_list(diagnostics),
        },
        {
            "slot": "topics",
            "title": "ТЕМЫ",
            "content": "ТЕМЫ\n" + _fmt_list(topics),
        },
        {
            "slot": "practice",
            "title": "ПРАКТИКА",
            "content": "ПРАКТИКА\n" + _fmt_list(practice),
        },
        {
            "slot": "control",
            "title": "КОНТРОЛЬ",
            "content": "КОНТРОЛЬ\nКритерий закрытия:\n" + _fmt_list(control, numbered=False),
        },
        {
            "slot": "next",
            "title": "СЛЕДУЮЩИЙ ШАГ",
            "content": f"СЛЕДУЮЩИЙ ШАГ\n\nСегодня:\n{next_action}",
        },
    ]

    # Trim all card contents to MAX_CARD_CONTENT_LEN
    for card in cards:
        if len(card["content"]) > MAX_CARD_CONTENT_LEN:
            card["content"] = card["content"][: MAX_CARD_CONTENT_LEN - 3] + "..."

    return cards
