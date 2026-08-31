from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.keyboards.inline import overflow_proposal_keyboard

router = Router()


async def _user_now(
    session, user_id: int, time_service, user_profile_service,
):
    return await user_profile_service.now_for(session, user_id, time_service)


@router.callback_query(F.data == "plan_overflow_preview")
async def plan_overflow_preview(
    callback: CallbackQuery, session_factory, time_service,
    user_profile_service, day_planning_service,
):
    await callback.answer("Считаю следующие дни")
    async with session_factory() as session:
        now = await _user_now(
            session, callback.from_user.id, time_service, user_profile_service
        )
        data = await day_planning_service.build_overflow_proposal(
            session, callback.from_user.id, now
        )
        await session.commit()
    await callback.message.edit_text(
        day_planning_service.render_proposal(data),
        reply_markup=overflow_proposal_keyboard(bool(data["proposal"])),
    )


@router.callback_query(F.data == "plan_overflow_confirm")
async def plan_overflow_confirm(
    callback: CallbackQuery, session_factory, time_service,
    user_profile_service, day_planning_service,
):
    await callback.answer("Сохраняю рабочие блоки")
    async with session_factory() as session:
        now = await _user_now(
            session, callback.from_user.id, time_service, user_profile_service
        )
        data = await day_planning_service.apply_overflow_proposal(
            session, callback.from_user.id, now
        )
        await session.commit()
    await callback.message.edit_text(
        day_planning_service.render_proposal(data, saved=True),
        reply_markup=overflow_proposal_keyboard(False),
    )


@router.callback_query(F.data == "plan_overflow_cancel")
async def plan_overflow_cancel(callback: CallbackQuery):
    await callback.answer("Оставил без изменений")
    await callback.message.edit_text(
        "План не изменён.\n\n"
        "Можно вернуться к нему позже или сначала изменить дневную вместимость в профиле.",
        reply_markup=overflow_proposal_keyboard(False),
    )
