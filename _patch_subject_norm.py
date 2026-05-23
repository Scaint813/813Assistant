#!/usr/bin/env python3
"""
Fix subject_normalizer.py:
1. Fix _is_generic_alias_of logic — "Математика" IS generic alias OF "Базовая математика", not the other way
2. Fix find_duplicate_exam_pairs — primary should be the MORE SPECIFIC name
3. Fix find_best_exam_match — "Математика" with only one math exam → return that exam (don't fall through)
"""

path = "bot/services/subject_normalizer.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# Fix _is_generic_alias_of docstring and logic
OLD_IS_GENERIC = '''def _is_generic_alias_of(specific: str, generic: str) -> bool:
    """
    Return True if `generic` is a generic version of `specific`.
    E.g.: specific="Базовая математика", generic="Математика" → True
    """
    gen_key = canonical_key(generic)
    spec_key = canonical_key(specific)
    # "математика" is a prefix of "базоваяматематика"
    return len(gen_key) < len(spec_key) and spec_key.endswith(gen_key)'''

NEW_IS_GENERIC = '''def _is_generic_alias_of(specific: str, generic: str) -> bool:
    """
    Return True if `generic` is a generic/ambiguous version of `specific`.

    E.g.:
        specific="Базовая математика", generic="Математика" → True
        specific="Русский язык", generic="Математика" → False

    Rule: generic key must be a suffix of specific key
    ("математика" is a suffix of "базоваяматематика")
    """
    gen_key = canonical_key(normalize_subject(generic))
    spec_key = canonical_key(normalize_subject(specific))
    if not gen_key or not spec_key:
        return False
    # generic is shorter AND specific ends with the generic key
    return len(gen_key) < len(spec_key) and spec_key.endswith(gen_key)'''

if OLD_IS_GENERIC in content:
    content = content.replace(OLD_IS_GENERIC, NEW_IS_GENERIC, 1)
    print("OK: fixed _is_generic_alias_of")
else:
    print("WARN: _is_generic_alias_of not found")

# Fix find_duplicate_exam_pairs: primary = specific (longer/more precise), duplicate = generic (shorter)
OLD_PAIRS = '''    for i, exam_a in enumerate(exams):
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

    return pairs'''

NEW_PAIRS = '''    for i, exam_a in enumerate(exams):
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
                # Same canonical → clear duplicate.
                # PRIMARY = the one with longer/more specific subject string.
                if len(exam_a.subject) >= len(exam_b.subject):
                    pairs.append((exam_a, exam_b))  # keep a, archive b
                    seen_ids.add(exam_b.id)
                else:
                    pairs.append((exam_b, exam_a))  # keep b, archive a
                    seen_ids.add(exam_a.id)
                break

            # "Математика" is generic alias of "Базовая математика":
            # _is_generic_alias_of(specific="Базовая математика", generic="Математика") → True
            if _is_generic_alias_of(canon_a, canon_b):
                # canon_a is specific (e.g. "Базовая математика"), canon_b is generic ("Математика")
                # PRIMARY = exam_a (more specific), DUPLICATE = exam_b (generic)
                pairs.append((exam_a, exam_b))
                seen_ids.add(exam_b.id)
                break
            elif _is_generic_alias_of(canon_b, canon_a):
                # canon_b is specific, canon_a is generic
                # PRIMARY = exam_b (more specific), DUPLICATE = exam_a (generic)
                pairs.append((exam_b, exam_a))
                seen_ids.add(exam_a.id)
                break

    return pairs'''

if OLD_PAIRS in content:
    content = content.replace(OLD_PAIRS, NEW_PAIRS, 1)
    print("OK: fixed find_duplicate_exam_pairs primary/duplicate order")
else:
    print("WARN: find_duplicate_exam_pairs loop not found")

# Fix find_best_exam_match — "Математика" when only Базовая exists should match
OLD_MATCH_GENERIC = '''    # 3. Special: "Математика" (generic) → find any math exam
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

    return None'''

NEW_MATCH_GENERIC = '''    # 3. Special: "Математика" (generic) → find any math exam if only one exists
    if target_canon == "Математика":
        math_exams = [
            e for e in exams
            if "математик" in e.subject.lower() or "математик" in normalize_subject(e.subject).lower()
        ]
        if len(math_exams) == 1:
            return math_exams[0]
        # If multiple math exams → ambiguous, return None (caller should handle)
        if len(math_exams) > 1:
            return None

    # 4. Fuzzy: generic target matches specific exam via _is_generic_alias_of
    # E.g. target="Математика" matches exam.subject="Базовая математика"
    for exam in exams:
        exam_canon = normalize_subject(exam.subject)
        if _is_generic_alias_of(exam_canon, target_canon):
            # target_canon is generic version of exam_canon → this is the right exam
            return exam

    return None'''

if OLD_MATCH_GENERIC in content:
    content = content.replace(OLD_MATCH_GENERIC, NEW_MATCH_GENERIC, 1)
    print("OK: fixed find_best_exam_match generic fuzzy matching")
else:
    print("WARN: find_best_exam_match generic block not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
