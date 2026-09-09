from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import get_config
from bot.database.migrations import configure_engine, create_schema
from bot.database.queries import get_or_create_runtime_state
from bot.handlers import (
    academic,
    domains,
    menu,
    operations,
    planning,
    productivity,
    quick_note,
    reminders,
    start,
    voice,
)
from bot.handlers import health as health_handler
from bot.handlers import settings as settings_handler
from bot.middlewares import AccessMiddleware
from bot.services.action_log_service import ActionLogService
from bot.services.ai_service import AIService
from bot.services.assistant_ux_service import AssistantUXService
from bot.services.calendar_service import CalendarService
from bot.services.checkin_service import CheckinService
from bot.services.cleanup_service import CleanupService
from bot.services.conflict_service import ConflictService, RouteTimeEstimator
from bot.services.conversation_repair_service import ConversationRepairService
from bot.services.conversation_service import ConversationService
from bot.services.daily_brief_service import DailyBriefService
from bot.services.day_planning_service import DayPlanningService
from bot.services.focus_service import FocusService
from bot.services.health_bridge import HealthBridgeServer
from bot.services.health_service import HealthService
from bot.services.hse_calendar_service import HSECalendarService
from bot.services.intent_parser import IntentParser
from bot.services.metric_service import MetricService
from bot.services.mini_app_server import MiniAppServer
from bot.services.miro_service import MiroService
from bot.services.miro_sync_coordinator import MiroSyncCoordinator
from bot.services.navigation_service import NavigationService
from bot.services.next_step_service import NextStepService
from bot.services.overload_service import OverloadService
from bot.services.preferences_service import PreferencesService
from bot.services.problem_block_service import ProblemBlockService
from bot.services.problem_resources_service import ProblemResourcesService
from bot.services.project_service import ProjectService
from bot.services.quality_service import QualityService
from bot.services.reliability_service import ReliabilityService
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.screen_service import ScreenService
from bot.services.task_prioritization_service import TaskPrioritizationService
from bot.services.time_service import TimeService
from bot.services.training_service import TrainingService
from bot.services.transcription_service import TranscriptionService
from bot.services.user_profile_service import UserProfileService


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    # ── 1. Config ──────────────────────────────────────────────────────────
    cfg = get_config()
    log.info(
        "Config loaded. Bot ID=%s, owner=%s, allowed_users=%s/%s",
        cfg.bot_id,
        cfg.allowed_user_id,
        len(cfg.allowed_user_ids),
        cfg.max_allowed_users,
    )

    # ── 2. DB / schema ─────────────────────────────────────────────────────
    engine = create_async_engine(cfg.database_url, echo=False)
    configure_engine(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)
    log.info("Database ready: %s", cfg.database_url)

    # ── 3. Bot & Dispatcher ────────────────────────────────────────────────
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать или обновить подсказки"),
            BotCommand(command="help", description="Примеры обычных фраз"),
            BotCommand(command="settings", description="Настройки ассистента"),
            BotCommand(command="brief", description="Сводка дня"),
            BotCommand(command="hse_status", description="Статус расписания ВШЭ"),
        ])
    except Exception:
        log.exception("Could not update the compact Telegram command menu")

    # ── 4. Middlewares ─────────────────────────────────────────────────────
    dp.message.middleware(AccessMiddleware(cfg.allowed_user_ids))
    dp.callback_query.middleware(AccessMiddleware(cfg.allowed_user_ids))

    # ── 5. Handlers ────────────────────────────────────────────────────────
    dp.include_router(start.router)
    dp.include_router(academic.router)
    dp.include_router(health_handler.router)
    dp.include_router(settings_handler.router)
    dp.include_router(productivity.router)
    dp.include_router(reminders.router)
    dp.include_router(menu.router)
    dp.include_router(operations.router)
    dp.include_router(planning.router)
    dp.include_router(domains.router)
    dp.include_router(voice.router)
    dp.include_router(quick_note.router)

    # ── 6. Services ────────────────────────────────────────────────────────
    time_service = TimeService(cfg.timezone, cfg.morning_time, cfg.day_time, cfg.evening_time, cfg.night_time)
    user_profile_service = UserProfileService(cfg.timezone)
    calendar_service = CalendarService(time_service)
    day_planning_service = DayPlanningService(calendar_service)
    route_time_estimator = RouteTimeEstimator(
        cfg.default_transfer_buffer_minutes
    )
    conflict_service = ConflictService(route_time_estimator)
    daily_brief_service = DailyBriefService(calendar_service, conflict_service)
    hse_calendar_service = HSECalendarService(
        calendar_service,
        session_factory,
        time_service,
        cfg.allowed_user_id,
        feed_url=cfg.hse_ical_url,
        enabled=cfg.hse_ical_sync_enabled,
        sync_interval_minutes=cfg.hse_ical_sync_interval_minutes,
    )
    ai_service = AIService(
        cfg.openai_api_key,
        cfg.openai_model_fast,
        cfg.openai_model_smart,
        cfg.openai_model,
        reasoning_fast=cfg.openai_reasoning_fast,
        reasoning_smart=cfg.openai_reasoning_smart,
        max_output_fast=cfg.openai_max_output_fast,
        max_output_smart=cfg.openai_max_output_smart,
    )
    intent_parser = IntentParser(ai_service, time_service)
    transcription_service = TranscriptionService(cfg.openai_api_key, cfg.transcription_model)
    miro_service = MiroService(cfg.miro_token, cfg.miro_board_id, cfg.miro_ai_zone_start_x, cfg.miro_ai_zone_start_y)
    problem_block_service = ProblemBlockService()
    problem_resources_service = ProblemResourcesService()
    next_step_service = NextStepService()
    overload_service = OverloadService()

    # ── 7. Scheduler ───────────────────────────────────────────────────────
    reminder_scheduler = ReminderScheduler(
        cfg.timezone, bot, session_factory, cfg.allowed_user_ids
    )
    await reminder_scheduler.start_scheduler()
    log.info("APScheduler started")
    hse_sync_scheduled = hse_calendar_service.schedule(
        reminder_scheduler.scheduler
    )
    log.info(
        "HSE iCal periodic sync: %s",
        "enabled" if hse_sync_scheduled else "disabled; .ics upload remains available",
    )

    # Создаём preference до регистрации check-in jobs. На чистой БД значение
    # берётся из env; дальше им управляет пользовательская кнопка без рестарта.
    timezone_by_user = {}
    async with session_factory() as session:
        for user_id in cfg.allowed_user_ids:
            profile = await user_profile_service.get(session, user_id)
            timezone_by_user[user_id] = profile.timezone
            await get_or_create_runtime_state(
                session,
                user_id,
                default_checkin_enabled=cfg.checkin_enabled,
            )
        await session.commit()

    checkin_service = CheckinService(
        reminder_scheduler.scheduler,
        session_factory,
        time_service,
        problem_block_service,
        next_step_service,
        overload_service,
        cfg.allowed_user_ids,
        cfg.checkin_enabled,
        cfg.checkin_morning_time,
        cfg.checkin_day_time,
        cfg.checkin_evening_time,
    )
    checkin_service.schedule_daily_checkins(bot, timezone_by_user)
    log.info(
        "Check-ins: %s | morning=%s day=%s evening=%s",
        "enabled by default" if cfg.checkin_enabled else "disabled by default (user can enable)",
        cfg.checkin_morning_time,
        cfg.checkin_day_time,
        cfg.checkin_evening_time,
    )

    action_log_service = ActionLogService()
    metric_service = MetricService()
    quality_service = QualityService(metric_service)
    conversation_repair_service = ConversationRepairService(time_service)
    focus_service = FocusService()
    task_prioritization_service = TaskPrioritizationService()
    assistant_ux_service = AssistantUXService(task_prioritization_service, calendar_service)
    project_service = ProjectService()
    conversation_service = ConversationService(
        assistant_ux_service,
        project_service,
        next_step_service,
        daily_brief_service=daily_brief_service,
        conflict_service=conflict_service,
    )
    training_service = TrainingService(calendar_service)
    health_service = HealthService(
        reminder_scheduler.scheduler,
        session_factory,
        time_service,
        bot,
        cfg.allowed_user_id,
        cfg.health_bridge_enabled,
        cfg.health_nudge_time,
        cfg.default_step_goal,
        cfg.health_min_step_gap,
    )
    health_service.schedule_daily_nudge()
    reliability_service = ReliabilityService(
        reminder_scheduler.scheduler,
        session_factory,
        time_service,
        cfg.database_url,
        cfg.allowed_user_id,
        cfg.allowed_user_ids,
        cfg.backup_dir,
        cfg.backup_retention_days,
        cfg.reliability_enabled,
        quality_service=quality_service,
    )
    reliability_service.schedule(bot)
    miro_sync_coordinator = MiroSyncCoordinator(
        miro_service,
        session_factory,
        time_service,
        cfg,
        next_step_service,
    )
    health_bridge = None
    if cfg.health_bridge_enabled:
        if not cfg.health_bridge_token:
            log.warning("HEALTH_BRIDGE_ENABLED=true but HEALTH_BRIDGE_TOKEN is missing; bridge disabled")
        else:
            health_bridge = HealthBridgeServer(
                cfg.health_bridge_host,
                cfg.health_bridge_port,
                cfg.health_bridge_token,
                cfg.allowed_user_id,
                session_factory,
                health_service,
                calendar_service,
            )
            await health_bridge.start()

    mini_app_server = None
    if cfg.mini_app_enabled:
        mini_app_url = cfg.mini_app_public_url.rstrip("/") + "/"
        parsed_mini_app_url = urlparse(mini_app_url)
        if parsed_mini_app_url.scheme != "https" or not parsed_mini_app_url.netloc:
            log.error(
                "MINI_APP_ENABLED=true but MINI_APP_PUBLIC_URL is not a valid HTTPS URL; "
                "Mini App disabled"
            )
        else:
            mini_app_server = MiniAppServer(
                cfg.mini_app_host,
                cfg.mini_app_port,
                cfg.bot_token,
                cfg.allowed_user_ids,
                session_factory,
                time_service,
                user_profile_service,
                calendar_service,
                conflict_service,
                hse_calendar_service,
                calendar_bridge_token=cfg.calendar_bridge_token,
                calendar_bridge_owner_id=cfg.allowed_user_id,
                calendar_bridge_public_url=(
                    mini_app_url + "bridge/v1/hse-calendar"
                ),
            )
            await mini_app_server.start()
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Управление",
                        web_app=WebAppInfo(url=mini_app_url),
                    )
                )
                log.info("Telegram Mini App menu button configured")
            except Exception:
                log.exception("Could not configure the Telegram Mini App menu button")

    # ── 8. DI wiring ───────────────────────────────────────────────────────
    dp["config"] = cfg
    dp["session_factory"] = session_factory
    dp["intent_parser"] = intent_parser
    dp["transcription_service"] = transcription_service
    dp["cleanup_service"] = CleanupService()
    dp["time_service"] = time_service
    dp["user_profile_service"] = user_profile_service
    dp["miro_service"] = miro_service
    dp["reminder_scheduler"] = reminder_scheduler
    dp["navigation_service"] = NavigationService()
    dp["overload_service"] = overload_service
    dp["problem_block_service"] = problem_block_service
    dp["problem_resources_service"] = problem_resources_service
    dp["next_step_service"] = next_step_service
    dp["checkin_service"] = checkin_service
    dp["screen_service"] = ScreenService()
    dp["preferences_service"] = PreferencesService()
    dp["action_log_service"] = action_log_service
    dp["metric_service"] = metric_service
    dp["quality_service"] = quality_service
    dp["conversation_repair_service"] = conversation_repair_service
    dp["focus_service"] = focus_service
    dp["calendar_service"] = calendar_service
    dp["day_planning_service"] = day_planning_service
    dp["conflict_service"] = conflict_service
    dp["daily_brief_service"] = daily_brief_service
    dp["hse_calendar_service"] = hse_calendar_service
    dp["task_prioritization_service"] = task_prioritization_service
    dp["assistant_ux_service"] = assistant_ux_service
    dp["project_service"] = project_service
    dp["conversation_service"] = conversation_service
    dp["reliability_service"] = reliability_service
    dp["miro_sync_coordinator"] = miro_sync_coordinator
    dp["health_service"] = health_service
    dp["training_service"] = training_service

    log.info(
        "Services: OpenAI=%s | Miro=%s | Transcription=%s",
        "set" if cfg.openai_api_key else "missing (fallback parser)",
        "set" if miro_service.is_configured() else "missing (sync disabled)",
        "set" if cfg.openai_api_key else "missing",
    )
    log.info("813Assistant started. Polling...")

    # ── 9. Polling ─────────────────────────────────────────────────────────
    try:
        await dp.start_polling(bot)
    finally:
        await miro_sync_coordinator.shutdown()
        if health_bridge:
            await health_bridge.stop()
        if mini_app_server:
            await mini_app_server.stop()
        reminder_scheduler.shutdown_scheduler()
        log.info("Scheduler stopped. Bot shutdown.")


if __name__ == "__main__":
    asyncio.run(main())
