"""Calendar event manager — fetches, caches, and triggers alerts."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from src.database import get_session
from src.database.models import CalendarEvent
from src.calendar.providers import MarketEvent, fetch_all_events, init_crypto_map
from src.config import settings

logger = logging.getLogger(__name__)

ALERT_WINDOWS_HOURS = [24, 1]


class CalendarManager:
    def __init__(self):
        self._initialized = False

    def _ensure_init(self) -> None:
        if not self._initialized:
            symbols = [a.symbol for a in settings.assets]
            init_crypto_map(symbols)
            self._initialized = True

    async def refresh_events(self) -> int:
        self._ensure_init()
        events = await fetch_all_events(days_ahead=14)
        stored = 0
        try:
            with get_session() as session:
                for ev in events:
                    existing = (
                        session.query(CalendarEvent)
                        .filter(CalendarEvent.unique_key == ev.unique_key)
                        .first()
                    )
                    if existing:
                        existing.event_time = ev.event_time
                        existing.impact = ev.impact
                        existing.description = ev.description
                        existing.forecast = ev.forecast
                        existing.previous = ev.previous
                    else:
                        session.add(CalendarEvent(
                            unique_key=ev.unique_key,
                            title=ev.title,
                            event_time=ev.event_time,
                            category=ev.category,
                            source=ev.source,
                            impact=ev.impact,
                            currency=ev.currency,
                            asset_symbol=ev.asset_symbol,
                            description=ev.description,
                            forecast=ev.forecast,
                            previous=ev.previous,
                        ))
                        stored += 1
                session.flush()
        except Exception as e:
            logger.error("Failed to store calendar events: %s", e)

        logger.info("Calendar refresh: %d new events stored, %d total fetched", stored, len(events))
        return stored

    def get_upcoming(self, hours_ahead: int = 48) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        try:
            with get_session() as session:
                events = (
                    session.query(CalendarEvent)
                    .filter(CalendarEvent.event_time >= now)
                    .filter(CalendarEvent.event_time <= cutoff)
                    .order_by(CalendarEvent.event_time.asc())
                    .all()
                )
                result = []
                for ev in events:
                    session.expunge(ev)
                    result.append(ev)
                return result
        except Exception as e:
            logger.error("Failed to query upcoming events: %s", e)
            return []

    def get_events_needing_alert(self) -> list[tuple[CalendarEvent, int]]:
        now = datetime.now(timezone.utc)
        alerts: list[tuple[CalendarEvent, int]] = []

        try:
            with get_session() as session:
                for hours in ALERT_WINDOWS_HOURS:
                    window_start = now + timedelta(hours=hours) - timedelta(minutes=15)
                    window_end = now + timedelta(hours=hours) + timedelta(minutes=15)

                    col_name = f"alerted_{hours}h"

                    events = (
                        session.query(CalendarEvent)
                        .filter(CalendarEvent.event_time >= window_start)
                        .filter(CalendarEvent.event_time <= window_end)
                        .all()
                    )

                    for ev in events:
                        already_alerted = getattr(ev, col_name, False)
                        if not already_alerted:
                            setattr(ev, col_name, True)
                            alerts.append((ev, hours))

                session.flush()

                for ev, _ in alerts:
                    session.expunge(ev)
        except Exception as e:
            logger.error("Failed to check alert windows: %s", e)

        return alerts


def format_event_alert(event: CalendarEvent, hours_before: int) -> str:
    time_str = event.event_time.strftime("%b %d, %H:%M UTC")
    impact_emoji = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "⚪"}.get(
        event.impact, "⚪"
    )
    category_emoji = "\U0001f4c8" if event.category == "macro" else "\U0001f4b0"

    lines = [
        f"{category_emoji} *Market Event Alert* — {hours_before}h before",
        "",
        f"{impact_emoji} *{event.title}*",
        f"\U0001f4c5 {time_str}",
    ]

    if event.asset_symbol:
        lines.append(f"\U0001f4b1 Asset: {event.asset_symbol}")
    if event.description:
        lines.append(f"\U0001f4dd {event.description}")
    if event.forecast:
        lines.append(f"\U0001f52e Forecast: {event.forecast}")
    if event.previous:
        lines.append(f"⏪ Previous: {event.previous}")

    lines.append(f"\U0001f4e1 Source: {event.source}")

    return "\n".join(lines)


def format_calendar_view(events: list[CalendarEvent]) -> str:
    if not events:
        return "\U0001f4c5 Market Calendar\n\nNo upcoming events in the next 48 hours."

    lines = ["\U0001f4c5 Market Calendar — Next 48h\n"]

    current_date = None
    for ev in events:
        date_str = ev.event_time.strftime("%A, %b %d")
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n{date_str}")

        time_str = ev.event_time.strftime("%H:%M UTC")
        impact_emoji = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "⚪"}.get(
            ev.impact, "⚪"
        )
        category_tag = "[MACRO]" if ev.category == "macro" else "[CRYPTO]"

        entry = f"  {impact_emoji} {time_str}  {category_tag} {ev.title}"
        if ev.asset_symbol:
            entry += f" ({ev.asset_symbol})"
        lines.append(entry)

    lines.append(f"\nTotal: {len(events)} events")
    return "\n".join(lines)


_calendar_manager: CalendarManager | None = None


def get_calendar_manager() -> CalendarManager:
    global _calendar_manager
    if _calendar_manager is None:
        _calendar_manager = CalendarManager()
    return _calendar_manager
