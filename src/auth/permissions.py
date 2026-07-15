from __future__ import annotations

import functools
import logging
from enum import Enum

from telegram import Update
from telegram.ext import ContextTypes

from src.auth.owner import _is_authorized, _log_unauthorized, _UNAUTHORIZED_MSG

logger = logging.getLogger(__name__)


class Permission(str, Enum):
    READ = "READ"
    TRADING = "TRADING"
    TRADE_CONFIRM = "TRADE_CONFIRM"
    PORTFOLIO = "PORTFOLIO"
    REPORTS = "REPORTS"
    SETTINGS = "SETTINGS"
    ADMIN = "ADMIN"


ALL_PERMISSIONS = frozenset(Permission)

_PERMISSION_DENIED_MSG = "This bot is private."


def get_user_permissions(user_id: int) -> frozenset[Permission]:
    from src.auth.owner import get_owner_ids
    if user_id in get_owner_ids():
        return ALL_PERMISSIONS
    return frozenset()


def has_permission(user_id: int, permission: Permission) -> bool:
    return permission in get_user_permissions(user_id)


def requires_permission(*permissions: Permission):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not _is_authorized(update):
                command = update.message.text if update.message else "unknown"
                _log_unauthorized(update, command)
                if update.message:
                    await update.message.reply_text(_UNAUTHORIZED_MSG)
                return

            user_id = update.effective_user.id if update.effective_user else None
            if user_id is None:
                if update.message:
                    await update.message.reply_text(_PERMISSION_DENIED_MSG)
                return

            user_perms = get_user_permissions(user_id)
            missing = set(permissions) - user_perms
            if missing:
                if update.message:
                    await update.message.reply_text(_PERMISSION_DENIED_MSG)
                return

            return await func(update, context)
        return wrapper
    return decorator
