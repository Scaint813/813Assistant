#!/usr/bin/env python3
"""
Patch ai_service.py:
1. Add update_exam_date, delete_exam_date, delete_study_schedule_item to SYSTEM_PROMPT and Intent Literal
2. Add target_subject, new_date fields to Intent
3. Improve _SUBJECT_NORM: base/profile math, full Russian name, письменный/устный английский
4. Add update/delete detection to fallback parser
"""

path = "bot/services/ai_service.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# ── 1. Extend SYSTEM_PROMPT with new intent types ──────────────────────────
OLD_PROMPT_INTENTS = """\
  set_exam_date
  create_study_schedule_item

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:"""

NEW_PROMPT_INTENTS = """\
  set_exam_date
  create_study_schedule_item
  update_exam_date
  delete_exam_date
  delete_study_schedule_item

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:"""

if OLD_PROMPT_INTENTS in content:
    content = content.replace(OLD_PROMPT_INTENTS, NEW_PROMPT_INTENTS, 1)
    print("OK: added new intents to SYSTEM_PROMPT list")
else:
    print("WARN: SYSTEM_PROMPT intents block not found")

# ── 2. Add update/delete rules to SYSTEM_PROMPT ────────────────────────────
OLD_RULE_6 = "6. Сегодня: {TODAY}. Используй для вычисления дат."
NEW_RULE_6 = """\
6. Сегодня: {TODAY}. Используй для вычисления дат.

7. update_exam_date — когда пользователь хочет изменить дату существующего экзамена.
   Пример: "поставь базовую математику 8 июня" → update_exam_date {target_subject: "Базовая математика", new_date: "2026-06-08"}
   Пример: "перенеси русский на 1 июня" → update_exam_date {target_subject: "Русский язык", new_date: "2026-06-01"}
   Пример: "математика 8 июня" (если уже есть в системе) → update_exam_date (не set_exam_date!)

8. delete_exam_date — когда пользователь хочет убрать/удалить экзамен.
   Пример: "убери экзамен математика" → delete_exam_date {target_subject: "Математика"}
   Пример: "удали дубль математика" → delete_exam_date {target_subject: "Математика"}

9. delete_study_schedule_item — когда пользователь хочет убрать занятие.
   Пример: "убери занятие по английскому в воскресенье" → delete_study_schedule_item {target_subject: "Английский", weekday: "sun"}"""

if OLD_RULE_6 in content:
    content = content.replace(OLD_RULE_6, NEW_RULE_6, 1)
    print("OK: added rules 7-9 to SYSTEM_PROMPT")
else:
    print("WARN: rule 6 pattern not found")

# ── 3. Extend Intent Literal type ─────────────────────────────────────────
OLD_LITERAL = '''\
        "get_problem_resources\", \"set_exam_date\", \"create_study_schedule_item\",
    ]'''

NEW_LITERAL = '''\
        "get_problem_resources\", \"set_exam_date\", \"create_study_schedule_item\",
        "update_exam_date\", \"delete_exam_date\", \"delete_study_schedule_item\",
    ]'''

if OLD_LITERAL in content:
    content = content.replace(OLD_LITERAL, NEW_LITERAL, 1)
    print("OK: extended Intent Literal with new types")
else:
    # Try alternative: the literal might be on one line
    old2 = '"get_problem_resources", "set_exam_date", "create_study_schedule_item",'
    new2 = '"get_problem_resources", "set_exam_date", "create_study_schedule_item",\n        "update_exam_date", "delete_exam_date", "delete_study_schedule_item",'
    if old2 in content:
        content = content.replace(old2, new2, 1)
        print("OK: extended Intent Literal (alt match)")
    else:
        print("WARN: Intent Literal not found")

# ── 4. Add target_subject, new_date fields to Intent model ────────────────
OLD_INTENT_FIELDS = "    location_or_link: str | None = None"
NEW_INTENT_FIELDS = """\
    location_or_link: str | None = None
    # update/delete fields
    target_subject: str | None = None   # for update_exam_date / delete_exam_date
    new_date: str | None = None         # for update_exam_date"""

if OLD_INTENT_FIELDS in content:
    content = content.replace(OLD_INTENT_FIELDS, NEW_INTENT_FIELDS, 1)
    print("OK: added target_subject, new_date fields to Intent")
else:
    print("WARN: Intent fields endpoint not found")

# ── 5. Improve _SUBJECT_NORM ──────────────────────────────────────────────
OLD_NORM = '''\
    _SUBJECT_NORM = {
        "русск": "Русский",
        "общество": "Обществознание",
        "обществозн": "Обществознание",
        "математик": "Математика",
        "английск": "Английский",
        "истори": "История",
        "биологи": "Биология",
        "химии": "Химия",
        "химия": "Химия",
        "физик": "Физика",
        "информатик": "Информатика",
        "географи": "География",
    }'''

NEW_NORM = '''\
    _SUBJECT_NORM = {
        # Specials FIRST (longer keys before shorter to avoid partial match)
        "письменный английский": "Письменный английский",
        "письменн": "Письменный английский",
        "устный английский": "Устный английский",
        "устн": "Устный английский",
        "базовая математика": "Базовая математика",
        "профильная математика": "Профильная математика",
        "профильн": "Профильная математика",
        "базов": "Базовая математика",
        "база": "Базовая математика",
        # Generic
        "русский язык": "Русский язык",
        "русск": "Русский язык",
        "обществозн": "Обществознание",
        "общество": "Обществознание",
        "математик": "Математика",
        "английск": "Английский",
        "истори": "История",
        "биологи": "Биология",
        "химии": "Химия",
        "химия": "Химия",
        "физик": "Физика",
        "информатик": "Информатика",
        "географи": "География",
    }'''

if OLD_NORM in content:
    content = content.replace(OLD_NORM, NEW_NORM, 1)
    print("OK: improved _SUBJECT_NORM")
else:
    print("WARN: _SUBJECT_NORM block not found")

# ── 6. Add update/delete detection to fallback parser ─────────────────────
OLD_FALLBACK_START = "        # ── Exam date detection ────────────────────────────────────────────────"
NEW_FALLBACK_PREFIX = """\
        # ── Update/delete detection (BEFORE create) ───────────────────────────────
        update_kws = ("поставь", "перенеси", "измени", "поменяй", "обнови", "исправь")
        delete_kws = ("убери", "удали", "отмени", "уберём", "архивируй", "не нужен")

        # update_exam_date
        if any(k in lowered for k in update_kws) and any(k in lowered for k in ("экзамен", "егэ", "математик", "русск", "обществ", "английск", "базов", "профил")):
            date_val = self._parse_date_from_text(lowered)
            subject = self._norm_subject(text)
            if date_val and subject:
                return {"intents": [{"type": "update_exam_date", "target_subject": subject, "new_date": date_val}]}

        # delete_exam_date
        if any(k in lowered for k in delete_kws) and any(k in lowered for k in ("экзамен", "егэ", "математик", "русск", "обществ", "английск", "базов", "профил")):
            subject = self._norm_subject(text)
            return {"intents": [{"type": "delete_exam_date", "target_subject": subject}]}

        # delete_study_schedule_item
        if any(k in lowered for k in delete_kws) and any(k in lowered for k in ("занятие", "репетитор", "урок")):
            subject = self._norm_subject(text)
            weekday = None
            for word, code in self._WEEKDAY_MAP.items():
                if word in lowered:
                    weekday = code
                    break
            intent_d = {"type": "delete_study_schedule_item", "target_subject": subject}
            if weekday:
                intent_d["weekday"] = weekday
            return {"intents": [intent_d]}

        # ── Exam date detection ────────────────────────────────────────────────"""

if OLD_FALLBACK_START in content:
    content = content.replace(OLD_FALLBACK_START, NEW_FALLBACK_PREFIX, 1)
    print("OK: added update/delete detection to fallback parser")
else:
    print("WARN: fallback exam date detection start not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("ai_service.py patch complete.")
