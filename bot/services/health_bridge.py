from __future__ import annotations

import hmac
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class HealthBridgeServer:
    def __init__(
        self, host: str, port: int, token: str, user_id: int, session_factory,
        health_service, calendar_service=None,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.user_id = user_id
        self.session_factory = session_factory
        self.health_service = health_service
        self.calendar_service = calendar_service
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=512 * 1024)
        app.router.add_post("/api/health/snapshot", self._snapshot)
        app.router.add_get("/api/health/status", self._status)
        app.router.add_post("/api/calendar/events", self._calendar_events)
        app.router.add_get("/api/calendar/status", self._calendar_status)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        logger.info("Health bridge listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def _authorized(self, request: web.Request) -> bool:
        value = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return bool(self.token) and hmac.compare_digest(value, expected)

    async def _status(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        return web.json_response({"status": "ok"})

    async def _snapshot(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("JSON object expected")
            async with self.session_factory() as session:
                row = await self.health_service.upsert_snapshot(session, self.user_id, payload)
                await session.commit()
                snapshot_id = row.id
                snapshot_date = row.date.isoformat()
        except (ValueError, TypeError, web.HTTPBadRequest) as exc:
            raise web.HTTPBadRequest(text=f"invalid snapshot: {exc}") from exc
        return web.json_response({"status": "saved", "id": snapshot_id, "date": snapshot_date})

    async def _calendar_status(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        return web.json_response({"status": "ok", "enabled": self.calendar_service is not None})

    async def _calendar_events(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        if self.calendar_service is None:
            raise web.HTTPServiceUnavailable(text="calendar service disabled")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("JSON object expected")
            async with self.session_factory() as session:
                count = await self.calendar_service.replace_events(session, self.user_id, payload)
                await session.commit()
        except (ValueError, TypeError, web.HTTPBadRequest) as exc:
            raise web.HTTPBadRequest(text=f"invalid calendar payload: {exc}") from exc
        return web.json_response({"status": "saved", "events": count})
