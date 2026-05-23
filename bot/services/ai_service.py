from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Key rule ──────────────────────────────────────────────────────────────────
# set_exam_date       : ЕГЭ/экзамен + дата предмета  (НЕ reminder)
# create_study_schedule_item : репетитор/занятие       (НЕ reminder)
# create_problem_block category=exam : учебная проблема (НЕ reminder)
# create_reminder     : ТОЛЬКО при явном "напомни / поставь напоминание"
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
Ты — личный AI-ассистент. Возвращай ТОЛЬКО JSON формата {"intents": [...]}.

Разрешённые типы intents:
  create_task
  create_reminder
  rest_day
  schedule_override
  show_today
  show_tasks
  do_nothing
  create_problem_block
  update_problem_block
  complete_problem_block
  archive_problem_block
  show_problem_blocks
  get_problem_solution
  get_problem_resources
  set_exam_date
  create_study_schedule_item
  update_exam_date
  delete_exam_date
  delete_study_schedule_item

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:

1. set_exam_date — когда пользователь называет предмет и дату экзамена/ЕГЭ.
   НЕ делай reminder из дат экзаменов!
   Пример: "ЕГЭ по русскому 3 июня" → set_exam_date {subject: "Русский", exam_date: "2026-06-03"}
   Пример: "общество 10 июня" (в контексте ЕГЭ) → set_exam_date {subject: "Обществознание", exam_date: "2026-06-10"}

2. create_study_schedule_item — когда пользователь называет репетитора или регулярное занятие.
   НЕ делай reminder из расписания репетитора!
   Пример: "репетитор по английскому по вторникам в 18:00" → create_study_schedule_item {subject: "Английский", weekday: "tue", time_str: "18:00", recurrence: "weekly", tutor_name: "репетитор"}
   Пример: "занятие по обществу в четверг 17:00" → create_study_schedule_item {subject: "Обществознание", weekday: "thu", time_str: "17:00", recurrence: "weekly"}

3. create_problem_block category=exam — когда пользователь называет учебную проблему/слабое место.
   НЕ делай task из учебных проблем!
   Пример: "проблема с 24 заданием" → create_problem_block {category: "exam", title: "Задание 24", ...}
   Пример: "плохо пишу комментарий по русскому" → create_problem_block {category: "exam", title: "Комментарий", ...}

4. create_reminder — ТОЛЬКО при явном слове "напомни", "поставь напоминание", "напоминание".
   Если пользователь не говорит "напомни" — НЕ создавай reminder.

5. Одно сообщение может содержать несколько intents — список intents может быть длиннее 1.

6. Сегодня: {TODAY}. Используй для вычисления дат.

7. update_exam_date — когда пользователь хочет изменить дату существующего экзамена.
   Пример: "поставь базовую математику 8 июня" → update_exam_date {target_subject: "Базовая математика", new_date: "2026-06-08"}
   Пример: "перенеси русский на 1 июня" → update_exam_date {target_subject: "Русский язык", new_date: "2026-06-01"}
   Пример: "математика 8 июня" (если уже есть в системе) → update_exam_date (не set_exam_date!)

8. delete_exam_date — когда пользователь хочет убрать/удалить экзамен.
   Пример: "убери экзамен математика" → delete_exam_date {target_subject: "Математика"}
   Пример: "удали дубль математика" → delete_exam_date {target_subject: "Математика"}

9. delete_study_schedule_item — когда пользователь хочет убрать занятие.
   Пример: "убери занятие по английскому в воскресенье" → delete_study_schedule_item {target_subject: "Английский", weekday: "sun"}
""".strip()


class Intent(BaseModel):
    type: Literal[
        "create_task", "create_reminder", "rest_day", "show_today", "show_tasks", "do_nothing",
        "create_problem_block", "update_problem_block", "complete_problem_block",
        "archive_problem_block", "show_problem_blocks", "get_problem_solution",
        "get_problem_resources", "set_exam_date", "create_study_schedule_item",
        "update_exam_date", "delete_exam_date", "delete_study_schedule_item",
    ]
    title: str | None = None
    description: str | None = None
    priority: str | None = "medium"
    deadline: str | None = None
    is_minor: bool | None = False
    auto_cleanup_allowed: bool | None = False
    text: str | None = None
    remind_at: str | None = None
    date: str | None = None
    time: str | None = None
    reply: str | None = None
    create_tasks: bool | None = False
    write_to_miro: bool | None = False
    category: str | None = None
    problem_text: str | None = None
    solution_strategy: str | None = None
    next_action: str | None = None
    pressure_level: str | None = None
    # exam_date fields
    subject: str | None = None
    exam_date: str | None = None
    # study_schedule fields
    weekday: str | None = None
    time_str: str | None = None
    recurrence: str | None = None
    tutor_name: str | None = None
    location_or_link: str | None = None
    # update/delete fields
    target_subject: str | None = None   # for update_exam_date / delete_exam_date
    new_date: str | None = None         # for update_exam_date


class AIResult(BaseModel):
    intents: list[Intent] = Field(default_factory=list)


class AIService:
    def __init__(self, api_key: str, model_fast: str, model_smart: str, model_default: str = ""):
        self.api_key = api_key
        self.model_fast = model_fast or model_default
        self.model_smart = model_smart or self.model_fast

    def choose_model_for_intent(self, intent_type: str | None, text: str, context: dict[str, Any]) -> str:
        t = (intent_type or "").lower()
        lowered = text.lower()
        smart_markers = ["анализ", "конфликт", "перегруз", "план", "next", "следующий шаг"]
        if t in {"analyze", "overload", "planning", "next_step"} or any(m in lowered for m in smart_markers):
            return self.model_smart or self.model_fast
        return self.model_fast or self.model_smart

    async def parse_intent_with_openai(self, text: str, context: dict[str, Any]) -> dict[str, Any] | None:
        if not self.api_key:
            logger.info("OPENAI_API_KEY is not configured; using fallback parser")
            return None
        model = self.choose_model_for_intent(None, text, context)
        if not model:
            logger.warning("No OpenAI model configured; using fallback parser")
            return None
        today = context.get("today", "")
        prompt = SYSTEM_PROMPT.replace("{TODAY}", today)
        logger.info("OpenAI parser model: %s", model)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"text": text, "context": context}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                parsed = AIResult.model_validate(json.loads(content))
                return parsed.model_dump()
        except Exception as exc:
            logger.exception("OpenAI parser failed, fallback will be used: %s", exc)
            return None

    def parse_intent_fallback(self, text: str) -> dict[str, Any]:
        """
        Rule-based fallback.
        Priority:
          1. Explicit study patterns  → set_exam_date / create_study_schedule_item
          2. Explicit problem words   → create_problem_block
          3. Explicit "напомни"       → create_reminder
          4. Rest day words           → rest_day
          5. Control phrases          → do_nothing
          6. Default                  → create_task
        """
        lowered = text.lower().strip()

        # ── Navigation / query intents ─────────────────────────────────────────
        if any(p in lowered for p in ("что сегодня", "что у меня сегодня", "покажи сегодня", "план на сегодня")):
            return {"intents": [{"type": "show_today"}]}
        if any(p in lowered for p in ("покажи задачи", "что по задачам", "активные задачи")):
            return {"intents": [{"type": "show_tasks"}]}
        if any(p in lowered for p in ("покажи блоки", "/problems", "/blocks", "активные блоки")):
            return {"intents": [{"type": "show_problem_blocks"}]}

        # ── Control phrases ────────────────────────────────────────────────────
        if any(p in lowered for p in ("не записывай", "просто подумай", "ничего не сохраняй", "не добавляй")) and "напомни" not in lowered:
            return {"intents": [{"type": "do_nothing", "reply": "Понял, ничего не записываю."}]}
        if any(p in lowered for p in ("отдыхаю", "день отдыха", "ничего не ставь", "без тренировки")):
            date_val = "today" if "сегодня" in lowered else ("tomorrow" if "завтра" in lowered else "today")
            return {"intents": [{"type": "rest_day", "date": date_val, "title": "День отдыха", "create_tasks": False, "write_to_miro": False}]}

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

        # ── Exam date detection ────────────────────────────────────────────────
        # Triggers: егэ|экзамен|эге + предмет + (число + месяц или дата)
        exam_keywords = ("егэ", "экзамен", "эге", "сдаю", "сдавать", "сдача")
        if any(k in lowered for k in exam_keywords):
            intents = self._extract_exam_intents(text, lowered)
            if intents:
                return {"intents": intents}

        # ── Study schedule detection ───────────────────────────────────────────
        tutor_keywords = ("репетитор", "занятие", "тренировка", "урок", "коуч", "ментор")
        day_keywords = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
                        "по вторникам", "по средам", "по четвергам", "каждую")
        if any(k in lowered for k in tutor_keywords) and any(k in lowered for k in day_keywords):
            intents = self._extract_schedule_intents(text, lowered)
            if intents:
                return {"intents": intents}

        # ── Study problem detection ────────────────────────────────────────────
        study_problem_keywords = (
            "проблема с заданием", "задание", "не получается", "плохо пишу",
            "путаюсь в задании", "слабое место", "не умею", "комментарий",
            "эссе", "сочинение", "критерий", "не дотягиваю", "24 задание",
            "задание 24", "план", "подпункт", "сложно"
        )
        study_subjects = ("русск", "общество", "математик", "английск", "истори", "биологи",
                          "химии", "физик", "географи", "информатик")
        is_study_context = (
            any(k in lowered for k in study_problem_keywords) and
            any(k in lowered for k in study_subjects)
        ) or (
            any(k in lowered for k in ("ошибка", "путаюсь")) and
            any(k in lowered for k in study_subjects)
        )
        if is_study_context:
            category = "exam"
            return {"intents": [{
                "type": "create_problem_block",
                "category": category,
                "title": text[:80],
                "problem_text": text,
                "solution_strategy": "Разобрать типовые задания. Найти критерии. Сделать 2 пробных примера.",
                "next_action": "Сделать 2 пробных задания по критериям",
                "priority": "high",
                "pressure_level": "normal",
            }]}

        # ── Generic problem detection ──────────────────────────────────────────
        if any(p in lowered for p in ("ошибка", "путаюсь", "срываю", "перегружаюсь", "откладываю", "конфликт")):
            category = "other"
            if any(k in lowered for k in ("егэ", "обществ", "экзам", "русск", "английск")):
                category = "exam"
            elif "сон" in lowered:
                category = "sleep"
            elif "заказ" in lowered:
                category = "orders"
            elif any(k in lowered for k in ("деньг", "оплат", "финанс")):
                category = "money"
            elif any(k in lowered for k in ("перегруз", "выгор")):
                category = "overload"
            return {"intents": [{
                "type": "create_problem_block",
                "category": category,
                "title": text[:80],
                "problem_text": text,
                "solution_strategy": "Разбить проблему на короткий протокол и убрать лишние действия",
                "next_action": "Сделать один короткий шаг по протоколу",
                "priority": "high",
                "pressure_level": "normal",
            }]}

        # ── Reminder: ONLY explicit "напомни" ─────────────────────────────────
        if "напомни" in lowered:
            return {"intents": [{"type": "create_reminder", "text": text, "date": "today", "time": "evening", "priority": "medium"}]}

        # ── Default: task ─────────────────────────────────────────────────────
        return {"intents": [{"type": "create_task", "title": text, "description": "", "priority": "medium", "deadline": None, "is_minor": False, "auto_cleanup_allowed": False}]}

    # ── Fallback helpers ──────────────────────────────────────────────────────

    _MONTH_MAP = {
        "янв": "01", "фев": "02", "мар": "03", "апр": "04",
        "май": "05", "мая": "05", "июн": "06", "июл": "07",
        "авг": "08", "сен": "09", "окт": "10", "ноя": "11", "дек": "12",
    }

    _WEEKDAY_MAP = {
        "понедельник": "mon", "вторник": "tue", "среда": "wed",
        "четверг": "thu", "пятница": "fri", "суббота": "sat", "воскресенье": "sun",
        "вторникам": "tue", "средам": "wed", "четвергам": "thu",
        "пятницам": "fri", "субботам": "sat", "воскресеньям": "sun",
        "понедельникам": "mon",
    }

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
    }

    def _norm_subject(self, text: str) -> str:
        lowered = text.lower()
        for key, val in self._SUBJECT_NORM.items():
            if key in lowered:
                return val
        # capitalize first word as fallback
        words = text.strip().split()
        return words[0].capitalize() if words else text

    def _parse_date_from_text(self, lowered: str) -> str | None:
        """Extract ISO date string from russian text like '3 июня'."""
        import re
        from datetime import date
        pattern = r"(\d{1,2})\s+(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)"
        m = re.search(pattern, lowered)
        if m:
            day = m.group(1).zfill(2)
            month = self._MONTH_MAP.get(m.group(2)[:3], "01")
            year = date.today().year
            return f"{year}-{month}-{day}"
        return None

    def _extract_exam_intents(self, text: str, lowered: str) -> list[dict]:
        """Try to extract one or more set_exam_date intents from free text."""
        import re
        intents = []
        # Find all "предмет + число + месяц" occurrences
        day_month_pattern = r"(\d{1,2})\s+(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)"
        for m in re.finditer(day_month_pattern, lowered):
            day = m.group(1).zfill(2)
            mon = self._MONTH_MAP.get(m.group(2)[:3], "01")
            from datetime import date
            iso_date = f"{date.today().year}-{mon}-{day}"
            # Find subject before this date (look back 40 chars)
            start = max(0, m.start() - 50)
            snippet = lowered[start:m.start()]
            subject = None
            for key, val in self._SUBJECT_NORM.items():
                if key in snippet:
                    subject = val
                    break
            if not subject:
                subject = "Предмет"
            intents.append({
                "type": "set_exam_date",
                "subject": subject,
                "exam_date": iso_date,
            })
        return intents

    def _extract_schedule_intents(self, text: str, lowered: str) -> list[dict]:
        """Extract create_study_schedule_item from free text."""
        import re
        # weekday
        weekday = "mon"
        for word, code in self._WEEKDAY_MAP.items():
            if word in lowered:
                weekday = code
                break
        # time HH:MM
        time_match = re.search(r"(\d{1,2}):(\d{2})", lowered)
        time_str = f"{time_match.group(1).zfill(2)}:{time_match.group(2)}" if time_match else ""
        # subject
        subject = "Предмет"
        for key, val in self._SUBJECT_NORM.items():
            if key in lowered:
                subject = val
                break
        # tutor
        tutor = "репетитор" if "репетитор" in lowered else ""
        return [{
            "type": "create_study_schedule_item",
            "subject": subject,
            "weekday": weekday,
            "time_str": time_str,
            "recurrence": "weekly",
            "tutor_name": tutor,
            "title": f"{subject} — {text[:60]}",
        }]
