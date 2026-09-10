from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

DEFAULTS: dict = {
    "ai_mode": "auto",         # auto | economy | smart | strict
    "token_mode": "balanced",  # economy | balanced | maximum
    "response_detail": "standard",  # short | standard | detailed
    "answer_style": "dry",     # dry | balanced | soft
    "chat_cleanup_enabled": True,
    "workday_start_hour": 9,
    "workday_end_hour": 21,
    "daily_focus_minutes": 180,
    "large_task_block_minutes": 90,
    "planning_weekends": True,
    "water_goal_ml": 0,
    "protein_goal_g": 0,
}


def _load(profile) -> dict:
    try:
        return json.loads(profile.preferences_json or "{}")
    except Exception:
        return {}


def _save(profile, prefs: dict) -> None:
    profile.preferences_json = json.dumps(prefs, ensure_ascii=False)


class PreferencesService:
    """Read/write user preferences stored in UserProfile.preferences_json."""

    def get(self, profile, key: str):
        prefs = _load(profile)
        return prefs.get(key, DEFAULTS.get(key))

    def get_all(self, profile) -> dict:
        prefs = _load(profile)
        return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}

    def set(self, profile, key: str, value) -> None:
        prefs = _load(profile)
        prefs[key] = value
        _save(profile, prefs)

    # ── Convenience getters ──────────────────────────────────────────────

    def ai_mode(self, profile) -> str:
        return self.get(profile, "ai_mode")

    def token_mode(self, profile) -> str:
        return self.get(profile, "token_mode")

    def response_detail(self, profile) -> str:
        return self.get(profile, "response_detail")

    def answer_style(self, profile) -> str:
        return self.get(profile, "answer_style")

    def chat_cleanup_enabled(self, profile) -> bool:
        return bool(self.get(profile, "chat_cleanup_enabled"))

    def planning(self, profile) -> dict[str, int | bool | str]:
        start = max(0, min(22, int(self.get(profile, "workday_start_hour") or 9)))
        end = max(start + 1, min(23, int(self.get(profile, "workday_end_hour") or 21)))
        focus = max(60, min(1440, int(self.get(profile, "daily_focus_minutes") or 180)))
        block = max(30, min(180, int(self.get(profile, "large_task_block_minutes") or 90)))
        auto_focus = min(focus, 480)
        if focus > 720:
            focus_level = "overload"
            focus_warning = (
                "Такой объём не оставляет достаточно времени на сон, движение и "
                "восстановление. Автоплан ограничен 8 часами фокуса и не будет "
                "заполнять оставшееся время автоматически."
            )
        elif focus > 480:
            focus_level = "high"
            focus_warning = (
                "Это высокая дневная нагрузка. Автоплан ограничен 8 часами, "
                "чтобы сохранить время на паузы и восстановление."
            )
        else:
            focus_level = "balanced"
            focus_warning = ""
        return {
            "workday_start_hour": start,
            "workday_end_hour": end,
            "daily_focus_minutes": focus,
            "auto_planning_minutes": auto_focus,
            "focus_load_level": focus_level,
            "focus_warning": focus_warning,
            "large_task_block_minutes": block,
            "planning_weekends": bool(self.get(profile, "planning_weekends")),
        }

    def wellness_goals(self, profile) -> dict[str, int]:
        return {
            "water_ml": max(0, min(10_000, int(self.get(profile, "water_goal_ml") or 0))),
            "protein_g": max(0, min(400, int(self.get(profile, "protein_goal_g") or 0))),
        }
