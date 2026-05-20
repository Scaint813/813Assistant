from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.database.queries import get_or_create_runtime_state

logger = logging.getLogger(__name__)


class ScreenService:
    """
    Manages a single 'screen' message per user.

    Rules:
    - Every navigation screen (HQ, next_step, lists) replaces the previous one.
    - Action Previews, reminder pushes, confirm results are NOT screens.
    - Safe: all Telegram errors during delete/edit are caught and logged.
    """

    async def delete_user_input(self, message) -> None:
        """
        Best-effort deletion of the user's incoming message.
        Call AFTER the bot has fully processed and responded to the message.
        Never raises — all Telegram errors are caught silently.
        """
        try:
            await message.bot.delete_message(message.chat.id, message.message_id)
        except Exception as exc:
            logger.debug("delete_user_input skipped (msg_id=%s): %s", message.message_id, exc)

    async def render_screen(
        self,
        bot: Bot,
        session,
        user_id: int,
        chat_id: int,
        text: str,
        reply_markup=None,
        parse_mode: str | None = None,
    ):
        """Delete old screen (if any), send new one, store message_id."""
        await self._delete_old_screen(bot, session, user_id)
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            await self._save_screen(session, user_id, chat_id, msg.message_id)
            return msg
        except TelegramBadRequest as exc:
            logger.warning("render_screen send failed for user %s: %s", user_id, exc)
            return None

    async def edit_or_render_screen(
        self,
        bot: Bot,
        session,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
    ):
        """
        Try to edit the current callback message.
        If not possible (e.g. same text, old message), fall back to render_screen.
        """
        try:
            msg = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            await self._save_screen(session, user_id, chat_id, message_id)
            return msg
        except TelegramBadRequest:
            # message not modified / too old / deleted — fall back
            return await self.render_screen(bot, session, user_id, chat_id, text, reply_markup)

    async def delete_last_screen(self, bot: Bot, session, user_id: int):
        """Explicitly delete the last screen and clear stored ids."""
        await self._delete_old_screen(bot, session, user_id)

    # ── private ───────────────────────────────────────────────────────────

    async def _delete_old_screen(self, bot: Bot, session, user_id: int):
        state = await get_or_create_runtime_state(session, user_id)
        if state.last_screen_chat_id and state.last_screen_message_id:
            try:
                await bot.delete_message(
                    chat_id=state.last_screen_chat_id,
                    message_id=state.last_screen_message_id,
                )
            except TelegramBadRequest as exc:
                # message already deleted / too old — ignore silently
                logger.debug("delete_old_screen ignored: %s", exc)
            state.last_screen_chat_id = None
            state.last_screen_message_id = None
            await session.flush()

    async def _save_screen(self, session, user_id: int, chat_id: int, message_id: int):
        state = await get_or_create_runtime_state(session, user_id)
        state.last_screen_chat_id = chat_id
        state.last_screen_message_id = message_id
        await session.flush()
