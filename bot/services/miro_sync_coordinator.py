from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class MiroSyncCoordinator:
    """Debounces automatic Miro updates so normal bot actions stay fast."""

    def __init__(
        self,
        miro_service,
        session_factory,
        time_service,
        config,
        next_step_service,
        delay_seconds: float = 15.0,
    ):
        self.miro_service = miro_service
        self.session_factory = session_factory
        self.time_service = time_service
        self.config = config
        self.next_step_service = next_step_service
        self.delay_seconds = delay_seconds
        self._tasks: dict[int, asyncio.Task] = {}

    def schedule(self, user_id: int) -> None:
        if not getattr(self.config, "miro_sync_enabled", False):
            return
        # Miro credentials and board are deployment-global for now. Keep this
        # integration owner-only so an expanded Telegram allowlist cannot mix
        # several users' private tasks on one board.
        if user_id != self.config.allowed_user_id or not self.miro_service.is_configured():
            return
        current = self._tasks.get(user_id)
        if current and not current.done():
            current.cancel()
        self._tasks[user_id] = asyncio.create_task(self._run(user_id))

    async def _run(self, user_id: int) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
            async with self.session_factory() as session:
                await self.miro_service.sync_all(
                    user_id,
                    session,
                    self.time_service,
                    self.config,
                    self.next_step_service,
                )
                await session.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Automatic Miro sync failed for user %s", user_id)

    async def shutdown(self) -> None:
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
