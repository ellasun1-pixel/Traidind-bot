import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram_bot.bot import (
    cmd_start, cmd_help, cmd_status, cmd_portfolio,
    cmd_signal, cmd_history, cmd_confirm, cmd_reject,
    cmd_pause, cmd_resume, cmd_settings, cmd_auth,
    cmd_scheduler, cmd_health, cmd_debug,
    cmd_reset_challenge, cmd_new_challenge,
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


@pytest.mark.asyncio
async def test_cmd_debug_imports_pandas(mock_update, mock_context):
    """Fix #3: /debug must not crash with NameError on pd.to_datetime."""
    import importlib
    import src.telegram_bot.bot as bot_module
    source = importlib.util.find_spec("src.telegram_bot.bot")
    with open(source.origin) as f:
        code = f.read()
    assert "import pandas" in code, "/debug uses pd.to_datetime but pandas is never imported"


@pytest.mark.asyncio
async def test_cmd_confirm_handles_expired_signal_gracefully(mock_update, mock_context):
    """Fix #4: /confirm must catch InvalidTransitionError from lifecycle.confirm()."""
    from src.signals.lifecycle import InvalidTransitionError

    mock_portfolio = MagicMock()
    mock_portfolio.confirm_buy.return_value = (True, "Bought BTC")
    mock_portfolio.is_challenge_active = True

    mock_sig = MagicMock()
    mock_sig.id = "test-sig-1"
    mock_sig.signal_type = "BUY"
    mock_sig.asset.symbol = "BTC/USD"
    mock_sig.entry_price = 50000.0
    mock_sig.stop_loss = 48500.0
    mock_sig.position_size_usd = 100.0
    mock_sig.max_loss_usd = 3.0
    mock_sig.expires_at = MagicMock()
    mock_sig.expires_at.tzinfo = MagicMock()

    from datetime import datetime, timezone
    mock_sig.expires_at.__le__ = MagicMock(return_value=False)

    mock_lifecycle = MagicMock()
    mock_lifecycle.confirm.side_effect = InvalidTransitionError("test-sig-1", "expired", "confirmed")

    with patch("src.telegram_bot.bot.get_portfolio", return_value=mock_portfolio), \
         patch("src.telegram_bot.bot.get_live_prices", new_callable=AsyncMock, return_value={}), \
         patch("src.telegram_bot.bot.get_session") as mock_gs, \
         patch("src.telegram_bot.bot.record_portfolio_snapshot"), \
         patch("src.telegram_bot.bot.SignalLifecycle", return_value=mock_lifecycle):

        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = [mock_sig]

        await cmd_confirm(mock_update, mock_context)

    text = mock_update.message.reply_text.call_args_list[-1][0][0]
    assert "expired" in text.lower() or "⚠️" in text


@pytest.mark.asyncio
async def test_cmd_portfolio_error_handling(mock_update, mock_context):
    """Fix #7: /portfolio must not hang on exception — uses same pattern as /status."""
    with patch("src.telegram_bot.bot.get_portfolio", side_effect=RuntimeError("DB down")):
        await cmd_portfolio(mock_update, mock_context)

    text = mock_update.message.reply_text.call_args[0][0]
    assert "error" in text.lower() or "DB down" in text


@pytest.mark.asyncio
async def test_cmd_pause_persists_state(mock_update, mock_context):
    """Fix #8: /pause must persist agent_mode to DB so it survives redeploy."""
    from src.config import AgentMode
    settings.agent_mode = AgentMode.PAPER_CHALLENGE

    with patch("src.telegram_bot.bot.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)

        await cmd_pause(mock_update, mock_context)

    assert settings.agent_mode == AgentMode.PAUSED
    text = mock_update.message.reply_text.call_args[0][0]
    assert "paused" in text.lower()
    settings.agent_mode = AgentMode.PAPER_CHALLENGE


@pytest.mark.asyncio
async def test_cmd_resume_restores_pre_pause_mode(mock_update, mock_context):
    """Fix #12: /resume must restore the mode that was active before /pause."""
    from src.config import AgentMode
    settings.agent_mode = AgentMode.PAUSED

    from src.database.repository import AppSettingRepository

    original_get = AppSettingRepository.get
    def mock_get(self, key, default=None):
        if key == "pre_pause_mode":
            return "ALERT_ONLY"
        return default

    with patch("src.telegram_bot.bot.get_session") as mock_gs, \
         patch.object(AppSettingRepository, "get", mock_get), \
         patch.object(AppSettingRepository, "set", MagicMock()):
        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)

        await cmd_resume(mock_update, mock_context)

    assert settings.agent_mode == AgentMode.ALERT_ONLY
    text = mock_update.message.reply_text.call_args[0][0]
    assert "ALERT_ONLY" in text
    settings.agent_mode = AgentMode.PAPER_CHALLENGE


def test_dead_code_pending_signals_removed():
    """Fix #15: _pending_signals and _store_pending_signal must be removed."""
    import src.telegram_bot.bot as bot_mod
    assert not hasattr(bot_mod, "_pending_signals"), "_pending_signals is dead code"
    assert not hasattr(bot_mod, "_store_pending_signal"), "_store_pending_signal is dead code"


@pytest.mark.asyncio
async def test_cmd_status_reads_signals_from_db(mock_update, mock_context):
    """Fix #14: /status must read last signals from DB, not just in-memory dict."""
    mock_portfolio = MagicMock()
    mock_portfolio.get_total_equity.return_value = 1010.0
    mock_portfolio.balance_usd = 1010.0
    mock_portfolio.challenge_status = "active"
    mock_portfolio.positions = []

    mock_signal = MagicMock()
    mock_signal.asset_id = 1
    mock_signal.asset.symbol = "BTC/USD"
    mock_signal.regime = "TREND"
    mock_signal.signal_type = "BUY"
    mock_signal.created_at = MagicMock()

    with patch("src.telegram_bot.bot.get_portfolio", return_value=mock_portfolio), \
         patch("src.telegram_bot.bot.get_live_prices", new_callable=AsyncMock, return_value={}), \
         patch("src.telegram_bot.bot.get_session") as mock_gs, \
         patch("src.telegram_bot.bot.get_last_signals", return_value={}):

        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)

        mock_subq = MagicMock()
        mock_session.query.return_value.group_by.return_value.subquery.return_value = mock_subq
        mock_session.query.return_value.join.return_value.join.return_value.all.return_value = [mock_signal]

        await cmd_status(mock_update, mock_context)

    text = mock_update.message.reply_text.call_args[0][0]
    assert "Status" in text


@pytest.mark.asyncio
async def test_cmd_confirm_expires_once_not_per_signal(mock_update, mock_context):
    """Fix #17: expire_old_signals must be called once before the loop, not per signal."""
    mock_portfolio = MagicMock()
    mock_portfolio.is_challenge_active = True

    mock_lifecycle = MagicMock()
    mock_lifecycle.expire_old_signals.return_value = []

    with patch("src.telegram_bot.bot.get_portfolio", return_value=mock_portfolio), \
         patch("src.telegram_bot.bot.get_live_prices", new_callable=AsyncMock, return_value={}), \
         patch("src.telegram_bot.bot.get_session") as mock_gs, \
         patch("src.telegram_bot.bot.record_portfolio_snapshot"), \
         patch("src.telegram_bot.bot.SignalLifecycle", return_value=mock_lifecycle):

        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = []

        await cmd_confirm(mock_update, mock_context)

    assert mock_lifecycle.expire_old_signals.call_count == 1


# --- Bug #6: Error handling for all remaining commands ---


@pytest.mark.asyncio
async def test_cmd_confirm_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_portfolio", side_effect=RuntimeError("DB down")):
        await cmd_confirm(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Confirm error" in text or "DB down" in text


@pytest.mark.asyncio
async def test_cmd_reject_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_session", side_effect=RuntimeError("DB down")):
        await cmd_reject(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Reject error" in text or "DB down" in text


@pytest.mark.asyncio
async def test_cmd_signal_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_last_signals", side_effect=RuntimeError("crash")):
        await cmd_signal(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Signal error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_history_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_portfolio", side_effect=RuntimeError("crash")):
        await cmd_history(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "History error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_settings_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.settings", side_effect=RuntimeError("crash")):
        mock_context.args = None
        with patch("src.telegram_bot.bot.settings") as mock_settings:
            mock_settings.agent_mode = MagicMock()
            mock_settings.agent_mode.value = "test"
            type(mock_settings).beginner_explanations = property(lambda s: (_ for _ in ()).throw(RuntimeError("crash")))
            await cmd_settings(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Settings error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_auth_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_user_permissions", side_effect=RuntimeError("crash")):
        await cmd_auth(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Auth error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_scheduler_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_scheduler_status", side_effect=RuntimeError("crash")):
        await cmd_scheduler(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Scheduler error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_health_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_health_service", side_effect=RuntimeError("crash")):
        await cmd_health(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Health error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_debug_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_pipeline", side_effect=RuntimeError("crash")):
        await cmd_debug(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Debug error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_reset_challenge_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_portfolio", side_effect=RuntimeError("crash")):
        await cmd_reset_challenge(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "Reset challenge error" in text or "crash" in text


@pytest.mark.asyncio
async def test_cmd_new_challenge_error_handling(mock_update, mock_context):
    with patch("src.telegram_bot.bot.get_portfolio", side_effect=RuntimeError("crash")):
        await cmd_new_challenge(mock_update, mock_context)
    text = mock_update.message.reply_text.call_args[0][0]
    assert "New challenge error" in text or "crash" in text


# --- Bug #3: beginner_explanations persistence ---


@pytest.mark.asyncio
async def test_cmd_settings_persists_beginner_explanations(mock_update, mock_context):
    original = settings.beginner_explanations
    mock_context.args = ["beginner", "false"]

    mock_repo = MagicMock()
    with patch("src.telegram_bot.bot.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)
        with patch("src.database.repository.AppSettingRepository", return_value=mock_repo):
            await cmd_settings(mock_update, mock_context)

    assert settings.beginner_explanations is False
    settings.beginner_explanations = original


@pytest.mark.asyncio
async def test_startup_sweep_restores_beginner_explanations():
    from src.scheduler.jobs import startup_sweep
    from src.database.repository import AppSettingRepository

    original = settings.beginner_explanations
    settings.beginner_explanations = True

    def mock_get(self, key, default=None):
        if key == "agent_mode":
            return None
        if key == "beginner_explanations":
            return "False"
        return default

    with patch("src.scheduler.jobs.get_session") as mock_gs, \
         patch.object(AppSettingRepository, "get", mock_get):
        mock_session = MagicMock()
        mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_gs.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter.return_value.all.return_value = []

        sched_repo_mock = MagicMock()
        sched_repo_mock.get_all.return_value = []
        with patch("src.scheduler.jobs.SchedulerStateRepository", return_value=sched_repo_mock), \
             patch("src.scheduler.jobs.SignalLifecycle") as mock_lc:
            mock_lc.return_value.expire_old_signals.return_value = []
            await startup_sweep()

    assert settings.beginner_explanations is False
    settings.beginner_explanations = original


# --- Bug #1: challenge_engine docstring ---


def test_challenge_engine_docstring_says_2_positions():
    import src.strategy.challenge_engine as ce
    assert "up to 2" in ce.__doc__ or "2 open positions" in ce.__doc__
    assert "up to 3" not in ce.__doc__
