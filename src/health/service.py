from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from src.health.models import HealthStatus, ComponentHealth, SystemHealth
from src.database.session import check_db_health, get_engine
from src.database import get_session
from src.database.repository import (
    SchedulerStateRepository, SignalRepository, PaperAccountRepository,
    HealthTransitionRepository, AuditLogRepository,
)
from src.config import settings

logger = logging.getLogger(__name__)

# Aggregation rules (documented):
#
# 1. Database UNHEALTHY -> Overall UNHEALTHY (nothing works without DB)
# 2. Scheduler UNHEALTHY -> Overall UNHEALTHY (no market checks = blind)
# 3. Any component UNHEALTHY -> Overall DEGRADED (at minimum)
# 4. Any component DEGRADED -> Overall DEGRADED
# 5. All components HEALTHY -> Overall HEALTHY


class HealthService:
    def __init__(self):
        self._last_statuses: dict[str, HealthStatus] = {}
        self._last_notification_hashes: dict[str, str] = {}
        self._send_message_func = None

    def set_send_message_func(self, func):
        self._send_message_func = func

    def check_database(self) -> ComponentHealth:
        try:
            result = check_db_health()
            if result["status"] == "ok":
                return ComponentHealth(
                    name="database",
                    status=HealthStatus.HEALTHY,
                    message=f"Connected ({result['backend']})",
                    checked_at=datetime.now(timezone.utc),
                )
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=f"Error: {result.get('error', 'unknown')}",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=f"Unreachable: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_scheduler(self) -> ComponentHealth:
        try:
            with get_session() as session:
                repo = SchedulerStateRepository(session)
                states = repo.get_all()

            if not states:
                return ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.UNHEALTHY,
                    message="No scheduler state found",
                    checked_at=datetime.now(timezone.utc),
                )

            now = datetime.now(timezone.utc)
            running = [s for s in states if s.current_status == "running"]
            stale_count = 0
            failed_jobs = []

            for s in states:
                last = s.last_success_at or s.last_run_at
                if last:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    age_minutes = (now - last).total_seconds() / 60
                    if age_minutes > 60:
                        stale_count += 1
                if s.last_error:
                    failed_jobs.append(s.job_name)

            if stale_count > 0 and stale_count == len(states):
                return ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.UNHEALTHY,
                    message=f"All {stale_count} jobs stale (>60 min since last run)",
                    checked_at=datetime.now(timezone.utc),
                )

            if failed_jobs:
                return ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.DEGRADED,
                    message=f"Jobs with errors: {', '.join(failed_jobs)}",
                    checked_at=datetime.now(timezone.utc),
                )

            if stale_count > 0:
                return ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.DEGRADED,
                    message=f"{stale_count}/{len(states)} jobs stale",
                    checked_at=datetime.now(timezone.utc),
                )

            active_count = sum(1 for s in states if s.run_count > 0)
            return ComponentHealth(
                name="scheduler",
                status=HealthStatus.HEALTHY,
                message=f"Running ({active_count} active jobs)",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="scheduler",
                status=HealthStatus.UNHEALTHY,
                message=f"Check failed: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_telegram(self) -> ComponentHealth:
        token = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id
        if not token or token.startswith("test_"):
            return ComponentHealth(
                name="telegram",
                status=HealthStatus.UNHEALTHY,
                message="Bot token not configured",
                checked_at=datetime.now(timezone.utc),
            )
        if not chat_id:
            return ComponentHealth(
                name="telegram",
                status=HealthStatus.DEGRADED,
                message="Chat ID not configured",
                checked_at=datetime.now(timezone.utc),
            )
        if self._send_message_func is None:
            return ComponentHealth(
                name="telegram",
                status=HealthStatus.DEGRADED,
                message="Send function not initialized",
                checked_at=datetime.now(timezone.utc),
            )
        return ComponentHealth(
            name="telegram",
            status=HealthStatus.HEALTHY,
            message="Connected",
            checked_at=datetime.now(timezone.utc),
        )

    def check_market_data(self) -> ComponentHealth:
        try:
            from src.scheduler.jobs import get_pipeline
            pipeline = get_pipeline()

            unhealthy_assets = []
            degraded_assets = []
            for asset_cfg in settings.assets:
                if not asset_cfg.active:
                    continue
                health = pipeline.get_health(asset_cfg.symbol)
                if health.latest_error:
                    unhealthy_assets.append(asset_cfg.symbol)
                elif health.candle_freshness_hours and health.candle_freshness_hours > settings.max_daily_candle_age_hours:
                    degraded_assets.append(asset_cfg.symbol)

            if unhealthy_assets:
                return ComponentHealth(
                    name="market_data",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Errors for: {', '.join(unhealthy_assets)}",
                    checked_at=datetime.now(timezone.utc),
                )
            if degraded_assets:
                return ComponentHealth(
                    name="market_data",
                    status=HealthStatus.DEGRADED,
                    message=f"Stale data for: {', '.join(degraded_assets)}",
                    checked_at=datetime.now(timezone.utc),
                )
            return ComponentHealth(
                name="market_data",
                status=HealthStatus.HEALTHY,
                message="All providers responding",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="market_data",
                status=HealthStatus.DEGRADED,
                message=f"Check failed: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_providers(self) -> ComponentHealth:
        try:
            from src.scheduler.jobs import get_pipeline
            pipeline = get_pipeline()

            kraken_ok = False
            coinbase_ok = False
            for asset_cfg in settings.assets:
                if not asset_cfg.active:
                    continue
                health = pipeline.get_health(asset_cfg.symbol)
                if health.current_provider == "kraken":
                    kraken_ok = True
                elif health.current_provider == "coinbase":
                    coinbase_ok = True

            if kraken_ok:
                return ComponentHealth(
                    name="providers",
                    status=HealthStatus.HEALTHY,
                    message=f"Kraken: OK, Coinbase: {'OK' if coinbase_ok else 'standby'}",
                    checked_at=datetime.now(timezone.utc),
                )
            if coinbase_ok:
                return ComponentHealth(
                    name="providers",
                    status=HealthStatus.DEGRADED,
                    message="Kraken: unavailable, Coinbase: OK (fallback active)",
                    checked_at=datetime.now(timezone.utc),
                )
            return ComponentHealth(
                name="providers",
                status=HealthStatus.UNHEALTHY,
                message="No providers responding",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="providers",
                status=HealthStatus.DEGRADED,
                message=f"Check failed: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_signal_engine(self) -> ComponentHealth:
        try:
            with get_session() as session:
                sig_repo = SignalRepository(session)
                pending = sig_repo.get_pending()
                pending_count = len(pending)

            return ComponentHealth(
                name="signal_engine",
                status=HealthStatus.HEALTHY,
                message=f"{pending_count} pending signal(s)",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="signal_engine",
                status=HealthStatus.UNHEALTHY,
                message=f"Check failed: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_paper_trading(self) -> ComponentHealth:
        try:
            with get_session() as session:
                acct_repo = PaperAccountRepository(session)
                account = acct_repo.get_or_create()
                balance = float(account.balance_usd)
                status_val = account.challenge_status

            if balance <= settings.loss_level:
                return ComponentHealth(
                    name="paper_trading",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Balance ${balance:.2f} at/below loss boundary ${settings.loss_level:.2f}",
                    checked_at=datetime.now(timezone.utc),
                )
            if status_val != "active":
                return ComponentHealth(
                    name="paper_trading",
                    status=HealthStatus.DEGRADED,
                    message=f"Challenge status: {status_val}",
                    checked_at=datetime.now(timezone.utc),
                )
            return ComponentHealth(
                name="paper_trading",
                status=HealthStatus.HEALTHY,
                message=f"Balance ${balance:.2f}, challenge active",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="paper_trading",
                status=HealthStatus.UNHEALTHY,
                message=f"Check failed: {e}",
                checked_at=datetime.now(timezone.utc),
            )

    def check_all(self) -> SystemHealth:
        checks = [
            self.check_database(),
            self.check_scheduler(),
            self.check_telegram(),
            self.check_market_data(),
            self.check_providers(),
            self.check_signal_engine(),
            self.check_paper_trading(),
        ]

        system = SystemHealth(
            status=HealthStatus.HEALTHY,
            checked_at=datetime.now(timezone.utc),
        )
        for c in checks:
            system.add(c)

        system.status = self._aggregate(system)
        return system

    def _aggregate(self, system: SystemHealth) -> HealthStatus:
        db = system.get("database")
        if db and db.status == HealthStatus.UNHEALTHY:
            return HealthStatus.UNHEALTHY

        scheduler = system.get("scheduler")
        if scheduler and scheduler.status == HealthStatus.UNHEALTHY:
            return HealthStatus.UNHEALTHY

        has_unhealthy = any(
            c.status == HealthStatus.UNHEALTHY
            for c in system.components.values()
        )
        if has_unhealthy:
            return HealthStatus.DEGRADED

        has_degraded = any(
            c.status == HealthStatus.DEGRADED
            for c in system.components.values()
        )
        if has_degraded:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY

    def run_check_and_record(self) -> SystemHealth:
        system = self.check_all()

        try:
            with get_session() as session:
                repo = HealthTransitionRepository(session)
                for name, component in system.components.items():
                    old_status = self._last_statuses.get(name)
                    if old_status is None:
                        self._last_statuses[name] = component.status
                        continue
                    if component.status != old_status:
                        repo.record(
                            component=name,
                            old_status=old_status.value,
                            new_status=component.status.value,
                            reason=component.message,
                        )
                        if component.status == HealthStatus.HEALTHY and old_status != HealthStatus.HEALTHY:
                            repo.mark_recovery(name)
                        self._last_statuses[name] = component.status

                old_overall = self._last_statuses.get("system")
                if old_overall is not None and system.status != old_overall:
                    repo.record(
                        component="system",
                        old_status=old_overall.value,
                        new_status=system.status.value,
                        reason=self._overall_reason(system),
                    )
                    if system.status == HealthStatus.HEALTHY and old_overall != HealthStatus.HEALTHY:
                        repo.mark_recovery("system")
                self._last_statuses["system"] = system.status

        except Exception as e:
            logger.error("Failed to record health transition: %s", e)

        return system

    def _overall_reason(self, system: SystemHealth) -> str:
        unhealthy = [n for n, c in system.components.items()
                     if c.status == HealthStatus.UNHEALTHY]
        degraded = [n for n, c in system.components.items()
                    if c.status == HealthStatus.DEGRADED]
        parts = []
        if unhealthy:
            parts.append(f"unhealthy: {', '.join(unhealthy)}")
        if degraded:
            parts.append(f"degraded: {', '.join(degraded)}")
        return "; ".join(parts) if parts else "all components healthy"

    async def notify_if_needed(self, system: SystemHealth):
        if self._send_message_func is None:
            return

        notifications = []

        for name, component in system.components.items():
            old_status = self._last_statuses.get(name)
            if old_status is None:
                continue

            if component.status == HealthStatus.UNHEALTHY and old_status != HealthStatus.UNHEALTHY:
                notifications.append(
                    f"UNHEALTHY: {name} — {component.message}"
                )
            elif component.status == HealthStatus.HEALTHY and old_status == HealthStatus.UNHEALTHY:
                notifications.append(
                    f"RECOVERED: {name} — {component.message}"
                )

        if not notifications:
            return

        msg_body = "\n".join(notifications)
        msg_hash = hashlib.md5(msg_body.encode()).hexdigest()

        if msg_hash in self._last_notification_hashes.values():
            return

        self._last_notification_hashes[system.status.value] = msg_hash

        message = f"*System Health Alert*\n\n{msg_body}"
        try:
            await self._send_message_func(message)
        except Exception as e:
            logger.error("Failed to send health notification: %s", e)

    def format_health_command(self, system: SystemHealth) -> str:
        status_emoji = {
            HealthStatus.HEALTHY: "\U0001f7e2",
            HealthStatus.DEGRADED: "\U0001f7e1",
            HealthStatus.UNHEALTHY: "\U0001f534",
        }

        lines = [
            "\U0001f3e5 *System Health*",
            "",
            f"Status: {status_emoji[system.status]} {system.status.value}",
            f"Environment: {settings.app_env.capitalize()}",
            "",
        ]

        for name, c in system.components.items():
            emoji = status_emoji[c.status]
            lines.append(f"{emoji} *{name.replace('_', ' ').title()}*: {c.message}")

        lines.append("")
        lines.append(f"Strategy Version: {settings.strategy_version}")
        lines.append(f"Live Trading: {'Enabled' if settings.live_trading_enabled else 'Disabled'}")
        lines.append(f"Agent Mode: {settings.agent_mode.value}")

        try:
            with get_session() as session:
                sched_repo = SchedulerStateRepository(session)
                states = sched_repo.get_all()
                active_jobs = sum(1 for s in states if s.run_count > 0)
                lines.append(f"Active Jobs: {active_jobs}")

                market_check = None
                for s in states:
                    if s.job_name == "market_check":
                        market_check = s
                        break
                if market_check and market_check.last_success_at:
                    ts = market_check.last_success_at
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    lines.append(f"Last Market Update: {ts.strftime('%Y-%m-%d %H:%M UTC')}")

                sig_repo = SignalRepository(session)
                pending_count = len(sig_repo.get_pending())
                lines.append(f"Pending Signals: {pending_count}")
        except Exception:
            pass

        return "\n".join(lines)


_health_service: HealthService | None = None


def get_health_service() -> HealthService:
    global _health_service
    if _health_service is None:
        _health_service = HealthService()
    return _health_service
