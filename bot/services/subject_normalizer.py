"""
Subject normalizer for 813Assistant.

Handles canonical matching for exam subjects:
- "математика" without qualifier → "Базовая математика" (if only basic exists in DB)
- "русский" → "Русский язык"
- "обществ" / "общество" → "Обществознание"
etc.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.database.models import ExamDate

# ── Canonical subject definitions ─────────────────────────────────────────────
# Each entry: (canonical_name, [aliases_lowercased])
# First matching alias wins.

_CANONICAL_DEFS: list[tuple[str, list[str]]] = [
    # Must be BEFORE generic "английский" to match longer first
    ("Письменный английский", [
        "письменный английский", "письменн", "письмен англ", "writing", "английский письм",
    ]),
    ("Устный английский", [
        "устный английский", "устн", "oral", "английский устн", "speaking",
    ]),
    # Must be BEFORE generic "математика"
    ("Базовая математика", [
        "базовая математика", "база", "базов", "математика база", "матем база",
        "базовая", "математика (база)", "математика(база)",
    ]),
    ("Профильная математика", [
        "профильная математика", "профиль", "профильн", "математика профил",
        "матем профил", "профильная", "математика (профиль)", "математика(профиль)",
    ]),
    # Generic matches (after specials)
    ("Русский язык", [
        "русский язык", "русский", "русск", "рус", "русскому", "русского",
    ]),
    ("Обществознание", [
        "обществознание", "обществозн", "общество", "обществ", "обществ.", "общага",
    ]),
    ("Английский", [
        "английский", "английск", "англ", "english",
    ]),
    ("Математика", [
        "математика", "математик", "матем", "мат",
    ]),
    ("История", ["история", "истори", "ист"]),
    ("Биология", ["биология", "биологи", "биол"]),
    ("Химия", ["химия", "химии", "хим"]),
    ("Физика", ["физика", "физик", "физ"]),
    ("Информатика", ["информатика", "информатик", "инф"]),
    ("География", ["география", "географи", "геогр"]),
    ("Литература", ["литература", "литератур", "лит"]),
]

# Build lookup: alias_lowered → canonical_name
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in _CANONICAL_DEFS:
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias.lower()] = _canon


def canonical_key(raw: str) -> str:
    """Return a stable lowercase key for deduplication comparison."""
    return re.sub(r"[^а-яёa-z0-9]", "", raw.lower().strip())


def normalize_subject(raw: str) -> str:
    """
    Normalize a free-text subject name to its canonical form.

    Examples:
        "русский" → "Русский язык"
        "база" → "Базовая математика"
        "общество" → "Обществознание"
        "письменный английский" → "Письменный английский"
        "Обществознание" → "Обществознание" (already canonical)
    """
    if not raw or not raw.strip():
        return raw or ""

    lowered = raw.lower().strip()

    # Exact match first
    if lowered in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[lowered]

    # Partial match: check if lowered starts with or contains an alias
    # Sort by alias length descending so longer aliases match first
    for alias, canon in sorted(_ALIAS_TO_CANONICAL.items(), key=lambda x: -len(x[0])):
        if lowered.startswith(alias) or alias in lowered:
            return canon

    # Fallback: capitalize first letter
    words = raw.strip().split()
    return words[0].capitalize() if words else raw


def subjects_are_same(a: str, b: str) -> bool:
    """Return True if two subject strings normalize to the same canonical."""
    return canonical_key(normalize_subject(a)) == canonical_key(normalize_subject(b))


def find_best_exam_match(exams: list, raw_subject: str) -> "ExamDate | None":
    """
    Find the best matching ExamDate from a list for a given raw subject string.

    Matching priority:
    1. Exact canonical match
    2. canonical_key match
    3. Special rule: "Математика" → prefers "Базовая математика" if that's the only math

    Returns None if no match found.
    """
    if not exams:
        return None

    target_canon = normalize_subject(raw_subject)
    target_key = canonical_key(target_canon)

    # 1. Exact canonical match
    for exam in exams:
        if normalize_subject(exam.subject) == target_canon:
            return exam

    # 2. canonical_key match (ignores punctuation/spaces)
    for exam in exams:
        if canonical_key(normalize_subject(exam.subject)) == target_key:
            return exam

    # 3. Special: "Математика" (generic) → find any math exam
    if target_canon == "Математика":
        math_exams = [
            e for e in exams
            if "математик" in e.subject.lower() or "математик" in normalize_subject(e.subject).lower()
        ]
        if len(math_exams) == 1:
            return math_exams[0]
        # If both базовая and профильная → ambiguous, return None (caller should handle)
        if len(math_exams) > 1:
            return None

    return None


def find_duplicate_exam_pairs(
    exams: list,
) -> list[tuple]:
    """
    Find pairs of ExamDate objects that appear to be duplicates
    (same canonical subject, or one is a generic alias of the other).

    Returns list of (primary, duplicate) tuples where primary is the one to keep
    (usually the more specific / earlier created one).
    """
    pairs = []
    seen_ids = set()

    for i, exam_a in enumerate(exams):
        if exam_a.id in seen_ids:
            continue
        canon_a = normalize_subject(exam_a.subject)
        key_a = canonical_key(canon_a)

        for exam_b in exams[i + 1:]:
            if exam_b.id in seen_ids:
                continue
            canon_b = normalize_subject(exam_b.subject)
            key_b = canonical_key(canon_b)

            if key_a == key_b:
                # Same canonical → clear duplicate
                # Keep the more specific name (longer subject string) or earlier ID
                if len(exam_a.subject) >= len(exam_b.subject):
                    pairs.append((exam_a, exam_b))
                    seen_ids.add(exam_b.id)
                else:
                    pairs.append((exam_b, exam_a))
                    seen_ids.add(exam_a.id)
                break

            # Check if one is a generic alias of the other
            # e.g. "Математика" ↔ "Базовая математика"
            if _is_generic_alias_of(canon_a, canon_b):
                pairs.append((exam_b, exam_a))  # exam_b (specific) is primary
                seen_ids.add(exam_a.id)
                break
            elif _is_generic_alias_of(canon_b, canon_a):
                pairs.append((exam_a, exam_b))  # exam_a (specific) is primary
                seen_ids.add(exam_b.id)
                break

    return pairs


def _is_generic_alias_of(specific: str, generic: str) -> bool:
    """
    Return True if `generic` is a generic version of `specific`.
    E.g.: specific="Базовая математика", generic="Математика" → True
    """
    gen_key = canonical_key(generic)
    spec_key = canonical_key(specific)
    # "математика" is a prefix of "базоваяматематика"
    return len(gen_key) < len(spec_key) and spec_key.endswith(gen_key)
