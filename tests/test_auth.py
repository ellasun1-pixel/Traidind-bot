import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.owner import (
    owner_only, get_owner_ids, get_chat_ids,
    validate_auth_config, _is_authorized,
)
from src.config import settings


OWNER_ID = 123456789
STRANGER_ID = 999999999
AUTHORIZED_CHAT_ID = 123456789


@pytest.fixture(autouse=True)
def _restore_settings():
    original_owner_ids = settings.telegram_owner_ids
    original_chat_ids = settings.telegram_chat_ids
    yield
    settings.telegram_owner_ids = original_owner_ids
    settings.telegram_chat_ids = original_chat_ids


def _make_update(user_id: int, chat_id: int | None = None, username: str = "testuser"):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id or user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = "/test"
    return update


def _make_context():
    context = MagicMock()
    context.args = []
    return context


class TestGetOwnerIds:
    def test_parses_single_id(self):
        settings.telegram_owner_ids = "123"
        assert get_owner_ids() == {123}

    def test_parses_multiple_ids(self):
        settings.telegram_owner_ids = "111,222,333"
        assert get_owner_ids() == {111, 222, 333}

    def test_handles_whitespace(self):
        settings.telegram_owner_ids = " 111 , 222 , 333 "
        assert get_owner_ids() == {111, 222, 333}

    def test_empty_string_returns_empty(self):
        settings.telegram_owner_ids = ""
        assert get_owner_ids() == set()

    def test_whitespace_only_returns_empty(self):
        settings.telegram_owner_ids = "   "
        assert get_owner_ids() == set()

    def test_ignores_non_numeric(self):
        settings.telegram_owner_ids = "123,abc,456"
        assert get_owner_ids() == {123, 456}


class TestGetChatIds:
    def test_parses_single_id(self):
        settings.telegram_chat_ids = "123"
        assert get_chat_ids() == {123}

    def test_parses_negative_group_id(self):
        settings.telegram_chat_ids = "-100123456789"
        assert get_chat_ids() == {-100123456789}

    def test_parses_mixed_ids(self):
        settings.telegram_chat_ids = "111,-100222"
        assert get_chat_ids() == {111, -100222}

    def test_empty_returns_empty(self):
        settings.telegram_chat_ids = ""
        assert get_chat_ids() == set()


class TestIsAuthorized:
    def test_owner_id_authorized(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        update = _make_update(OWNER_ID)
        assert _is_authorized(update) is True

    def test_stranger_not_authorized(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        update = _make_update(STRANGER_ID)
        assert _is_authorized(update) is False

    def test_authorized_chat_id(self):
        settings.telegram_owner_ids = ""
        settings.telegram_chat_ids = str(AUTHORIZED_CHAT_ID)
        update = _make_update(STRANGER_ID, chat_id=AUTHORIZED_CHAT_ID)
        assert _is_authorized(update) is True

    def test_unauthorized_chat_id(self):
        settings.telegram_owner_ids = ""
        settings.telegram_chat_ids = str(AUTHORIZED_CHAT_ID)
        update = _make_update(STRANGER_ID, chat_id=888888888)
        assert _is_authorized(update) is False

    def test_owner_in_unauthorized_chat(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = str(AUTHORIZED_CHAT_ID)
        update = _make_update(OWNER_ID, chat_id=888888888)
        assert _is_authorized(update) is True


class TestOwnerOnlyDecorator:
    @pytest.mark.asyncio
    async def test_authorized_user_passes_through(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock(return_value="ok")
        decorated = owner_only(inner)
        update = _make_update(OWNER_ID)
        context = _make_context()
        result = await decorated(update, context)
        inner.assert_called_once_with(update, context)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = owner_only(inner)
        update = _make_update(STRANGER_ID)
        context = _make_context()
        await decorated(update, context)
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_gets_private_message(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = owner_only(inner)
        update = _make_update(STRANGER_ID)
        context = _make_context()
        await decorated(update, context)
        update.message.reply_text.assert_called_once_with("This bot is private.")

    @pytest.mark.asyncio
    async def test_unauthorized_no_information_leak(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = owner_only(inner)
        update = _make_update(STRANGER_ID)
        context = _make_context()
        await decorated(update, context)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Paper" not in reply_text
        assert "Challenge" not in reply_text
        assert "command" not in reply_text.lower()
        assert "help" not in reply_text.lower()


class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_unauthorized_attempt_logged(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = owner_only(inner)
        update = _make_update(STRANGER_ID, username="hacker")
        update.message.text = "/status"
        context = _make_context()

        with patch("src.database.get_session") as mock_session:
            mock_ctx = MagicMock()
            mock_session.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            mock_session.return_value.__exit__ = MagicMock(return_value=False)
            await decorated(update, context)

            mock_ctx.add.assert_called_once()
            audit_entry = mock_ctx.add.call_args[0][0]
            assert audit_entry.action == "UNAUTHORIZED_ACCESS"
            assert audit_entry.actor == str(STRANGER_ID)
            assert audit_entry.detail["user_id"] == STRANGER_ID
            assert audit_entry.detail["username"] == "hacker"
            assert audit_entry.detail["command"] == "/status"

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_response(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        inner = AsyncMock()
        decorated = owner_only(inner)
        update = _make_update(STRANGER_ID)
        context = _make_context()

        with patch("src.database.get_session", side_effect=Exception("db down")):
            await decorated(update, context)

        update.message.reply_text.assert_called_once_with("This bot is private.")
        inner.assert_not_called()


class TestValidateAuthConfig:
    def test_raises_when_no_ids_configured(self):
        settings.telegram_owner_ids = ""
        settings.telegram_chat_ids = ""
        with pytest.raises(ValueError, match="No authorized users"):
            validate_auth_config()

    def test_passes_with_owner_ids(self):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        validate_auth_config()

    def test_passes_with_chat_ids_only(self):
        settings.telegram_owner_ids = ""
        settings.telegram_chat_ids = str(AUTHORIZED_CHAT_ID)
        validate_auth_config()


class TestAllCommandsProtected:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd_name", [
        "cmd_start", "cmd_help", "cmd_status", "cmd_portfolio",
        "cmd_signal", "cmd_history", "cmd_confirm", "cmd_reject",
        "cmd_pause", "cmd_resume", "cmd_settings",
    ])
    async def test_command_blocked_for_stranger(self, cmd_name):
        settings.telegram_owner_ids = str(OWNER_ID)
        settings.telegram_chat_ids = ""
        from src.telegram_bot import bot
        handler = getattr(bot, cmd_name)
        update = _make_update(STRANGER_ID)
        context = _make_context()
        await handler(update, context)
        update.message.reply_text.assert_called_once_with("This bot is private.")


class TestCreateBotAuthValidation:
    def test_create_bot_rejects_empty_allowlist(self):
        settings.telegram_owner_ids = ""
        settings.telegram_chat_ids = ""
        with pytest.raises(ValueError, match="No authorized users"):
            from src.telegram_bot.bot import create_bot
            create_bot(token="fake_token")
