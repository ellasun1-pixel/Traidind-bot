import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram_bot.bot import (
    cmd_start, cmd_help, cmd_status, cmd_portfolio,
    cmd_signal, cmd_history, cmd_confirm, cmd_reject,
    cmd_pause, cmd_resume, cmd_settings,
)
from src.config import settings, AgentMode


@pytest.fixture
def mock_update():
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 123456789
    update.effective_user.username = "test_owner"
    update.effective_chat = MagicMock()
    update.effective_chat.id = 123456789
    return update


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.args = []
    return context


@pytest.mark.asyncio
async def test_cmd_start(mock_update, mock_context):
    await cmd_start(mock_update, mock_context)
    mock_update.message.reply_text.assert_called_once()
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Paper Challenge" in text


@pytest.mark.asyncio
async def test_cmd_help(mock_update, mock_context):
    await cmd_help(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "/status" in text
    assert "/portfolio" in text
    assert "/confirm" in text
    assert "/pause" in text


@pytest.mark.asyncio
async def test_cmd_status(mock_update, mock_context):
    await cmd_status(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Balance" in text or "Status" in text


@pytest.mark.asyncio
async def test_cmd_portfolio(mock_update, mock_context):
    await cmd_portfolio(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Portfolio" in text


@pytest.mark.asyncio
async def test_cmd_signal_no_signals(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_last_signals", return_value={}):
        await cmd_signal(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "No signals" in text


@pytest.mark.asyncio
async def test_cmd_history_empty(mock_update, mock_context):
    from src.portfolio.manager import PaperPortfolio
    with patch("src.telegram_bot.bot.get_portfolio", return_value=PaperPortfolio()):
        await cmd_history(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "No completed" in text or "No trades" in text


@pytest.mark.asyncio
async def test_cmd_confirm_no_pending(mock_update, mock_context):
    mock_session = MagicMock()
    mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = []
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    with patch("src.telegram_bot.bot.get_session", return_value=mock_session):
        await cmd_confirm(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "No pending" in text


@pytest.mark.asyncio
async def test_cmd_reject_no_pending(mock_update, mock_context):
    mock_session = MagicMock()
    mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = []
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    with patch("src.telegram_bot.bot.get_session", return_value=mock_session):
        await cmd_reject(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "No pending" in text


@pytest.mark.asyncio
async def test_cmd_pause(mock_update, mock_context):
    original = settings.agent_mode
    await cmd_pause(mock_update, mock_context)
    assert settings.agent_mode == AgentMode.PAUSED
    text = mock_update.message.reply_text.call_args[0][0]
    assert "paused" in text.lower()
    settings.agent_mode = original


@pytest.mark.asyncio
async def test_cmd_resume(mock_update, mock_context):
    settings.agent_mode = AgentMode.PAUSED
    await cmd_resume(mock_update, mock_context)
    assert settings.agent_mode == AgentMode.PAPER_CHALLENGE
    text = mock_update.message.reply_text.call_args[0][0]
    assert "resumed" in text.lower()


@pytest.mark.asyncio
async def test_cmd_settings_view(mock_update, mock_context):
    await cmd_settings(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Settings" in text
    assert "Beginner" in text


@pytest.mark.asyncio
async def test_cmd_settings_toggle(mock_update, mock_context):
    original = settings.beginner_explanations
    mock_context.args = ["beginner", "false"]
    await cmd_settings(mock_update, mock_context)
    assert settings.beginner_explanations is False
    text = mock_update.message.reply_text.call_args[0][0]
    assert "False" in text
    settings.beginner_explanations = original
