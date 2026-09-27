import time
import pytest
from core.risk_manager import RiskManager
from config import load_config
from bot import BinanceTrBot

def test_risk_manager_rollover_decision():
    rm = RiskManager(
        take_profit_pct=2.0,
        stop_loss_pct=1.0,
        trailing_activation_pct=0.80,
        trailing_stop_pct=0.50,
        prevent_rebuy_churn=True
    )

    position = {
        "symbol": "SOL_TRY",
        "entry_price": 100.0,
        "quantity": 10.0,
        "highest_price": 102.5,
    }

    # 1. Kârlı çıkış + Alım Sinyali -> Rollover onaylanmalı
    allowed, msg = rm.should_rollover_position(
        position=position,
        current_price=102.5,
        is_profitable_exit=True,
        strategy_signal="BUY",
        is_top_leader=False
    )
    assert allowed is True
    assert "Pozisyon devredildi" in msg

    # 2. Kârlı çıkış + Radar Lideri -> Rollover onaylanmalı
    allowed, msg = rm.should_rollover_position(
        position=position,
        current_price=102.5,
        is_profitable_exit=True,
        strategy_signal="HOLD",
        is_top_leader=True
    )
    assert allowed is True
    assert "Pozisyon devredildi" in msg

    # 3. Zarar Kes (Stop-Loss) -> Rollover KESİNLİKLE REDDEDİLMELİ!
    allowed, msg = rm.should_rollover_position(
        position=position,
        current_price=98.5,
        is_profitable_exit=False,
        strategy_signal="BUY",
        is_top_leader=True
    )
    assert allowed is False
    assert "Zarar kes durumunda devir yapılamaz" in msg

    # 4. Kârlı ama ne lider ne de BUY sinyali var -> Çıkış yapılmalı
    allowed, msg = rm.should_rollover_position(
        position=position,
        current_price=102.5,
        is_profitable_exit=True,
        strategy_signal="HOLD",
        is_top_leader=False
    )
    assert allowed is False
    assert "Devir koşulu sağlanmadı" in msg

    # 5. prevent_rebuy_churn = False ise devir devre dışı olmalı
    rm.prevent_rebuy_churn = False
    allowed, msg = rm.should_rollover_position(
        position=position,
        current_price=102.5,
        is_profitable_exit=True,
        strategy_signal="BUY",
        is_top_leader=True
    )
    assert allowed is False
    assert "Devir koruması devre dışı" in msg


def test_bot_step_position_rollover():
    cfg = load_config()
    cfg.trading.mode = "simulation"
    cfg.trading.prevent_rebuy_churn = True
    cfg.strategy.take_profit_pct = 2.0
    cfg.strategy.stop_loss_pct = 1.0

    bot = BinanceTrBot(cfg)

    # Sanal pozisyon aç
    pos = bot.simulator.buy("SOL_TRY", price=100.0, budget_try=1000.0, reason="Test Giriş")
    assert pos is not None
    assert len(bot.simulator.positions) == 1

    # Fiyatı TP seviyesinin üzerine çıkar (+2.5%)
    bot.scanner.cached_top_pairs = [{"symbol": "SOL_TRY", "change_pct": 5.0, "abs_change": 5.0}]
    bot.scanner.last_scan_time = time.time()
    bot.scanner.cached_top_pairs[0]["updated_at"] = time.time()
    bot.scanner.watchlist.min_observation_seconds = 0
    mock_snap = {
        "symbol": "SOL_TRY",
        "price": 102.5,
        "bid": 102.5, "ask": 102.55, "timestamp": time.time(),
        "quote_valid": True, "features_ready": True,
        "rsi": 45.0,
        "ema_fast": 103.0,
        "ema_slow": 101.0,
        "bb_lower": 99.0,
        "bb_middle": 101.0,
        "bb_upper": 103.0,
        "change_24h_pct": 5.0,
    }
    engine = bot.get_engine_for("SOL_TRY")
    engine.latest_snapshot = mock_snap
    engine.update_market_state = lambda: mock_snap

    # Step çalıştır
    bot.step()

    # Pozisyon kapatılmamış olmalı (devredilmiş olmalı)
    assert len(bot.simulator.positions) == 1
    updated_pos = list(bot.simulator.positions.values())[0]
    # Risk tabanı yenilenir, gerçek işlem maliyeti ve giriş fiyatı korunur.
    assert updated_pos["entry_price"] == pytest.approx(100.0)
    assert updated_pos["risk_reference_price"] == pytest.approx(102.5)
    assert updated_pos["invested_cost"] == pytest.approx(1000.0)
    assert len(bot.simulator.closed_trades) == 0
