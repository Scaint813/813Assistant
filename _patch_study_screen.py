#!/usr/bin/env python3
"""
Patch menu.py section_study to show compact /study output:
- Short exam format: "Базов — 08.06, 17 дн."
- Only next 4 lessons by weekday sorted from today
- study_blocks short: "• Русский, зад.21"
- "Следующий шаг: /next"
"""

path = "bot/handlers/menu.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

OLD_STUDY_BODY = '''    lines = ["УЧЁБА", ""]

    if exams:
        lines.append("Экзамены:")
        for i, e in enumerate(exams, 1):
            days_left = (e.exam_date - today).days
            days_str = f", осталось {days_left} дн." if days_left >= 0 else " (прошло)"
            lines.append(f"{i}. {e.subject} — {e.exam_date.strftime('%d.%m.%Y')}{days_str}")
        lines.append("")
    else:
        lines += ["Экзамены:", "— ещё не зафиксированы.", ""]

    if schedule:
        _WD = {"mon": "понедельник", "tue": "вторник", "wed": "среда",
               "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье"}
        lines.append("Занятия:")
        for i, s in enumerate(schedule, 1):
            wd = _WD.get(s.weekday or "", s.weekday or "")
            t = s.time_str or ""
            tutor = f", {s.tutor_name}" if s.tutor_name else ""
            lines.append(f"{i}. {s.subject} — {wd}, {t}{tutor}")
        lines.append("")
    else:
        lines += ["Занятия с репетитором: не зафиксированы.", ""]

    if study_blocks:
        lines.append("Активные блоки:")
        for i, b in enumerate(study_blocks[:3], 1):
            lines.append(f"{i}. {b.title}")
            if b.next_action:
                lines.append(f"   Следующий шаг: {b.next_action[:80]}")
        lines.append("")
    else:
        lines += ["Активные блоки: нет.", ""]

    if not exams and not schedule and not study_blocks:
        lines = [
            "УЧЁБА", "",
            "Данные ещё не зафиксированы.", "",
            "Можно написать:",
            '"ЕГЭ по обществу 10 июня, проблема с 24 заданием"',
            '"репетитор по английскому по вторникам в 18:00"',
        ]
    else:
        lines += ["Быстрый ввод:", '"проблема с 24 заданием по обществу"', '"ЕГЭ по русскому 3 июня"']'''

NEW_STUDY_BODY = '''    # ── Compact /study format ────────────────────────────────────────────────

    # Short subject abbreviations
    def _short_subj(s: str) -> str:
        MAP = {
            "Русский язык": "Русский",
            "Обществознание": "Общество",
            "Базовая математика": "База",
            "Профильная математика": "Профиль",
            "Письменный английский": "Англ. письм.",
            "Устный английский": "Англ. устный",
            "Английский": "Английский",
        }
        return MAP.get(s, s[:15])

    lines = ["УЧЁБА", ""]

    if exams:
        lines.append("Экзамены:")
        for i, e in enumerate(exams, 1):
            days_left = (e.exam_date - today).days
            days_str = f", {days_left} дн." if days_left > 0 else (" (сегодня!)" if days_left == 0 else " (прошёл)")
            lines.append(f"{i}. {_short_subj(e.subject)} — {e.exam_date.strftime('%d.%m')}{days_str}")
        lines.append("")
    else:
        lines += ["Экзамены: не зафиксированы.", ""]

    if schedule:
        _WD_SHORT = {"mon": "пн", "tue": "вт", "wed": "ср",
                     "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}
        _WD_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_idx = today.weekday()  # 0=mon
        # Sort schedule by days from today
        def _days_until(item_wd: str) -> int:
            wd_idx = _WD_ORDER.index(item_wd) if item_wd in _WD_ORDER else 0
            d = (wd_idx - today_idx) % 7
            return d if d > 0 else 7
        sorted_sched = sorted(schedule, key=lambda s: _days_until(s.weekday or "mon"))
        show_sched = sorted_sched[:4]
        hidden = len(sorted_sched) - len(show_sched)
        lines.append("Занятия (ближайшие):")
        for s in show_sched:
            wd = _WD_SHORT.get(s.weekday or "", s.weekday or "")
            t = s.time_str or ""
            tutor = f", {s.tutor_name}" if s.tutor_name else ""
            lines.append(f"{wd} — {_short_subj(s.subject)} {t}{tutor}")
        if hidden > 0:
            lines.append(f"+ ещё {hidden}")
        lines.append("")
    else:
        lines += ["Занятия: не зафиксированы.", ""]

    if study_blocks:
        lines.append("Блоки:")
        for b in study_blocks[:3]:
            lines.append(f"• {b.title[:50]}")
        lines.append("")
    else:
        lines += ["Блоки: нет.", ""]

    if not exams and not schedule and not study_blocks:
        lines = [
            "УЧЁБА", "",
            "Данные ещё не зафиксированы.", "",
            "Напиши:",
            '"ЕГЭ по обществу 11 июня"',
            '"репетитор по англ по вторникам 18:00"',
            '"проблема с заданием 24 по обществу"',
        ]
    else:
        lines += ["Следующий шаг: /next"]'''

if OLD_STUDY_BODY in content:
    content = content.replace(OLD_STUDY_BODY, NEW_STUDY_BODY, 1)
    print("OK: replaced /study body with compact format")
else:
    print("WARN: study body not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
