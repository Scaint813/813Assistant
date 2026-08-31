from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from bot.database.models import Reminder, ReminderDelivery
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)


def render_system_notice(
    *,
    icon: str,
    title: str,
    summary: str,
    impact: str,
    action: str,
    details: list[str] | tuple[str, ...] = (),
) -> str:
    lines = [
        "⚙️ Служебное уведомление 813Assistant",
        f"{icon} {title}",
        "",
        "Что произошло:",
        summary,
        "",
        "Влияние:",
        impact,
        "",
        "Что делать:",
        action,
    ]
    if details:
        lines += ["", "Подробности:", *(f"• {item}" for item in details)]
    return "\n".join(lines)


class ReliabilityService:
    """Cheap operational checks and recoverable SQLite backups without AI calls."""

    def __init__(
        self,
        scheduler,
        session_factory,
        time_service,
        database_url: str,
        owner_user_id: int,
        allowed_user_ids: tuple[int, ...],
        backup_dir: str,
        retention_days: int = 14,
        enabled: bool = True,
        quality_service=None,
    ):
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.time_service = time_service
        self.database_url = database_url
        self.owner_user_id = owner_user_id
        self.allowed_user_ids = tuple(allowed_user_ids)
        self.backup_dir = Path(backup_dir)
        self.retention_days = max(1, retention_days)
        self.enabled = enabled
        self.quality_service = quality_service
        self._last_issues: tuple[str, ...] = ()
        self._last_backup_ok: bool | None = None
        self._last_quality_signature: tuple[str, ...] = ()

    def schedule(self, bot) -> None:
        if not self.enabled:
            return
        self.scheduler.add_job(
            self.run_check,
            "interval",
            minutes=15,
            args=[bot],
            id="system:reliability",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.create_backup,
            "cron",
            hour=3,
            minute=10,
            args=[bot],
            id="system:backup",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.verify_latest_backup,
            "cron",
            hour=3,
            minute=25,
            args=[bot],
            id="system:backup_verify",
            replace_existing=True,
            max_instances=1,
        )
        if self.quality_service is not None:
            self.scheduler.add_job(
                self.send_quality_digest,
                "cron",
                hour=8,
                minute=45,
                args=[bot],
                id="system:quality_slo",
                replace_existing=True,
                max_instances=1,
            )

    async def run_check(self, bot) -> list[str]:
        issues: list[str] = []
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
                result = await session.execute(
                    select(Reminder).where(
                        Reminder.user_id.in_(self.allowed_user_ids),
                        Reminder.status == "active",
                    )
                )
                now = self.time_service.now()
                for reminder in result.scalars().all():
                    remind_at = ensure_aware(reminder.remind_at, now.tzinfo)
                    if remind_at > now + timedelta(minutes=2) and not self.scheduler.get_job(f"reminder:{reminder.id}"):
                        issues.append(f"напоминание #{reminder.id} не запланировано")
                delivery_result = await session.execute(
                    select(ReminderDelivery).where(
                        ReminderDelivery.user_id.in_(self.allowed_user_ids),
                        ReminderDelivery.status.in_(("pending", "failed", "uncertain")),
                    )
                )
                for delivery in delivery_result.scalars().all():
                    updated_at = ensure_aware(delivery.updated_at, now.tzinfo)
                    if delivery.status == "pending" and updated_at and updated_at < now - timedelta(minutes=15):
                        issues.append(f"доставка {delivery.occurrence_key} зависла")
                    elif delivery.status == "failed" and delivery.attempts >= 3:
                        issues.append(f"доставка {delivery.occurrence_key} не удалась")
                    elif delivery.status == "uncertain":
                        issues.append(
                            f"доставка {delivery.occurrence_key} требует ручной проверки"
                        )
        except Exception as exc:
            logger.exception("Reliability DB check failed")
            issues.append(f"база данных недоступна: {type(exc).__name__}")
        if not self.scheduler.running:
            issues.append("планировщик остановлен")

        current = tuple(sorted(set(issues)))
        if current != self._last_issues:
            if current:
                await self._alert(
                    bot,
                    render_system_notice(
                        icon="🚨",
                        title="Нужна проверка работы бота",
                        summary="Автоматическая диагностика обнаружила техническую проблему.",
                        impact=(
                            "Часть напоминаний или фоновых функций может работать нестабильно. "
                            "Сохранённые задачи сами по себе не удалены."
                        ),
                        action=(
                            "Открой /health. Если статус не OK — проверь процесс бота и его логи."
                        ),
                        details=current,
                    ),
                )
            elif self._last_issues:
                await self._alert(
                    bot,
                    render_system_notice(
                        icon="✅",
                        title="Работа бота восстановлена",
                        summary=(
                            "База данных, планировщик и фоновые задания снова проходят проверку."
                        ),
                        impact="Бот и напоминания должны работать в обычном режиме.",
                        action="Ничего делать не нужно.",
                    ),
                )
            self._last_issues = current
        return list(current)

    async def create_backup(self, bot=None) -> Path | None:
        source = self._sqlite_path()
        if source is None or not source.exists():
            return None
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            target = self.backup_dir / f"assistant-{stamp}.sqlite3"
            with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
                source_db.backup(target_db)
            if not self.verify_backup(target):
                raise RuntimeError(f"Backup verification failed: {target}")
            cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
            for path in self.backup_dir.glob("assistant-*.sqlite3"):
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                    path.unlink(missing_ok=True)
            logger.info("SQLite backup created: %s", target)
            return target
        except Exception as exc:
            logger.exception("SQLite backup failed")
            if bot is not None:
                await self._alert(
                    bot,
                    render_system_notice(
                        icon="⚠️",
                        title="Свежая резервная копия не создана",
                        summary="Ночная копия базы данных завершилась ошибкой.",
                        impact=(
                            "Сам бот продолжает работать, но до следующей успешной копии "
                            "защита от потери данных слабее."
                        ),
                        action=(
                            "Проверь свободное место, права на каталог backup и логи процесса."
                        ),
                        details=(f"Тип ошибки: {type(exc).__name__}",),
                    ),
                )
            return None

    def verify_backup(self, backup_path: Path) -> bool:
        """Restore a backup into a temporary SQLite file and validate the restored DB."""
        try:
            with tempfile.TemporaryDirectory(prefix="813assistant-restore-") as directory:
                restored = Path(directory) / "restored.sqlite3"
                source_uri = f"file:{backup_path.resolve()}?mode=ro"
                with (
                    sqlite3.connect(source_uri, uri=True) as backup_db,
                    sqlite3.connect(restored) as restored_db,
                ):
                    backup_db.backup(restored_db)
                    integrity = restored_db.execute("PRAGMA integrity_check").fetchone()[0]
                    tables = {
                        row[0]
                        for row in restored_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                if integrity != "ok" or not tables:
                    return False
                if "user_profile" in tables:
                    required = {
                        "user_profile",
                        "tasks",
                        "reminders",
                        "user_runtime_state",
                        "schema_migrations",
                    }
                    if not required.issubset(tables):
                        return False
            return True
        except (OSError, sqlite3.Error):
            logger.exception("Backup restore verification failed: %s", backup_path)
            return False

    async def verify_latest_backup(self, bot=None) -> bool:
        backups = list(self.backup_dir.glob("assistant-*.sqlite3"))
        latest = max(backups, key=lambda path: path.stat().st_mtime) if backups else None
        ok = bool(latest and self.verify_backup(latest))
        if ok != self._last_backup_ok:
            if not ok and bot is not None:
                await self._alert(
                    bot,
                    render_system_notice(
                        icon="🚨",
                        title="Резервную копию нельзя подтвердить",
                        summary=(
                            "Последняя копия базы не прошла тестовое восстановление."
                        ),
                        impact=(
                            "Бот продолжает работать, но эту копию пока нельзя считать надёжной."
                        ),
                        action=(
                            "Проверь каталог backup и логи, затем создай и протестируй новую копию."
                        ),
                    ),
                )
            elif ok and self._last_backup_ok is False and bot is not None:
                await self._alert(
                    bot,
                    render_system_notice(
                        icon="✅",
                        title="Резервная копия снова исправна",
                        summary="Последняя копия успешно прошла тестовое восстановление.",
                        impact="Защита данных снова работает в обычном режиме.",
                        action="Ничего делать не нужно.",
                    ),
                )
            self._last_backup_ok = ok
        return ok

    async def send_quality_digest(self, bot) -> list[str]:
        if self.quality_service is None:
            return []
        violations: list[str] = []
        readable_violations: list[str] = []
        async with self.session_factory() as session:
            now = self.time_service.now()
            for user_id in self.allowed_user_ids:
                evaluation = await self.quality_service.evaluate_user(
                    session, user_id, now, days=7
                )
                label = "Ваш профиль" if user_id == self.owner_user_id else f"Профиль {user_id}"
                for item in evaluation["violations"]:
                    violations.append(f"пользователь {user_id}: {item}")
                    readable_violations.append(
                        f"{label}: {self._humanize_quality_violation(item)}"
                    )
        signature = tuple(sorted(violations))
        if signature and signature != self._last_quality_signature:
            await self._alert(
                bot,
                render_system_notice(
                    icon="⚠️",
                    title="Один из показателей качества ниже ориентира",
                    summary=(
                        "Автоматическая проверка сравнила работу бота за последние 7 дней "
                        "с внутренними ориентирами."
                    ),
                    impact=(
                        "Это не задача и не напоминание. Данные не потеряны; "
                        "бот может отвечать медленнее или иногда требовать исправления."
                    ),
                    action=(
                        "Ничего срочного. Если неудобство заметно в работе — открой /metrics "
                        "или проверь логи; иначе сообщение можно просто закрыть."
                    ),
                    details=readable_violations,
                ),
            )
        self._last_quality_signature = signature
        return violations

    @staticmethod
    def _humanize_quality_violation(value: str) -> str:
        parse_match = re.search(r"p95 разбора (\d+) мс", value)
        if parse_match:
            seconds = int(parse_match.group(1)) / 1000
            return (
                f"95% разборов завершались не дольше чем за {seconds:.1f} сек.; "
                "внутренний ориентир — до 8 сек."
            )
        delivery_match = re.search(r"p95 задержки напоминаний ([\d.]+) сек", value)
        if delivery_match:
            return (
                "95% напоминаний доставлялись не дольше чем за "
                f"{delivery_match.group(1)} сек.; ориентир — до 60 сек."
            )
        if value.startswith("исправляется "):
            return value.replace("исправляется", "приходится исправлять", 1)
        if value.startswith("одношаговые уточнения"):
            return value.replace(
                "одношаговые уточнения",
                "уточняющие вопросы",
                1,
            )
        if value.startswith("неудачных доставок напоминаний"):
            return value + ". Некоторые напоминания могли не дойти."
        return value

    def _sqlite_path(self) -> Path | None:
        prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
        for prefix in prefixes:
            if self.database_url.startswith(prefix):
                value = self.database_url[len(prefix):]
                if value == ":memory:":
                    return None
                return Path(value).expanduser().resolve()
        return None

    async def _alert(self, bot, text_value: str) -> None:
        try:
            await bot.send_message(self.owner_user_id, text_value)
        except Exception:
            logger.exception("Could not send reliability alert")
