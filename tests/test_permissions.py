import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.permissions import (
    Permission, ALL_PERMISSIONS, get_user_permissions,
    has_permission, requires_permission,
)
from src.config import settings


OWNER_ID = 123456789
STRANGER_ID = 999999999


@pytest.fixture(autouse=True)
def _restore_settings():
    original_owner_ids = settings.telegram_owner_ids
    original_chat_ids = settings.telegram_chat_ids
    yield
    settings.telegram_owner_ids = original_owner_ids
    settings.telegram_chat_ids = original_chat_ids


def _make_update(user_id, chat_id=None):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "test"
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id or user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = "/test"
    return update


class TestPermissionEnum:
    def test_all_permissions_defined(self):
        expected = {"READ", "TRADING", "TRADE_CONFIRM", "PORTFOLIO", "REPORTS", "SETTINGS", "ADMIN"}
        actual = {p.value for p in Permission}
        assert expected == actual

    def test_all_permissions_constant(self):
        assert len(ALL_PERMISSIONS) == 7
        for p in Permission:
            assert p in ALL_PERMISSIONS


class TestGetUserPermissions:
    def test_owner_has_all_permissions(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        perms = get_user_permissions(OWNER_ID)
        assert perms == ALL_PERMISSIONS

    def test_unauthorized_user_has_no_permissions(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        perms = get_user_permissions(STRANGER_ID)
        assert perms == frozenset()

    def test_has_permission_owner(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        for p in Permission:
            assert has_permission(OWNER_ID, p) is True

    def test_has_permission_stranger(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        for p in Permission:
            assert has_permission(STRANGER_ID, p) is False


class TestRequiresPermissionDecorator:
    @pytest.mark.asyncio
    async def test_owner_passes_single_permission(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock(return_value="ok")
        decorated = requires_permission(Permission.READ)(inner)
        update = _make_update(OWNER_ID)
        context = MagicMock()
        result = await decorated(update, context)
        inner.assert_called_once()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_owner_passes_multiple_permissions(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock(return_value="ok")
        decorated = requires_permission(Permission.TRADING, Permission.ADMIN)(inner)
        update = _make_update(OWNER_ID)
        context = MagicMock()
        result = await decorated(update, context)
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_stranger_blocked(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = requires_permission(Permission.READ)(inner)
        update = _make_update(STRANGER_ID)
        context = MagicMock()
        await decorated(update, context)
        inner.assert_not_called()
        update.message.reply_text.assert_called_once_with("This bot is private.")

    @pytest.mark.asyncio
    async def test_can_extend_without_changing_handler(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""

        @requires_permission(Permission.REPORTS)
        async def handler(update, context):
            return "report_data"

        update = _make_update(OWNER_ID)
        context = MagicMock()
        result = await handler(update, context)
        assert result == "report_data"


class TestAuthCommand:
    @pytest.mark.asyncio
    async def test_auth_works_for_owner(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = str(OWNER_ID)
        from src.telegram_bot.bot import cmd_auth
        update = _make_update(OWNER_ID)
        context = MagicMock()
        context.args = []
        await cmd_auth(update, context)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Authorized" in text
        assert str(OWNER_ID) in text
        for p in Permission:
            assert p.value in text

    @pytest.mark.asyncio
    async def test_auth_blocked_for_stranger(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        from src.telegram_bot.bot import cmd_auth
        update = _make_update(STRANGER_ID)
        context = MagicMock()
        await cmd_auth(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert text == "This bot is private."

    @pytest.mark.asyncio
    async def test_auth_never_exposes_secrets(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = str(OWNER_ID)
        settings.telegram_bot_token = "secret_bot_token_12345"
        from src.telegram_bot.bot import cmd_auth
        update = _make_update(OWNER_ID)
        context = MagicMock()
        context.args = []
        await cmd_auth(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "secret_bot_token" not in text
        assert "token" not in text.lower()
        assert "password" not in text.lower()
        assert "key" not in text.lower()
        assert "credential" not in text.lower()

    @pytest.mark.asyncio
    async def test_auth_shows_environment(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = str(OWNER_ID)
        from src.telegram_bot.bot import cmd_auth
        update = _make_update(OWNER_ID)
        context = MagicMock()
        context.args = []
        await cmd_auth(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "Development" in text or "Production" in text

    @pytest.mark.asyncio
    async def test_auth_shows_db_status(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = str(OWNER_ID)
        from src.telegram_bot.bot import cmd_auth
        update = _make_update(OWNER_ID)
        context = MagicMock()
        context.args = []
        await cmd_auth(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "Database" in text
        assert "Connected" in text or "Error" in text
