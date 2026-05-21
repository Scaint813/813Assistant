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
