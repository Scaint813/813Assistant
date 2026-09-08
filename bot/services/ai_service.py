from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from bot.services.intent_quality import (
    SAFE_INTENT_CLARIFICATION,
    is_assistant_feedback,
    looks_like_task_request,
)

logger = logging.getLogger(__name__)

_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
AI_ROUTING_POLICY_VERSION = "routing-v2-2026-08-01"


@dataclass(frozen=True, slots=True)
class AIRequestPolicy:
    model: str
    tier: Literal["fast", "smart"]
    reasoning_effort: str | None
    max_completion_tokens: int

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
  show_tomorrow
  show_week
  show_daily_brief
  show_day_review
  show_conflicts
  show_tasks
  show_reminders
  show_projects
  show_inbox
  show_help
  show_weekly_review
  show_next
  pick_task
  plan_day
  show_archive
  show_study
  show_automations
  show_settings
  do_nothing
  update_task
  complete_task
  archive_task
  update_reminder
  cancel_reminder
  create_problem_block
  update_problem_block
  complete_problem_block
  archive_problem_block
  show_problem_blocks
  get_problem_solution
  get_problem_resources
  set_exam_date
  create_study_schedule_item
  decompose_project
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
   В поле text записывай только СМЫСЛ напоминания, без слова «напоминание» и без времени.
   Никогда не возвращай text="Напоминание".
   Пример: "18.20, напоминание — поход к врачу" →
   create_reminder {text: "поход к врачу", date: "today", time: "18:20"}

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

10. decompose_project — когда пользователь явно просит разбить цель/проект на шаги.
    Верни project, objective и 3–10 конкретных steps. Каждый шаг содержит title,
    outcome, next_action, estimated_minutes,
    dependencies (список названий предыдущих шагов) и blocked_reason.
    Шаг должен описывать наблюдаемый результат, а не абстрактное «заняться проектом».

11. Для create_task по возможности заполняй outcome, next_action,
    estimated_minutes, project, dependencies и blocked_reason. Не придумывай
    срок или оценку времени, если пользователь их не сообщил.

12. Для повторяющегося reminder заполняй recurrence: none/daily/weekdays/weekly/monthly.

13. Пользователь НЕ обязан помнить команды. Понимай естественные формулировки:
    «что у меня сегодня» → show_today; «что у меня завтра» → show_tomorrow;
    «сводка дня» / «что важно сегодня» → show_daily_brief;
    «подведи итоги дня» / «вечерняя сводка» → show_day_review;
    «покажи конфликты в расписании» / «есть ли накладки» → show_conflicts;
    «покажи расписание на неделю» → show_week; «покажи все дела» → show_tasks;
    «покажи напоминания» → show_reminders; «покажи проекты» → show_projects;
    «разберём входящие» → show_inbox; «что ты умеешь» → show_help;
    «подведи итоги недели» → show_weekly_review; «что делать сейчас» → show_next;
    «дай любую задачу» / «выбери одну задачу» → pick_task. Это выбор ТОЛЬКО
    из уже сохранённых задач: не создавай create_task;
    «помоги составить план на день» / «что влезет сегодня» / «что взять и
    что перенести» → plan_day. Это анализ уже сохранённых задач и свободного
    времени: не создавай задачи и напоминания;
    «покажи архив» → show_archive; «покажи учёбу» → show_study;
    «покажи автоматизации» → show_automations; «открой настройки» → show_settings.

14. Изменения существующих объектов:
    «перенеси задачу Документы на завтра» → update_task {target_title:"Документы", deadline:"..."};
    «в задаче Документы срок в пятницу и нужно 30 минут» → update_task;
    «задача Документы готова» → complete_task {target_title:"Документы"};
    «убери задачу Документы» → archive_task {target_title:"Документы"};
    «перенеси напоминание Врач на 19:00» → update_reminder {target_text:"Врач", remind_at:"..."};
    «отмени напоминание Врач» → cancel_reminder {target_text:"Врач"}.
    Если пользователь говорит «это/его/последнее», оставь target пустым: ссылка будет
    разрешена по последнему показанному объекту. Не превращай изменение в новую задачу.

15. Для каждого intent верни source_text — часть исходного сообщения, к которой он
    относится. Это обязательно для сообщения с несколькими поручениями.
""".strip()


class TaskStep(BaseModel):
    title: str
    outcome: str | None = None
    next_action: str | None = None
    importance: int | None = 3
    urgency: int | None = 3
    estimated_minutes: int | None = 30
    dependencies: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    deadline: str | None = None


class Intent(BaseModel):
    type: Literal[
        "create_task", "create_reminder", "rest_day", "schedule_override",
        "show_today", "show_tomorrow", "show_week", "show_daily_brief",
        "show_day_review", "show_conflicts", "show_tasks", "do_nothing",
        "show_reminders", "show_projects", "show_inbox", "show_help",
        "show_weekly_review", "show_next", "pick_task", "plan_day",
        "show_archive", "show_study", "show_automations", "show_settings",
        "update_task", "complete_task", "archive_task",
        "update_reminder", "cancel_reminder",
        "create_problem_block", "update_problem_block", "complete_problem_block",
        "archive_problem_block", "show_problem_blocks", "get_problem_solution",
        "get_problem_resources", "set_exam_date", "create_study_schedule_item",
        "update_exam_date", "delete_exam_date", "delete_study_schedule_item",
        "decompose_project",
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
    override_date: str | None = None
    mode: str | None = None
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
    outcome: str | None = None
    importance: int | None = 3
    urgency: int | None = 3
    estimated_minutes: int | None = 30
    project: str | None = None
    objective: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    # update/delete fields
    target_subject: str | None = None   # for update_exam_date / delete_exam_date
    new_date: str | None = None         # for update_exam_date
    source_text: str | None = None
    target: str | None = None
    target_id: int | None = None
    target_title: str | None = None
    target_text: str | None = None
    new_title: str | None = None
    new_text: str | None = None


class AIResult(BaseModel):
    intents: list[Intent] = Field(default_factory=list)


class AIService:
    def __init__(
        self,
        api_key: str,
        model_fast: str,
        model_smart: str,
        model_default: str = "",
        *,
        reasoning_fast: str = "low",
        reasoning_smart: str = "medium",
        max_output_fast: int = 1800,
        max_output_smart: int = 5000,
    ):
        self.api_key = api_key
        self.model_fast = model_fast or model_default
        self.model_smart = model_smart or self.model_fast
        self.reasoning_fast = self._normalize_effort(reasoning_fast, "low")
        self.reasoning_smart = self._normalize_effort(reasoning_smart, "medium")
        self.max_output_fast = max(600, max_output_fast)
        self.max_output_smart = max(1200, max_output_smart)
        self.last_usage: dict[str, Any] = {}

    def choose_model_for_intent(self, intent_type: str | None, text: str, context: dict[str, Any]) -> str:
        return self.request_policy(intent_type, text, context).model

    def request_policy(
        self,
        intent_type: str | None,
        text: str,
        context: dict[str, Any],
        *,
        planning: bool = False,
    ) -> AIRequestPolicy:
        """Resolve model depth and token ceiling from the user's real settings."""
        t = (intent_type or "").lower()
        lowered = text.lower()
        preferences = context.get("assistant_preferences") or {}
        ai_mode = str(preferences.get("ai_mode") or "auto").lower()
        token_mode = str(preferences.get("token_mode") or "balanced").lower()
        smart_markers = (
            "анализ", "проанализ", "конфликт", "перегруз", "составь план",
            "разбей", "декомпоз", "приоритет", "next", "следующий шаг",
        )
        complex_request = (
            planning
            or t in {"analyze", "overload", "planning", "next_step", "decompose_project"}
            or any(marker in lowered for marker in smart_markers)
        )

        if ai_mode == "economy":
            tier: Literal["fast", "smart"] = "fast"
        elif ai_mode == "smart":
            tier = "smart"
        else:
            tier = "smart" if complex_request else "fast"

        if tier == "smart":
            model = self.model_smart or self.model_fast
            base_effort = self.reasoning_smart
            base_max_tokens = self.max_output_smart
            effort_by_token_mode = {"economy": "low", "maximum": "high"}
        else:
            model = self.model_fast or self.model_smart
            base_effort = self.reasoning_fast
            base_max_tokens = self.max_output_fast
            effort_by_token_mode = {"economy": "none", "maximum": "medium"}

        effort = effort_by_token_mode.get(token_mode, base_effort)
        multiplier = {"economy": 0.7, "maximum": 1.6}.get(token_mode, 1.0)
        response_detail = str(preferences.get("response_detail") or "standard").lower()
        multiplier *= {"short": 0.8, "detailed": 1.2}.get(response_detail, 1.0)
        if ai_mode == "strict":
            multiplier *= 0.75
        max_tokens = max(600, int(base_max_tokens * multiplier))
        return AIRequestPolicy(
            model=model,
            tier=tier,
            reasoning_effort=effort if self._supports_reasoning(model) else None,
            max_completion_tokens=max_tokens,
        )

    async def parse_intent_with_openai(self, text: str, context: dict[str, Any]) -> dict[str, Any] | None:
        self.last_usage = {}
        if not self.api_key:
            logger.info("OPENAI_API_KEY is not configured; using fallback parser")
            return None
        policy = self.request_policy(None, text, context)
        if not policy.model:
            logger.warning("No OpenAI model configured; using fallback parser")
            return None
        today = context.get("today", "")
        prompt = SYSTEM_PROMPT.replace("{TODAY}", today)
        prompt = f"{prompt}\n\n{self._preference_guidance(context)}"
        logger.info(
            "OpenAI parser policy: tier=%s model=%s effort=%s max_output=%s",
            policy.tier,
            policy.model,
            policy.reasoning_effort or "n/a",
            policy.max_completion_tokens,
        )
        payload = {
            "model": policy.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"text": text, "context": context}, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        payload.update(self._request_controls(policy, temperature=0))
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
            self.last_usage = self._usage_payload("parser", response.json())
            self._log_usage("parser", response.json())
            parsed = AIResult.model_validate(
                json.loads(response.json()["choices"][0]["message"]["content"])
            )
            return parsed.model_dump()
        except Exception:
            logger.exception("OpenAI parser failed, fallback will be used")
            return None

    async def generate_task_plan(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a structured plan, with a deterministic honest fallback."""
        policy = self.request_policy("planning", text, context, planning=True)
        if self.api_key and policy.model:
            payload = {
                "model": policy.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Разбей цель на 3–10 конкретных проверяемых шагов. "
                            "Верни только JSON: {project, objective, steps:[{title,outcome,next_action,"
                            "estimated_minutes,dependencies,blocked_reason}]}. "
                            "Не выдумывай факты, людей, даты и ресурсы. "
                            f"{self._preference_guidance(context)}"
                        ),
                    },
                    {"role": "user", "content": json.dumps({"request": text, "context": context}, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            }
            payload.update(self._request_controls(policy, temperature=0.2))
            logger.info(
                "OpenAI planning policy: tier=%s model=%s effort=%s max_output=%s",
                policy.tier,
                policy.model,
                policy.reasoning_effort or "n/a",
                policy.max_completion_tokens,
            )
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                self.last_usage = self._usage_payload("planning", response.json())
                self._log_usage("planning", response.json())
                result = json.loads(response.json()["choices"][0]["message"]["content"])
                steps = [TaskStep.model_validate(step).model_dump() for step in result.get("steps", [])]
                if steps:
                    return {
                        "project": str(result.get("project") or text)[:128],
                        "objective": str(result.get("objective") or text)[:1000],
                        "steps": steps[:10],
                    }
            except Exception:
                logger.exception("Project planning failed, using safe skeleton")

        project = self._clean_project_request(text)
        titles = [
            "Зафиксировать измеримый результат и ограничения",
            "Собрать необходимые входные данные и материалы",
            "Выполнить основной рабочий этап",
            "Проверить результат по критериям и закрыть проект",
        ]
        return {
            "project": project[:128],
            "objective": project,
            "steps": [
                {
                    "title": title,
                    "outcome": title,
                    "next_action": title,
                    "importance": 4 if index in (0, 2) else 3,
                    "urgency": 3,
                    "estimated_minutes": 30 if index != 2 else 60,
                    "dependencies": [] if index == 0 else [titles[index - 1]],
                    "blocked_reason": "",
                }
                for index, title in enumerate(titles)
            ],
        }

    @staticmethod
    def _normalize_effort(value: str, default: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _REASONING_EFFORTS else default

    @staticmethod
    def _supports_reasoning(model: str) -> bool:
        lowered = model.lower()
        return lowered.startswith(("gpt-5", "o1", "o3", "o4"))

    @staticmethod
    def _request_controls(policy: AIRequestPolicy, temperature: float) -> dict[str, Any]:
        controls: dict[str, Any] = {"max_completion_tokens": policy.max_completion_tokens}
        if policy.reasoning_effort:
            controls["reasoning_effort"] = policy.reasoning_effort
        else:
            controls["temperature"] = temperature
        return controls

    @staticmethod
    def _preference_guidance(context: dict[str, Any]) -> str:
        preferences = context.get("assistant_preferences") or {}
        detail = str(preferences.get("response_detail") or "standard").lower()
        style = str(preferences.get("answer_style") or "dry").lower()
        detail_guidance = {
            "short": "Поля формулируй максимально кратко; для плана достаточно 3–5 шагов.",
            "detailed": "Давай конкретные детали; для плана допустимо 6–10 шагов.",
        }.get(detail, "Сохраняй практичную среднюю детализацию; для плана обычно 4–7 шагов.")
        style_guidance = {
            "soft": "Тон формулировок спокойный и поддерживающий, без сюсюканья.",
            "balanced": "Тон формулировок нейтральный и человечный.",
        }.get(style, "Тон формулировок деловой и прямой, без воды.")
        return f"{detail_guidance} {style_guidance}"

    @staticmethod
    def _log_usage(operation: str, body: dict[str, Any]) -> None:
        usage = body.get("usage") or {}
        if usage:
            logger.info(
                "OpenAI %s usage: input=%s output=%s total=%s",
                operation,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )

    @staticmethod
    def _usage_payload(operation: str, body: dict[str, Any]) -> dict[str, Any]:
        usage = body.get("usage") or {}
        return {
            "operation": operation,
            "model": body.get("model") or "",
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
    def parse_intent_fallback(self, text: str) -> dict[str, Any]:
        """
        Rule-based fallback.
        Priority:
          1. Explicit study patterns  → set_exam_date / create_study_schedule_item
          2. Explicit problem words   → create_problem_block
          3. Explicit "напомни"       → create_reminder
          4. Rest day words           → rest_day
          5. Control phrases          → do_nothing
          6. Unknown input            → clarification, without a mutation
        """
        lowered = text.lower().strip()

        if is_assistant_feedback(text):
            return {
                "intents": [],
                "clarification": (
                    "Похоже, это обратная связь об ассистенте, а не новое дело. "
                    "Ничего не сохраняю. Напиши, что именно сработало неправильно."
                ),
            }

        if any(p in lowered for p in (
            "сводка дня", "утренняя сводка", "что важно сегодня",
            "главное на сегодня",
        )):
            return {"intents": [{"type": "show_daily_brief"}]}
        if any(p in lowered for p in (
            "подведи итоги дня", "итоги дня", "вечерняя сводка",
        )):
            return {"intents": [{"type": "show_day_review"}]}
        if any(p in lowered for p in (
            "покажи конфликты", "конфликты в расписании", "есть ли накладки",
            "проверь накладки", "проверь пересечения",
        )):
            return {"intents": [{"type": "show_conflicts"}]}

        if AIService._looks_like_day_plan_query(lowered):
            return {"intents": [{"type": "plan_day"}]}

        if any(p in lowered for p in (
            "что у меня завтра", "дела на завтра", "план на завтра",
            "расписание на завтра", "покажи завтра",
        )):
            return {"intents": [{"type": "show_tomorrow"}]}
        if any(p in lowered for p in (
            "что у меня на неделю", "дела на неделю", "план на неделю",
            "расписание на неделю", "покажи неделю", "ближайшие семь дней",
        )):
            return {"intents": [{"type": "show_week"}]}

        if AIService._looks_like_task_pick_query(lowered):
            return {"intents": [{"type": "pick_task"}]}

        if any(marker in lowered for marker in ("разбей на шаги", "разбей проект", "составь план", "декомпозируй")):
            return {"intents": [{"type": "decompose_project", "project": self._clean_project_request(text)}]}

        # ── Navigation / query intents ─────────────────────────────────────────
        if any(p in lowered for p in (
            "что сегодня", "что у меня сегодня", "дела на сегодня",
            "покажи сегодня", "план на сегодня", "расписание на сегодня",
        )):
            return {"intents": [{"type": "show_today"}]}
        if any(p in lowered for p in ("покажи задачи", "что по задачам", "активные задачи")):
            return {"intents": [{"type": "show_tasks"}]}
        if any(p in lowered for p in ("покажи все дела", "все мои дела", "список дел")):
            return {"intents": [{"type": "show_tasks"}]}
        if any(p in lowered for p in (
            "покажи напоминания",
            "какие напоминания",
            "какие у меня напоминания",
            "мои напоминания",
        )):
            return {"intents": [{"type": "show_reminders"}]}
        if any(p in lowered for p in ("покажи проекты", "что по проектам", "мои проекты")):
            return {"intents": [{"type": "show_projects"}]}
        if any(p in lowered for p in ("разбери входящие", "разберём входящие", "покажи входящие")):
            return {"intents": [{"type": "show_inbox"}]}
        if any(p in lowered for p in ("что ты умеешь", "как тобой пользоваться", "покажи примеры")):
            return {"intents": [{"type": "show_help"}]}
        if any(p in lowered for p in ("итоги недели", "подведи итоги недели", "обзор недели")):
            return {"intents": [{"type": "show_weekly_review"}]}
        if any(p in lowered for p in ("что делать сейчас", "что мне делать сейчас", "следующее действие")):
            return {"intents": [{"type": "show_next"}]}
        if any(p in lowered for p in ("покажи архив", "что в архиве", "завершённые дела", "завершенные дела")):
            return {"intents": [{"type": "show_archive"}]}
        if any(p in lowered for p in ("покажи учёбу", "покажи учебу", "что по учёбе", "что по учебе")):
            return {"intents": [{"type": "show_study"}]}
        if any(p in lowered for p in ("покажи автоматизации", "настрой напоминания", "настрой подсказки")):
            return {"intents": [{"type": "show_automations"}]}
        if any(p in lowered for p in ("открой настройки", "покажи настройки", "настрой ассистента")):
            return {"intents": [{"type": "show_settings"}]}
        if any(p in lowered for p in ("покажи блоки", "/problems", "/blocks", "активные блоки")):
            return {"intents": [{"type": "show_problem_blocks"}]}

        # ── Control phrases ────────────────────────────────────────────────────
        if (
            any(p in lowered for p in ("не записывай", "просто подумай", "ничего не сохраняй", "не добавляй"))
            and "напомни" not in lowered
            and "напоминание" not in lowered
        ):
            return {"intents": [{"type": "do_nothing", "reply": "Понял, ничего не записываю."}]}

        # ── Natural edits (before create rules) ───────────────────────────────
        if "напоминан" in lowered and any(word in lowered for word in ("отмени", "удали", "убери")):
            return {"intents": [{
                "type": "cancel_reminder",
                "target_text": self._clean_target(text, "напоминан"),
                "source_text": text,
            }]}
        if "напоминан" in lowered and any(word in lowered for word in ("перенеси", "измени", "поменяй", "исправь")):
            return {"intents": [{
                "type": "update_reminder",
                "target_text": self._clean_target(text, "напоминан"),
                "source_text": text,
            }]}
        if "задач" in lowered and any(word in lowered for word in ("готова", "готово", "выполнил", "выполнила", "закрой", "заверши")):
            return {"intents": [{
                "type": "complete_task",
                "target_title": self._clean_target(text, "задач"),
                "source_text": text,
            }]}
        if "задач" in lowered and any(word in lowered for word in ("удали", "убери", "архивируй")):
            return {"intents": [{
                "type": "archive_task",
                "target_title": self._clean_target(text, "задач"),
                "source_text": text,
            }]}
        if "задач" in lowered and (
            any(word in lowered for word in ("перенеси", "измени", "поменяй", "исправь", "добавь срок"))
            or ("в задач" in lowered and any(word in lowered for word in ("срок", "дедлайн", "займёт", "займет", "минут", "час")))
        ):
            return {"intents": [{
                "type": "update_task",
                "target_title": self._clean_target(text, "задач"),
                "source_text": text,
            }]}

        # An explicit reminder request wins over unrelated domain keywords.
        if "напомни" in lowered or "напоминание" in lowered:
            return {"intents": [{"type": "create_reminder", "text": text, "priority": "medium", "source_text": text}]}

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

        # ── Conservative task fallback ────────────────────────────────────────
        if looks_like_task_request(text):
            return {"intents": [{
                "type": "create_task",
                "title": text,
                "description": "",
                "priority": "medium",
                "deadline": None,
                "is_minor": False,
                "auto_cleanup_allowed": False,
                "source_text": text,
            }]}
        return {"intents": [], "clarification": SAFE_INTENT_CLARIFICATION}

    @staticmethod
    def _looks_like_task_pick_query(lowered: str) -> bool:
        return any(phrase in lowered for phrase in (
            "дай любую задачу",
            "дай мне любую задачу",
            "выбери задачу",
            "выбери одну задачу",
            "какую задачу сделать",
            "какую задачу мне сделать",
            "что взять сейчас",
        ))

    @staticmethod
    def _looks_like_day_plan_query(lowered: str) -> bool:
        direct = (
            "составь план на день",
            "составь мне план на день",
            "помоги составить план на день",
            "распланируй день",
            "распланируй сегодня",
            "что влезет сегодня",
            "что поместится сегодня",
            "что взять сегодня",
            "что не брать сегодня",
        )
        if any(phrase in lowered for phrase in direct):
            return True
        return (
            any(word in lowered for word in ("план", "распредели", "распиши"))
            and any(word in lowered for word in ("сегодня", "на день", "по времени"))
            and any(word in lowered for word in ("задач", "дел", "влез", "вмест", "перенес"))
        )

    def parse_intent_fallback_batch(self, text: str) -> dict[str, Any]:
        """Parse explicit voice/text lists without merging every line into one task."""
        chunks = re.split(r"[\r\n]+", text)
        if len(chunks) < 2:
            spoken = re.split(
                r"\b(?:во[- ]первых|первое|во[- ]вторых|второе|в[- ]третьих|третье)\b\s*[:,.-]?",
                text,
                flags=re.IGNORECASE,
            )
            spoken = [part for part in spoken if part.strip()]
            if len(spoken) >= 2:
                chunks = spoken
        if len(chunks) < 2:
            chunks = re.split(
                r";(?=\s*(?:напомни|добавь|создай|запиши|ещё|еще|также)\b)",
                text,
                flags=re.IGNORECASE,
            )
        raw_parts = []
        for raw in chunks:
            part = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", raw).strip()
            if part:
                raw_parts.append(part)
        if len(raw_parts) < 2:
            return self.parse_intent_fallback(text)
        intents: list[dict[str, Any]] = []
        clarifications: list[str] = []
        for part in raw_parts[:12]:
            parsed = self.parse_intent_fallback(part)
            if parsed.get("clarification"):
                clarifications.append(f"«{part}»: {parsed['clarification']}")
            for intent in parsed.get("intents", []):
                item = dict(intent)
                item.setdefault("source_text", part)
                intents.append(item)
        if clarifications:
            return {"intents": [], "clarification": "\n\n".join(clarifications)}
        return {"intents": intents}

    @staticmethod
    def _clean_target(text: str, entity_word: str) -> str:
        value = re.sub(
            r"\b(?:перенеси|измени|поменяй|исправь|отмени|удали|убери|архивируй|закрой|заверши|готова|готово|выполнил[аи]?)\b",
            " ", text, flags=re.IGNORECASE,
        )
        value = re.sub(rf"\b{entity_word}\w*\b", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\b(?:срок|дедлайн|займ(?:ё|е)т|нужно)\b.*$", "", value, flags=re.IGNORECASE)
        value = re.sub(r"^\s*(?:в|по|про)\s+", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\b(?:на|до|к)\s+(?:сегодня|завтра|послезавтра|\d{1,2}[.:]\d{2}).*$", "", value, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", value).strip(" .,:;—-")

    @staticmethod
    def _clean_project_request(text: str) -> str:
        cleaned = text
        for marker in ("разбей на шаги", "разбей проект", "составь план", "декомпозируй"):
            cleaned = re.sub(re.escape(marker), "", cleaned, count=1, flags=re.IGNORECASE)
        return cleaned.strip(" :—-.,") or text.strip()

    # ── Fallback helpers ──────────────────────────────────────────────────────

    _MONTH_MAP: ClassVar[dict[str, str]] = {
        "янв": "01", "фев": "02", "мар": "03", "апр": "04",
        "май": "05", "мая": "05", "июн": "06", "июл": "07",
        "авг": "08", "сен": "09", "окт": "10", "ноя": "11", "дек": "12",
    }

    _WEEKDAY_MAP: ClassVar[dict[str, str]] = {
        "понедельник": "mon", "вторник": "tue", "среда": "wed",
        "четверг": "thu", "пятница": "fri", "суббота": "sat", "воскресенье": "sun",
        "вторникам": "tue", "средам": "wed", "четвергам": "thu",
        "пятницам": "fri", "субботам": "sat", "воскресеньям": "sun",
        "понедельникам": "mon",
    }

    _SUBJECT_NORM: ClassVar[dict[str, str]] = {
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
        pattern = r"(\d{1,2})\s+(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)"
        m = re.search(pattern, lowered)
        if m:
            day = m.group(1).zfill(2)
            month = self._MONTH_MAP.get(m.group(2)[:3], "01")
            year = datetime.now(UTC).year
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
            iso_date = f"{datetime.now(UTC).year}-{mon}-{day}"
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
