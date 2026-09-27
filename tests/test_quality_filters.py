import pytest
import time
from core.risk_manager import RiskManager
from core.market_scanner import MarketScanner
from config import load_config
from bot import BinanceTrBot

def test_risk_manager_loss_cooldown():
    rm = RiskManager(
        symbol_cooldown_seconds=90,
        loss_cooldown_seconds=300
    )

    # 1. Başarılı çıkış (Kâr Al) -> 90s standart cooldown uygulanmalı
    rm.record_trade_exit("SOL_TRY", is_loss=False)
    allowed, msg = rm.can_open_position(0, is_basket_filling=True, symbol="SOL_TRY")
    assert allowed is False
    assert "90s" in msg or "bekleme süresi aktif" in msg

    # 2. Zararlı çıkış (Stop Loss) -> 300s ceza beklemesi uygulanmalı
    rm.record_trade_exit("VANA_TRY", is_loss=True)
    allowed, msg = rm.can_open_position(0, is_basket_filling=True, symbol="VANA_TRY")
    assert allowed is False
    assert "ceza beklemesi aktif" in msg
    assert "300s" in msg or "299s" in msg or "300" in msg


def test_strict_buy_signal_requirement():
    cfg = load_config()
    cfg.trading.mode = "simulation"
    cfg.trading.auto_fill_portfolio = False
    cfg.trading.require_strict_buy_signal = True
    cfg.trading.candidate_observation_seconds = 0
    cfg.trading.min_observation_gain_pct = 0.0

    bot = BinanceTrBot(cfg)
    bot.scanner.scan_top_active_pairs = lambda **kwargs: [{"symbol": "SOL_TRY", "change_pct": 5.0, "velocity_1m_pct": 0.0, "quant_score": 8.0, "updated_at": time.time()}]
    bot.scanner.cached_top_pairs = [{"symbol": "SOL_TRY", "change_pct": 5.0, "velocity_1m_pct": 0.0, "quant_score": 8.0, "updated_at": time.time()}]
    bot.scanner.watchlist.min_observation_seconds = 0  # Direkt onay

    # Mock engine ile HOLD sinyali üret
    mock_snap = {
        "symbol": "SOL_TRY",
        "price": 100.0,
        "bid": 100.0, "ask": 100.05, "timestamp": time.time(),
        "quote_valid": True, "features_ready": True,
        "rsi": 60.0,
        "bb_lower": 90.0,
        "bb_middle": 95.0,
        "bb_upper": 105.0,
        "ema_fast": 94.0,
        "ema_slow": 96.0,
        "change_24h_pct": -1.0,
    }
    engine = bot.get_engine_for("SOL_TRY")
    engine.latest_snapshot = mock_snap
    engine.update_market_state = lambda: mock_snap

    # Step çalıştır -> Sinyal HOLD olduğu için ALIM YAPILMAMALI
    bot.step()
    assert len(bot.simulator.positions) == 0

    # Şimdi BUY sinyali mockla
    bot.scanner.scan_top_active_pairs = lambda **kwargs: [{"symbol": "SOL_TRY", "change_pct": 5.0, "velocity_1m_pct": 0.5, "quant_score": 8.0, "updated_at": time.time()}]
    bot.scanner.cached_top_pairs = [{"symbol": "SOL_TRY", "change_pct": 5.0, "velocity_1m_pct": 0.5, "quant_score": 8.0, "updated_at": time.time()}]
    mock_buy_snap = {
        "symbol": "SOL_TRY",
        "price": 100.0,
        "bid": 100.0, "ask": 100.05, "timestamp": time.time(),
        "quote_valid": True, "features_ready": True,
        "rsi": 38.0,  # Oversold
        "bb_lower": 100.5,
        "bb_middle": 105.0,
        "bb_upper": 110.0,
        "ema_fast": 102.0,
        "ema_slow": 100.0,
        "change_24h_pct": 2.0,
    }

    engine.latest_snapshot = mock_buy_snap
    engine.update_market_state = lambda: mock_buy_snap

    bot.step()
    # Sinyal BUY olduğu için ALIM YAPILMALI
    assert len(bot.simulator.positions) == 1
    assert "SOL_TRY" in bot.simulator.positions[list(bot.simulator.positions.keys())[0]]["symbol"]
