from __future__ import annotations

from typing import ClassVar


class OverloadService:
    KEYWORDS: ClassVar[dict[str, str]] = {
        "плохо": "stress",
        "сгораю": "stress",
        "не вывожу": "stress",
        "перегруз": "stress",
        "перегрев": "stress",
        "пустота": "stress",
        "не спал": "sleep_low",
        "боль": "pain",
        "болит": "pain",
        "не могу ходить": "pain",
        "разнос": "stress",
        "устал": "fatigue",
        "разбит": "fatigue",
        "паника": "stress",
        "конфликт": "conflict",
        "никотин": "nicotine",
        "кофеин": "caffeine",
        "обезбол": "pain",
    }

    def detect_overload(self, text: str, context: dict) -> dict:
        lowered = text.lower()
        triggers = []
        for kw, tag in self.KEYWORDS.items():
            if kw in lowered and tag not in triggers:
                triggers.append(tag)
        is_overload = bool(triggers)
        level = "high" if len(triggers) >= 2 else "medium"
        return {
            "is_overload": is_overload,
            "level": level if is_overload else "low",
            "triggers": triggers,
            "recommended_mode": "stabilization" if is_overload else "normal",
        }
