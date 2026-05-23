#!/usr/bin/env python3
"""
Append new exam/schedule query functions to bot/database/queries.py.
"""

path = "bot/database/queries.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

APPEND = '''

# -- Smart exam upsert / archive / dedupe ------------------------------------

async def upsert_exam_date_smart(session, user_id, subject, exam_date, normalizer=None):
    """
    Upsert exam date using canonical subject matching.

    Returns (exam, op) where op is "created" | "updated".
    If normalizer is provided, subject is normalized before lookup.
    """
    from sqlalchemy import select
    from bot.database.models import ExamDate
    from bot.services.subject_normalizer import normalize_subject, find_best_exam_match

    canonical_subject = normalize_subject(subject) if normalizer is None else normalizer(subject)

    # Load all active exams for this user
    res = await session.execute(
        select(ExamDate).where(ExamDate.user_id == user_id)
    )
    all_exams = list(res.scalars().all())

    # Find best match using normalizer
    existing = find_best_exam_match(all_exams, canonical_subject)

    if existing:
        old_date = existing.exam_date
        existing.exam_date = exam_date
        existing.status = "active"
        await session.flush()
        return existing, "updated"

    # Create new
    row = ExamDate(user_id=user_id, subject=canonical_subject, exam_date=exam_date)
    session.add(row)
    await session.flush()
    return row, "created"


async def get_exam_date_by_id(session, user_id, exam_id):
    """Return ExamDate by id for user_id, or None."""
    from sqlalchemy import and_, select
    from bot.database.models import ExamDate
    res = await session.execute(
        select(ExamDate).where(and_(ExamDate.id == exam_id, ExamDate.user_id == user_id))
    )
    return res.scalar_one_or_none()


async def archive_exam_date(session, user_id, exam_id, notes="deduplicated"):
    """Set exam_date status to 'archived'. Returns the exam or None if not found."""
    exam = await get_exam_date_by_id(session, user_id, exam_id)
    if exam:
        exam.status = "archived"
        if notes:
            exam.notes = notes
        await session.flush()
    return exam


async def find_duplicate_exams(session, user_id):
    """
    Find duplicate ExamDate pairs for user using subject normalizer.

    Returns list of (primary_exam, duplicate_exam) tuples.
    primary = the one to keep (more specific / earlier).
    duplicate = the one to archive.
    """
    from sqlalchemy import select
    from bot.database.models import ExamDate
    from bot.services.subject_normalizer import find_duplicate_exam_pairs

    res = await session.execute(
        select(ExamDate).where(
            ExamDate.user_id == user_id,
            ExamDate.status == "active",
        ).order_by(ExamDate.id.asc())
    )
    all_exams = list(res.scalars().all())
    return find_duplicate_exam_pairs(all_exams)


async def archive_study_schedule_item(session, user_id, item_id, notes=""):
    """Set study_schedule_item status to 'archived'. Returns item or None."""
    from sqlalchemy import and_, select
    from bot.database.models import StudyScheduleItem
    res = await session.execute(
        select(StudyScheduleItem).where(
            and_(StudyScheduleItem.id == item_id, StudyScheduleItem.user_id == user_id)
        )
    )
    item = res.scalar_one_or_none()
    if item:
        item.status = "archived"
        if notes:
            item.notes = notes
        await session.flush()
    return item
'''

if "upsert_exam_date_smart" in content:
    print("Functions already present, skipping append.")
else:
    content = content.rstrip() + "\n" + APPEND
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("OK: appended smart exam upsert/archive/dedupe functions")
