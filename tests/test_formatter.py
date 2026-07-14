import pytest
from src.notifier.formatter import SignalFormatter
from src.strategy.engine import TradeSignal
from src.strategy.regime import MarketRegime


def _make_signal():
    return TradeSignal(
        signal_type="BUY",
        priority="HIGH",
        asset_symbol="BTC/USD",
        regime=MarketRegime.TREND,
        entry_price=50000.0,
        stop_loss=48500.0,
        position_size_usd=100.0,
        max_loss_usd=3.0,
        order_type="LIMIT",
        cancel_level=50500.0,
        reason="Strong trend confirmed",
        explanation="Good entry",
        price_range_low=49900.0,
        price_range_high=50100.0,
        remaining_usd=896.40,
        current_balance=1000.0,
        distance_to_win=120.0,
        distance_to_loss=50.0,
    )


class TestBeginnerExplanations:
    def test_beginner_mode_on(self):
        fmt = SignalFormatter(beginner_mode=True)
        text = fmt.format_signal(_make_signal())
        assert "automatic exit to limit losses" in text
        assert "STOP-LOSS" in text

    def test_beginner_mode_off(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_make_signal())
        assert "automatic exit to limit losses" not in text
        assert "STOP-LOSS" in text

    def test_toggle_beginner(self):
        fmt_on = SignalFormatter(beginner_mode=True)
        fmt_off = SignalFormatter(beginner_mode=False)
        text_on = fmt_on.format_signal(_make_signal())
        text_off = fmt_off.format_signal(_make_signal())
        assert len(text_on) > len(text_off)


class TestSignalFormat:
    def test_all_15_fields_present(self):
        fmt = SignalFormatter(beginner_mode=False)
        signal = _make_signal()
        text = fmt.format_signal(signal)
        assert "HIGH" in text
        assert "BUY" in text
        assert "BTC/USD" in text
        assert "$100.00" in text
        assert "49900.00" in text
        assert "50100.00" in text
        assert "LIMIT" in text
        assert "50500.00" in text
        assert "48500.00" in text
        assert "$3.00" in text
        assert "Good entry" in text
        assert "Strong trend" in text
        assert "$896.40" in text
        assert "$1000.00" in text
        assert "$120.00" in text
        assert "$50.00" in text

    def test_confirm_prompt_for_buy(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_make_signal())
        assert "/confirm" in text

    def test_no_confirm_for_no_trade(self):
        fmt = SignalFormatter(beginner_mode=False)
        signal = TradeSignal(
            signal_type="NO_TRADE",
            priority="MEDIUM",
            asset_symbol="BTC/USD",
            regime=MarketRegime.CHOP,
            reason="No signal",
            current_balance=1000.0,
            distance_to_win=120.0,
            distance_to_loss=50.0,
        )
        text = fmt.format_signal(signal)
        assert "/confirm" not in text
