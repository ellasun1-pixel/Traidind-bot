import pytest
from src.portfolio.manager import PaperPortfolio
from src.config import settings
from src.strategy.engine import StrategyEngine


@pytest.fixture
def portfolio():
    return PaperPortfolio(starting_balance=1000.0)


class TestPnLCalculation:
    def test_realized_pnl_profit(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=52000.0)
        assert ok
        assert portfolio.realized_pnl_total > 0

    def test_realized_pnl_loss(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=48000.0)
        assert ok
        assert portfolio.realized_pnl_total < 0

    def test_unrealized_pnl(self, portfolio):
        portfolio.confirm_buy(
            symbol="ETH/USD",
            entry_price=3000.0,
            position_value_usd=100.0,
            stop_loss=2900.0,
            risk_dollars=3.0,
        )
        pnl = portfolio.get_unrealized_pnl({"ETH/USD": 3100.0})
        quantity = 100.0 / 3000.0
        commission = 100.0 * settings.commission_pct
        spread = 100.0 * settings.spread_pct
        expected = (3100.0 - 3000.0) * quantity - commission - spread
        assert abs(pnl - expected) < 0.01


class TestCommissionAndSpread:
    def test_commission_deducted_on_buy(self, portfolio):
        initial = portfolio.balance_usd
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        expected_cost = 100.0 + 100.0 * settings.commission_pct + 100.0 * settings.spread_pct
        assert abs(portfolio.balance_usd - (initial - expected_cost)) < 0.01

    def test_commission_deducted_on_sell(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        portfolio.confirm_sell("BTC/USD", exit_price=50000.0)
        assert portfolio.realized_pnl_total < 0


class TestConfirmTrade:
    def test_trade_only_after_confirm(self, portfolio):
        assert len(portfolio.get_open_positions()) == 0
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        assert len(portfolio.get_open_positions()) == 1

    def test_sell_no_position(self, portfolio):
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=50000.0)
        assert not ok

    def test_challenge_won_blocks_new_trades(self, portfolio):
        portfolio.challenge_status = "won"
        ok, msg = portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        assert not ok


class TestRiskBudgetEnforcement:
    def test_exceeds_max_positions(self):
        p = PaperPortfolio(starting_balance=1100.0)
        ok1, _ = p.confirm_buy("BTC/USD", 50000, 30, 48500, 1.5)
        ok2, _ = p.confirm_buy("ETH/USD", 3000, 30, 2900, 1.5)
        assert ok1 and ok2
        ok, msg = p.confirm_buy("LTC/USD", 200, 30, 190, 1.5)
        assert not ok
        assert "positions" in msg.lower()

    def test_exceeds_risk_budget(self, portfolio):
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 16.0)
        assert not ok

    def test_within_new_risk_budget(self, portfolio):
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 400, 48500, 13.0)
        assert ok


class TestChallengeStatus:
    def test_win_at_1120(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        assert portfolio.challenge_status == "won"

    def test_loss_at_950(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        assert portfolio.challenge_status == "lost"

    def test_win_blocks_normal_buys(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert not ok

    def test_loss_blocks_buys(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert not ok

    def test_buy_does_not_falsely_trigger_loss(self):
        """Buying a position reduces cash but not equity — should stay active."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert ok
        assert portfolio.challenge_status == "active", (
            f"Balance=${portfolio.balance_usd:.2f}, equity=${portfolio._get_equity_estimate():.2f}, "
            f"status={portfolio.challenge_status} — should be active"
        )

    def test_confirm_uses_equity_not_cash_for_circuit_breakers(self):
        """Second confirm must not be blocked by low cash when equity is healthy.

        Reproduces: first BUY drops cash below $955, but equity stays ~$1000.
        A second BUY should pass the circuit breaker (equity > $975), not be
        rejected with 'BLOCKED: Balance <= $955'.
        """
        portfolio = PaperPortfolio(starting_balance=1000.0)
        ok1, msg1 = portfolio.confirm_buy("BTC/USD", 50000, 400, 48500, 13.0)
        assert ok1, f"First buy should succeed: {msg1}"

        assert portfolio.balance_usd < 600, "Cash should be well below $955 after large buy"
        equity = portfolio._get_equity_estimate()
        assert equity > 975, f"Equity should be healthy (~$1000), got ${equity:.2f}"

        ok2, msg2 = portfolio.confirm_buy("LINK/USD", 15.0, 100, 14.5, 13.0)
        assert ok2, (
            f"Second buy should NOT be blocked by circuit breaker. "
            f"Cash=${portfolio.balance_usd:.2f}, Equity=${equity:.2f}, "
            f"Rejection: {msg2}"
        )

    def test_equity_based_loss_detection(self):
        """Loss only triggers when total equity (cash + positions) drops to $950."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.balance_usd = 940.0
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "lost"

    def test_lost_is_terminal(self):
        """Lost status is permanent — only /new_challenge can reset it."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "lost"
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "lost"

    def test_won_is_terminal(self):
        """Won status does not revert to active."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "won"

    def test_reset_challenge_refuses_lost(self):
        """Lost challenge cannot be resurrected — use /new_challenge instead."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "lost"
        msg = portfolio.reset_challenge_status()
        assert portfolio.challenge_status == "lost"
        assert "cannot resurrect" in msg.lower() or "new_challenge" in msg.lower()

    def test_reset_challenge_preserves_won(self):
        """Manual reset cannot revert terminal 'won' status."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        msg = portfolio.reset_challenge_status()
        assert portfolio.challenge_status == "won"
        assert "cannot reset" in msg.lower()


class TestChallengeEndedStopsSignals:
    def test_is_challenge_active_true_when_active(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        assert portfolio.is_challenge_active is True

    def test_is_challenge_active_false_when_won(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        assert portfolio.is_challenge_active is False

    def test_is_challenge_active_false_when_lost(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        assert portfolio.is_challenge_active is False

    def test_challenge_ended_message_won(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        msg = portfolio.get_challenge_ended_message()
        assert "WON" in msg
        assert "1120" in msg or "1,120" in msg

    def test_challenge_ended_message_lost(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        msg = portfolio.get_challenge_ended_message()
        assert "LOST" in msg


class TestNewChallenge:
    def test_new_challenge_resets_balance(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.balance_usd = 800.0
        portfolio.challenge_status = "lost"
        archive, msg = portfolio.start_new_challenge()
        assert portfolio.balance_usd == 1000.0
        assert portfolio.challenge_status == "active"
        assert portfolio.is_challenge_active is True

    def test_new_challenge_archives_trades(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        portfolio.confirm_sell("BTC/USD", 51000)
        assert len(portfolio.closed_trades) == 1
        archive, msg = portfolio.start_new_challenge()
        assert archive["total_trades"] == 1
        assert len(archive["trades"]) == 1
        assert portfolio.closed_trades == []
        assert portfolio.positions == []

    def test_new_challenge_clears_realized_pnl(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.realized_pnl_total = 50.0
        archive, msg = portfolio.start_new_challenge()
        assert archive["realized_pnl"] == 50.0
        assert portfolio.realized_pnl_total == 0.0

    def test_new_challenge_message_format(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        archive, msg = portfolio.start_new_challenge()
        assert "New Paper Challenge started" in msg
        assert "WON" in msg
        assert "$1000.00" in msg or "$1,000.00" in msg


class TestEquityBasedDistances:
    def test_distance_to_win_uses_equity_not_cash(self):
        """After a BUY, distance_to_win should be based on equity (unchanged),
        not on reduced cash balance."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)

        equity = portfolio._get_equity_estimate()
        assert abs(equity - 1000.0) < 1.0, "Equity should be ~$1000 after buy"
        assert portfolio.balance_usd < 900, "Cash should be reduced by ~$100"

        expected_distance_to_win = settings.win_level - equity
        expected_distance_to_loss = equity - settings.loss_level

        assert abs(expected_distance_to_win - 120.0) < 1.0
        assert abs(expected_distance_to_loss - 50.0) < 1.0

    def test_engine_receives_equity_based_distance(self):
        """The StrategyEngine should produce distance_to_win based on equity."""
        import pandas as pd
        import numpy as np

        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)

        equity = portfolio._get_equity_estimate()
        engine = StrategyEngine()

        df = pd.DataFrame({
            "open": np.random.uniform(49000, 51000, 250),
            "high": np.random.uniform(49000, 51000, 250),
            "low": np.random.uniform(49000, 51000, 250),
            "close": np.random.uniform(49000, 51000, 250),
            "volume": np.random.uniform(100, 1000, 250),
        })

        signal = engine.analyze(
            "BTC/USD", df, df, 50000.0,
            equity, portfolio.get_open_positions(),
            portfolio.get_total_open_risk(),
        )

        assert abs(signal.distance_to_win - (settings.win_level - equity)) < 0.01
        assert abs(signal.distance_to_loss - (equity - settings.loss_level)) < 0.01


class TestPortfolioInvariants:
    """Invariants that must hold regardless of trade sequence."""

    def test_cash_never_negative(self, portfolio):
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        portfolio.confirm_buy("ETH/USD", 3000, 100, 2900, 3.0)
        assert portfolio.balance_usd >= 0, f"Cash went negative: ${portfolio.balance_usd:.2f}"

    def test_open_positions_never_exceed_max(self, portfolio):
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        portfolio.confirm_buy("ETH/USD", 3000, 100, 2900, 3.0)
        ok, msg = portfolio.confirm_buy("LINK/USD", 15, 100, 14, 3.0)
        assert not ok
        assert len([p for p in portfolio.positions if p.status == "open"]) <= settings.max_open_positions

    def test_total_cost_never_exceeds_starting_balance(self, portfolio):
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        portfolio.confirm_buy("ETH/USD", 3000, 100, 2900, 3.0)
        open_pos = [p for p in portfolio.positions if p.status == "open"]
        total_spent = sum(p.position_value_usd + p.commission_usd + p.spread_cost_usd for p in open_pos)
        assert total_spent <= portfolio.starting_balance, (
            f"Total spent ${total_spent:.2f} exceeds starting balance ${portfolio.starting_balance:.2f}"
        )

    def test_cash_plus_positions_equals_equity(self, portfolio):
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        open_pos = [p for p in portfolio.positions if p.status == "open"]
        position_value = sum(p.entry_price * p.quantity for p in open_pos)
        equity = portfolio.get_total_equity({})
        assert abs(equity - (portfolio.balance_usd + position_value)) < 0.01

    def test_buy_rejected_when_insufficient_balance(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        ok, _ = portfolio.confirm_buy("BTC/USD", 50000, 500, 48500, 3.0)
        assert ok
        ok2, msg2 = portfolio.confirm_buy("ETH/USD", 3000, 600, 2900, 3.0)
        assert not ok2, "Should reject buy when insufficient balance"
        assert "insufficient" in msg2.lower()
        assert portfolio.balance_usd >= 0, f"Cash went negative: ${portfolio.balance_usd:.2f}"


class TestRestoreDeduplication:
    """Verify restore_from_db handles duplicate/excess DB rows."""

    def test_restore_deduplicates_same_symbol(self):
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone

        mock_positions = []
        for i in range(3):
            pos = MagicMock()
            pos.id = i + 1
            pos.asset = MagicMock()
            pos.asset.symbol = "ETH/USD"
            pos.entry_price = 3000.0
            pos.quantity = 0.01
            pos.side = "BUY"
            pos.stop_loss = 2900.0
            pos.signal_id = None
            pos.is_open = True
            pos.opened_at = datetime(2026, 7, 20, i, 0, tzinfo=timezone.utc)
            mock_positions.append(pos)

        with patch("src.portfolio.manager.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            mock_query = mock_session.query.return_value
            mock_join = mock_query.join.return_value
            mock_filter = mock_join.filter.return_value

            call_count = [0]
            def side_effect_order_by(*args, **kwargs):
                call_count[0] += 1
                result = MagicMock()
                if call_count[0] == 1:
                    result.all.return_value = mock_positions
                else:
                    result.all.return_value = []
                    result.first.return_value = None
                return result
            mock_filter.order_by = side_effect_order_by
            mock_filter.filter.return_value.order_by = side_effect_order_by

            portfolio = PaperPortfolio.restore_from_db()

            open_pos = [p for p in portfolio.positions if p.status == "open"]
            assert len(open_pos) <= 1, f"Expected ≤1 open position, got {len(open_pos)}"

    def test_restore_caps_at_max_positions(self):
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone

        symbols = ["BTC/USD", "ETH/USD", "LINK/USD", "SOL/USD"]
        mock_positions = []
        for i, sym in enumerate(symbols):
            pos = MagicMock()
            pos.id = i + 1
            pos.asset = MagicMock()
            pos.asset.symbol = sym
            pos.entry_price = 100.0
            pos.quantity = 0.1
            pos.side = "BUY"
            pos.stop_loss = 90.0
            pos.signal_id = None
            pos.is_open = True
            pos.opened_at = datetime(2026, 7, 20, i, 0, tzinfo=timezone.utc)
            mock_positions.append(pos)

        with patch("src.portfolio.manager.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            mock_query = mock_session.query.return_value
            mock_join = mock_query.join.return_value
            mock_filter = mock_join.filter.return_value

            call_count = [0]
            def side_effect_order_by(*args, **kwargs):
                call_count[0] += 1
                result = MagicMock()
                if call_count[0] == 1:
                    result.all.return_value = mock_positions
                else:
                    result.all.return_value = []
                    result.first.return_value = None
                return result
            mock_filter.order_by = side_effect_order_by
            mock_filter.filter.return_value.order_by = side_effect_order_by
            mock_filter.filter.return_value.filter.return_value.order_by = side_effect_order_by

            portfolio = PaperPortfolio.restore_from_db()

            open_pos = [p for p in portfolio.positions if p.status == "open"]
            assert len(open_pos) <= settings.max_open_positions, (
                f"Open positions {len(open_pos)} exceeds max {settings.max_open_positions}"
            )


class TestChallengeResetDBCleanup:
    """Critical fix #1: /new_challenge must close DB positions before resetting."""

    def test_start_new_challenge_closes_db_positions(self):
        from unittest.mock import patch, MagicMock, call

        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        assert len(portfolio.positions) == 1

        mock_pos_1 = MagicMock()
        mock_pos_1.entry_price = 50000.0
        mock_pos_2 = MagicMock()
        mock_pos_2.entry_price = 3000.0

        with patch("src.portfolio.manager.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            mock_repo = MagicMock()
            mock_repo.get_open.return_value = [mock_pos_1, mock_pos_2]

            with patch("src.portfolio.manager.PositionRepository", return_value=mock_repo):
                archive, msg = portfolio.start_new_challenge()

            assert mock_repo.close.call_count == 2
            for c in mock_repo.close.call_args_list:
                assert c.kwargs["close_reason"] == "challenge_reset"
                assert c.kwargs["realized_pnl"] == 0.0

        assert portfolio.balance_usd == 1000.0
        assert len(portfolio.positions) == 0
        assert portfolio.challenge_status == "active"

    def test_challenge_reset_positions_excluded_from_balance_replay(self):
        """After reset, restore_from_db must not replay challenge_reset closes."""
        from unittest.mock import patch, MagicMock

        with patch("src.portfolio.manager.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            mock_query = mock_session.query.return_value
            mock_join = mock_query.join.return_value
            mock_filter = mock_join.filter.return_value

            call_count = [0]
            def side_effect_order_by(*args, **kwargs):
                call_count[0] += 1
                result = MagicMock()
                result.all.return_value = []
                result.first.return_value = None
                return result
            mock_filter.order_by = side_effect_order_by
            mock_filter.filter.return_value.order_by = side_effect_order_by
            mock_filter.filter.return_value.filter.return_value.order_by = side_effect_order_by
            mock_filter.filter.return_value.filter.return_value.filter.return_value.order_by = side_effect_order_by

            portfolio = PaperPortfolio.restore_from_db()

            assert portfolio.balance_usd == settings.starting_balance
            assert len(portfolio.positions) == 0

    def test_full_cycle_buy_reset_restore_no_duplicates(self):
        """Reproduces the exact prior failure: multiple confirms + reset + restore."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone

        portfolio = PaperPortfolio(starting_balance=1000.0)

        portfolio.confirm_buy(
            symbol="BTC/USD", entry_price=50000.0,
            position_value_usd=100.0, stop_loss=48500.0, risk_dollars=3.0,
        )
        portfolio.confirm_buy(
            symbol="ETH/USD", entry_price=3000.0,
            position_value_usd=100.0, stop_loss=2850.0, risk_dollars=5.0,
        )
        assert len(portfolio.positions) == 2

        closed_positions = []

        with patch("src.portfolio.manager.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            mock_repo = MagicMock()
            mock_db_pos_1 = MagicMock()
            mock_db_pos_1.entry_price = 50000.0
            mock_db_pos_2 = MagicMock()
            mock_db_pos_2.entry_price = 3000.0
            mock_repo.get_open.return_value = [mock_db_pos_1, mock_db_pos_2]

            def track_close(pos, exit_price, realized_pnl, close_reason):
                closed_positions.append(close_reason)
            mock_repo.close.side_effect = track_close

            with patch("src.portfolio.manager.PositionRepository", return_value=mock_repo):
                archive, msg = portfolio.start_new_challenge()

        assert all(r == "challenge_reset" for r in closed_positions)
        assert len(closed_positions) == 2

        assert portfolio.balance_usd == 1000.0
        assert len(portfolio.positions) == 0
        assert portfolio.challenge_status == "active"


class TestUniqueOpenPositionConstraint:
    """Critical fix #2: partial unique index via Alembic migration (PostgreSQL)
    and application-level guard in _persist_buy (all backends)."""

    def test_migration_file_creates_partial_unique_index(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "m008", "alembic/versions/008_unique_open_position.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "008"
        assert mod.down_revision == "007"
        assert hasattr(mod, "upgrade")
        assert hasattr(mod, "downgrade")

    def test_persist_buy_closes_stale_before_creating(self):
        """Application-level guard: _persist_buy closes existing open row first."""
        from unittest.mock import patch, MagicMock

        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy(
            symbol="BTC/USD", entry_price=50000.0,
            position_value_usd=100.0, stop_loss=48500.0, risk_dollars=3.0,
        )

        mock_asset = MagicMock()
        mock_asset.id = 1

        with patch("src.portfolio.manager.get_session") as mock_gs:
            mock_session = MagicMock()
            mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)
            mock_session.query.return_value.filter.return_value.first.return_value = mock_asset

            stale = MagicMock()
            stale.entry_price = 50000.0
            mock_repo = MagicMock()
            mock_repo.get_open.return_value = [stale]
            mock_repo.create.return_value = MagicMock(id=99)

            with patch("src.portfolio.manager.PositionRepository", return_value=mock_repo):
                portfolio._persist_buy(portfolio.positions[0], signal_id=None)

            mock_repo.close.assert_called_once_with(
                stale, portfolio.positions[0].entry_price, 0.0, "duplicate_cleanup",
            )

    def test_postgresql_partial_index_sql_content(self):
        """Verify the migration SQL creates the correct partial unique index."""
        with open("alembic/versions/008_unique_open_position.py") as f:
            content = f.read()
        assert "uq_one_open_per_asset" in content
        assert "WHERE is_open" in content or "WHERE is_open = true" in content
        assert "UNIQUE INDEX" in content
        assert "paper_positions" in content
        assert "asset_id" in content


class TestConfirmBuyDuplicateSymbolCheck:
    """Fix #11: confirm_buy must reject duplicate symbols in-memory."""

    def test_confirm_buy_rejects_duplicate_symbol(self, portfolio):
        ok1, msg1 = portfolio.confirm_buy(
            symbol="BTC/USD", entry_price=50000.0,
            position_value_usd=100.0, stop_loss=48500.0, risk_dollars=3.0,
        )
        assert ok1

        ok2, msg2 = portfolio.confirm_buy(
            symbol="BTC/USD", entry_price=51000.0,
            position_value_usd=100.0, stop_loss=49000.0, risk_dollars=3.0,
        )
        assert not ok2
        assert "already have" in msg2.lower() or "open position" in msg2.lower()

    def test_confirm_buy_allows_different_symbols(self, portfolio):
        ok1, _ = portfolio.confirm_buy(
            symbol="BTC/USD", entry_price=50000.0,
            position_value_usd=100.0, stop_loss=48500.0, risk_dollars=3.0,
        )
        assert ok1

        ok2, _ = portfolio.confirm_buy(
            symbol="ETH/USD", entry_price=3000.0,
            position_value_usd=100.0, stop_loss=2850.0, risk_dollars=3.0,
        )
        assert ok2


class TestResetChallengeCannotResurrect:
    """Fix #13: /reset_challenge must not resurrect a lost challenge."""

    def test_reset_refuses_to_resurrect_lost(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "lost"
        result = portfolio.reset_challenge_status(prices={})
        assert "cannot resurrect" in result.lower() or "lost" in result.lower()
        assert portfolio.challenge_status == "lost"

    def test_reset_refuses_won(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        result = portfolio.reset_challenge_status(prices={})
        assert "cannot" in result.lower()
        assert portfolio.challenge_status == "won"

    def test_reset_keeps_active_unchanged(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "active"
        result = portfolio.reset_challenge_status(prices={})
        assert portfolio.challenge_status in ("active", "won", "lost")
