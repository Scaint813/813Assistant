"""
Curated study problem templates for 813Assistant.

For each known (subject, task_type) pair, provides:
- next_action: ONE concrete next step (for Telegram)
- theory_topics: list of topics to review (for Miro)
- practice_plan: list of practice tasks (for Miro)
- control_plan: spaced-repetition review plan (for Miro)
- miro_sections: dict with keys ПРОБЛЕМА/ТЕМЫ/ПРАКТИКА/КОНТРОЛЬ/СЛЕДУЮЩИЙ_ШАГ

Used by ProblemBlockService to enrich problem blocks with deep preparation plans.
"""
from __future__ import annotations

import re

# ── Task number extraction ────────────────────────────────────────────────────

def _extract_task_num(title: str, problem_text: str) -> int | None:
    """Extract a task number like 21, 24, 25 from title or problem_text."""
    for text in (title, problem_text):
        m = re.search(r"задани[ея]\s*(\d+)|(\d+)\s*задани[ея]|зад\.?\s*(\d+)", text.lower())
        if m:
            n = m.group(1) or m.group(2) or m.group(3)
            return int(n)
    return None


def _extract_subject_key(subject: str, title: str, problem_text: str) -> str:
    """Return a normalized subject key for template lookup."""
    combined = (subject + " " + title + " " + problem_text).lower()
    if any(k in combined for k in ("русск", "русский")):
        return "russian"
    if any(k in combined for k in ("обществ", "обществозн")):
        return "social"
    if any(k in combined for k in ("английск", "англ")):
        return "english"
    if any(k in combined for k in ("матем", "математик", "база", "профиль")):
        return "math"
    if any(k in combined for k in ("физик",)):
        return "physics"
    if any(k in combined for k in ("химии", "химия")):
        return "chemistry"
    if any(k in combined for k in ("биологи",)):
        return "biology"
    if any(k in combined for k in ("истори",)):
        return "history"
    return "generic"


# ── Templates ─────────────────────────────────────────────────────────────────

_TEMPLATES: dict[tuple[str, int | None], dict] = {

    # ── Russian language ──────────────────────────────────────────────────────

    ("russian", 21): {
        "next_action": "Открыть 5 заданий 21 из банка ФИПИ и выписать, какое правило нарушено в каждом.",
        "theory_topics": [
            "Пунктуация в сложном предложении (ССП, СПП, БСП)",
            "Обособленные члены предложения (причастные и деепричастные обороты)",
            "Вводные слова и конструкции",
            "Грамматическая основа: подлежащее и сказуемое",
            "Однородные члены и знаки при них",
        ],
        "practice_plan": [
            "10 заданий 21 из банка ФИПИ — подряд без подсказок",
            "Каждую ошибку записать: ошибка → правило → пример",
            "Сделать таблицу «тип запятой → правило → пример»",
            "Повтор через 2 дня: ещё 5 заданий",
            "Мини-тест за неделю до экзамена",
        ],
        "control_plan": [
            "Через 2 дня: 5 заданий по слабым темам",
            "Через 5 дней: 5 новых заданий",
            "За 7 дней до экзамена: мини-тест 10 заданий",
        ],
    },

    ("russian", 24): {
        "next_action": "Найти 3 варианта задания 24 и определить, какие знаки препинания расставлены — по каждому правило.",
        "theory_topics": [
            "Двоеточие: обобщающее слово, прямая речь, бессоюзное предложение",
            "Тире: сказуемое-существительное, обобщающее слово, бессоюзное",
            "Скобки и запятые при вводных словах",
            "Знаки в сложном предложении с разными видами связи",
        ],
        "practice_plan": [
            "5 заданий 24 — выписать обоснование каждого знака",
            "Сравнить объяснение с правилом из учебника",
            "Разобрать разбор одного предложения пошагово",
        ],
        "control_plan": [
            "Через 3 дня: 5 новых заданий 24",
            "За 5 дней до экзамена: 10 заданий подряд",
        ],
    },

    ("russian", 25): {
        "next_action": "Написать один комментарий к проблеме текста по структуре: пример 1 — пояснение — пример 2 — пояснение — связь.",
        "theory_topics": [
            "Структура сочинения: тезис → комментарий → позиция автора → своя позиция → вывод",
            "Комментарий: 2 примера из текста + пояснение + связь между примерами",
            "Позиция автора: не пересказ, а отношение",
            "Критерии К1–К6 в деталях",
            "Типичные ошибки: пересказ вместо примера, нет пояснения, нет связи",
        ],
        "practice_plan": [
            "Написать комментарий к одному тексту по шаблону",
            "Проверить по критериям: есть 2 примера, 2 пояснения, связь?",
            "Написать позицию автора (2-3 предложения)",
            "Написать полное сочинение на один текст",
            "Разобрать типичные ошибки комментария",
        ],
        "control_plan": [
            "Через 3 дня: написать комментарий к новому тексту",
            "Через неделю: полное сочинение + самопроверка",
        ],
    },

    # ── Обществознание ────────────────────────────────────────────────────────

    ("social", 24): {
        "next_action": "Написать план по одной теме из банка ФИПИ: пункт 1 — определение, пункты 2–4 — раскрывающие аспекты с подпунктами.",
        "theory_topics": [
            "Структура сложного плана: тема → понятие → 3-5 пунктов → подпункты",
            "Первый пункт: всегда определение/понятие",
            "Подпункты должны конкретно раскрывать пункт (не менее 3 в двух пунктах)",
            "Стиль Котовой/Лисковой: академические формулировки",
            "Типичные ошибки: слишком общие пункты, нет подпунктов, нарушена логика",
        ],
        "practice_plan": [
            "Написать 3 плана по разным темам (государство, право, экономика)",
            "Проверить каждый по критериям: есть ли 3 пункта с подпунктами?",
            "Сравнить с эталонными планами из банка ФИПИ",
            "Отработать 2 самые сложные темы",
        ],
        "control_plan": [
            "Через 2 дня: написать план по новой теме",
            "За 5 дней до экзамена: 3 плана подряд по таймеру 30 мин",
        ],
    },

    # ── Английский ───────────────────────────────────────────────────────────

    ("english", None): {
        "next_action": "Написать один параграф эссе по структуре: thesis → argument → example → conclusion.",
        "theory_topics": [
            "Эссе: структура (intro, 2 body paragraphs, conclusion)",
            "Устная часть: монолог, диалог, описание фото",
            "Грамматика: времена, модальные глаголы, условные предложения",
            "Лексика: academic vocabulary, connectors",
            "Критерии оценки письменной и устной части",
        ],
        "practice_plan": [
            "Написать одно эссе по готовой теме",
            "Проверить: thesis, 2 аргумента, примеры, вывод",
            "Записать устный монолог по фото (2 мин)",
            "Разобрать типичные грамматические ошибки",
        ],
        "control_plan": [
            "Через 3 дня: новое эссе по другой теме",
            "За неделю до экзамена: симуляция устной части",
        ],
    },

    # ── Базовая математика ────────────────────────────────────────────────────

    ("math", None): {
        "next_action": "Решить 5 задач из задания 1-5 варианта ЕГЭ базовой математики без калькулятора.",
        "theory_topics": [
            "Арифметика: проценты, дроби, степени",
            "Уравнения и неравенства",
            "Геометрия: площади, объёмы, теорема Пифагора",
            "Статистика и теория вероятностей (задания 10-12)",
            "Текстовые задачи на движение, работу, смеси",
        ],
        "practice_plan": [
            "5 заданий из блока 1-10 — без подсказок",
            "Разобрать каждую ошибку: в чём причина?",
            "Сделать карточки: тип задачи → формула → пример",
            "Решить полный вариант по таймеру (3 часа)",
        ],
        "control_plan": [
            "Через 2 дня: 10 новых задач из слабых блоков",
            "За неделю: полный пробный вариант",
        ],
    },

}

# Generic fallback
_GENERIC_TEMPLATE: dict = {
    "next_action": "Разобрать одно типовое задание по этой теме и выписать алгоритм решения.",
    "theory_topics": [
        "Изучить точную формулировку задания в спецификации ЕГЭ",
        "Повторить теоретическую базу по теме",
        "Найти критерии оценки",
    ],
    "practice_plan": [
        "Решить 5 типовых заданий",
        "Разобрать ошибки",
        "Повтор через 2 дня",
    ],
    "control_plan": [
        "Через 3 дня: проверить снова",
        "За неделю до экзамена: мини-тест",
    ],
}


def build_study_problem_plan(subject: str, title: str, problem_text: str) -> dict:
    """
    Build a structured study preparation plan for a problem block.

    Returns:
        dict with keys:
            next_action: str           — one concrete next step (for Telegram short message)
            theory_topics: list[str]   — topics to review
            practice_plan: list[str]   — practice tasks
            control_plan: list[str]    — spaced review schedule
            miro_plan: dict            — ready-made sections for Miro render
    """
    subject_key = _extract_subject_key(subject, title, problem_text)
    task_num = _extract_task_num(title, problem_text)

    # Try exact (subject_key, task_num) match
    tmpl = _TEMPLATES.get((subject_key, task_num))

    # If no exact task_num match, try (subject_key, None) for generic subject
    if tmpl is None:
        tmpl = _TEMPLATES.get((subject_key, None))

    # Final fallback
    if tmpl is None:
        tmpl = _GENERIC_TEMPLATE

    theory_text = "\n".join(f"{i}. {t}" for i, t in enumerate(tmpl["theory_topics"], 1))
    practice_text = "\n".join(f"{i}. {p}" for i, p in enumerate(tmpl["practice_plan"], 1))
    control_text = "\n".join(f"• {c}" for c in tmpl["control_plan"])

    subject_label = subject or "Предмет"
    task_label = f" задание {task_num}" if task_num else ""
    header = f"{subject_label}{task_label}"

    miro_plan = {
        "header": header,
        "problem": problem_text[:200] if problem_text else title,
        "theory": theory_text,
        "practice": practice_text,
        "control": control_text,
        "next_action": tmpl["next_action"],
    }

    return {
        "next_action": tmpl["next_action"],
        "theory_topics": tmpl["theory_topics"],
        "practice_plan": tmpl["practice_plan"],
        "control_plan": tmpl["control_plan"],
        "miro_plan": miro_plan,
    }
