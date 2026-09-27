import time
import pytest
from config import load_config
from bot import BinanceTrBot

def test_portfolio_stop_loss_triggers_shutdown():
    cfg = load_config()
    cfg.trading.mode = "simulation"
    cfg.trading.initial_virtual_balance = 10000.0
    cfg.strategy.portfolio_stop_loss_pct = 2.0

    bot = BinanceTrBot(cfg)
    bot.is_running = True

    # 1. İlk pozisyon aç ve zarar ile kapat (-150 TL)
    bot.simulator.buy("BTC_TRY", price=100.0, budget_try=2000.0, reason="Test 1")
    pos_id = list(bot.simulator.positions.keys())[0]
    bot.simulator.sell(pos_id, price=92.5, reason="Stop Loss")  # ~ -150 TL zarar

    # 2. İkinci pozisyon aç ve zarar ile kapat (-100 TL)
    bot.simulator.buy("ETH_TRY", price=100.0, budget_try=2000.0, reason="Test 2")
    pos_id2 = list(bot.simulator.positions.keys())[0]
    bot.simulator.sell(pos_id2, price=95.0, reason="Stop Loss")  # ~ -100 TL zarar

    # Toplam zarar ~ -250 TL (-%2.5 > -%2.0)
    summary = bot.simulator.get_summary()
    assert summary["total_pnl_pct"] <= -2.0

    # 3. Üçüncü bir pozisyon açık olsun
    bot.simulator.buy("SOL_TRY", price=100.0, budget_try=2000.0, reason="Açık Pozisyon")
    assert len(bot.simulator.positions) == 1

    # Mock SOL_TRY engine
    mock_snap = {
        "symbol": "SOL_TRY",
        "price": 100.0,
        "bid": 100.0, "ask": 100.05, "timestamp": time.time(),
        "quote_valid": True, "features_ready": True,
        "rsi": 50.0,
    }
    engine = bot.get_engine_for("SOL_TRY")
    engine.latest_snapshot = mock_snap
    engine.update_market_state = lambda: mock_snap

    # Step çalıştırıldığında portföy stop loss tetiklenmeli
    bot.step()

    # Tüm açık pozisyonlar acilen satılmış olmalı
    assert len(bot.simulator.positions) == 0
    # Bot durdurulmuş olmalı
    assert bot.is_running is False
    assert "Portföy Stop-Loss Tetiklendi" in bot.current_status_text
