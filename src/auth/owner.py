from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from src.config import settings

logger = logging.getLogger(__name__)

_UNAUTHORIZED_MSG = "This bot is private."


def get_owner_ids() -> set[int]:
    raw = settings.telegram_owner_ids
    if not raw or not raw.strip():
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def get_chat_ids() -> set[int]:
    raw = settings.telegram_chat_ids
    if not raw or not raw.strip():
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def _is_authorized(update: Update) -> bool:
    owner_ids = get_owner_ids()
    chat_ids = get_chat_ids()

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if user_id and user_id in owner_ids:
        return True
    if chat_id and chat_id in chat_ids:
        return True
    return False


def _log_unauthorized(update: Update, command: str) -> None:
    user = update.effective_user
    chat = update.effective_chat
    logger.warning(
        "Unauthorized access attempt: user_id=%s username=%s chat_id=%s command=%s",
        user.id if user else None,
        user.username if user else None,
        chat.id if chat else None,
        command,
    )
    try:
        from src.database import get_session, AuditLog
        with get_session() as session:
            session.add(AuditLog(
                action="UNAUTHORIZED_ACCESS",
                actor=str(user.id) if user else "unknown",
                detail={
                    "user_id": user.id if user else None,
                    "username": user.username if user else None,
                    "chat_id": chat.id if chat else None,
                    "command": command,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            ))
    except Exception:
        logger.debug("Could not write audit log for unauthorized access", exc_info=True)


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            command = update.message.text if update.message else "unknown"
            _log_unauthorized(update, command)
            if update.message:
                await update.message.reply_text(_UNAUTHORIZED_MSG)
            return
        return await func(update, context)
    return wrapper


def validate_auth_config() -> None:
    owner_ids = get_owner_ids()
    chat_ids = get_chat_ids()
    if not owner_ids and not chat_ids:
        raise ValueError(
            "No authorized users configured. "
            "Set TELEGRAM_OWNER_IDS and/or TELEGRAM_CHAT_IDS environment variables."
        )
    logger.info(
        "Auth configured: %d owner IDs, %d chat IDs",
        len(owner_ids), len(chat_ids),
    )
