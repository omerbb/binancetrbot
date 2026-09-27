import time
import pytest
from unittest.mock import MagicMock
from core.risk_manager import RiskManager
from core.live_trader import LiveTraderEngine
from bot import BinanceTrBot
from config import BotConfig

def test_risk_manager_zero_price_does_not_trigger_stop_loss():
    rm = RiskManager(take_profit_pct=1.5, stop_loss_pct=0.85)
    pos = {
        "position_id": "test_pos_1",
        "symbol": "SOL_TRY",
        "entry_price": 2500.0,
        "quantity": 0.5,
    }
    
    # 0.0 anlık fiyat stop-loss tetiklememeli
    should_close, reason, pnl_pct = rm.evaluate_exit(pos, current_price=0.0)
    assert not should_close
    assert pnl_pct == 0.0
    assert "Geçersiz fiyat" in reason

    # Negatif anlık fiyat stop-loss tetiklememeli
    should_close, reason, pnl_pct = rm.evaluate_exit(pos, current_price=-10.0)
    assert not should_close
    assert pnl_pct == 0.0

    # 0.0 giriş fiyatı stop-loss tetiklememeli
    pos_invalid_entry = {
        "position_id": "test_pos_2",
        "symbol": "SOL_TRY",
        "entry_price": 0.0,
        "quantity": 0.5,
    }
    should_close, reason, pnl_pct = rm.evaluate_exit(pos_invalid_entry, current_price=2500.0)
    assert not should_close
    assert pnl_pct == 0.0

def test_live_trader_get_summary_zero_price_fallback():
    mock_client = MagicMock()
    mock_client.api_key = "dummy"
    mock_client.secret_key = "dummy"
    mock_client.get_account_spot.return_value = {
        "code": 0,
        "data": {
            "accountAssets": [
                {"asset": "TRY", "free": "800.0", "locked": "0.0"}
            ]
        }
    }
    
    engine = LiveTraderEngine(client=mock_client, symbol="AUTO")
    engine.closed_trades = []
    engine.positions = {
        "order_123": {
            "position_id": "order_123",
            "symbol": "AVAX_TRY",
            "entry_price": 500.0,
            "current_price": 0.0,  # Fiyat henüz gelmedi
            "quantity": 0.4,
            "invested_cost": 200.0,
        }
    }
    engine.session_initial_balance = 1000.0
    
    summary = engine.get_summary()
    # Fiyat 0 olsa bile entry_price'a fallback yapmalı, -200 TL / -100% zarar göstermemeli
    open_pos = summary["open_positions"][0]
    assert open_pos["current_price"] == 500.0
    assert open_pos["unrealized_pnl"] == 0.0
    assert open_pos["unrealized_pnl_pct"] == 0.0
    assert summary["invested_value"] == 200.0
    assert summary["total_equity"] == 1000.0
    assert summary["total_pnl"] == 0.0
    assert summary["total_pnl_pct"] == 0.0

def test_live_trader_settlement_lag_does_not_trigger_portfolio_stop_loss():
    mock_client = MagicMock()
    mock_client.api_key = "dummy"
    mock_client.secret_key = "dummy"
    
    # 2200 TL bakiye ile başlandı, 3 adet 400 TL coin alındı (Kalan TRY = 1000 TL)
    mock_client.get_account_spot.return_value = {
        "code": 0,
        "data": {
            "accountAssets": [
                {"asset": "TRY", "free": "1000.0", "locked": "0.0"}
            ]
        }
    }
    
    engine = LiveTraderEngine(client=mock_client, symbol="AUTO")
    engine.session_initial_balance = 2200.0
    
    # 1. Pozisyon kapandı (-4.41 TL ile)
    engine.closed_trades = [
        {"symbol": "SOLV_TRY", "net_pnl": -4.41, "pnl_pct": -0.90, "is_win": False}
    ]
    # Kalan 2 pozisyon açık
    engine.positions = {
        "pos_2": {"position_id": "pos_2", "symbol": "PUMP_TRY", "entry_price": 0.20, "current_price": 0.20, "quantity": 2000.0, "invested_cost": 400.0, "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0},
        "pos_3": {"position_id": "pos_3", "symbol": "VIRTUAL_TRY", "entry_price": 40.0, "current_price": 40.0, "quantity": 10.0, "invested_cost": 400.0, "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0},
    }
    
    # Binance TR API TRY bakiyesini henüz güncellememiş olsa bile (hala 1000 TL):
    summary = engine.get_summary()
    
    # Kâr/Zarar -400 TL değil, sadece -4.41 TL olmalıdır!
    assert summary["realized_pnl"] == -4.41
    assert summary["total_pnl"] == -4.41
    assert summary["total_pnl_pct"] == pytest.approx(-4.41 / 2200.0 * 100.0, abs=0.01)
    # Portföy stop-loss eşiği (%10 veya %2.5) asla tetiklenmez
    rm = RiskManager(portfolio_stop_loss_pct=2.5)
    sl_triggered, _ = rm.evaluate_portfolio_stop_loss(summary["total_pnl_pct"])
    assert not sl_triggered

def test_bot_force_test_buy_live_arguments():
    cfg = BotConfig()
    cfg.trading.mode = "live"
    cfg.trading.symbol = "SOL_TRY"
    cfg.trading.budget_per_trade = 200.0
    
    bot = BinanceTrBot(config=cfg)
    bot.live_trader = MagicMock()
    bot.live_trader.buy.return_value = {
        "position_id": "live_1",
        "symbol": "SOL_TRY",
        "entry_price": 2500.0,
        "quantity": 0.08,
    }
    bot.scanner = MagicMock()
    bot.get_engine_for = MagicMock()
    engine_mock = MagicMock()
    engine_mock.update_market_state.return_value = {"price": 2500.0, "bid": 2500.0, "ask": 2500.0, "timestamp": time.time(), "quote_valid": True}
    bot.get_engine_for.return_value = engine_mock
    
    pos = bot.force_test_buy(symbol="SOL_TRY", budget=200.0, reason="Manuel Test")
    assert pos is not None
    bot.live_trader.buy.assert_called_once_with("SOL_TRY", 2500.0, 200.0, reason="Manuel Test")
