import time
import math
import threading
import logging
from typing import Dict, Any, Optional

from config import BotConfig, load_config, save_config, config_to_dict
from core.binance_client import BinanceTrClient
from core.market_data import MarketDataEngine
from core.market_scanner import MarketScanner
from core.risk_manager import RiskManager
from core.simulator import SimulatorEngine
from core.live_trader import LiveTraderEngine
from strategies.fee_recovery import FeeRecoveryStrategy
from strategies.adaptive_regime import AdaptiveRegimeStrategy
from strategies.rsi_bollinger import RsiBollingerStrategy
from strategies.momentum_ema import MomentumEmaStrategy
from strategies.grid_scalper import GridScalperStrategy
from strategies.quick_test_scalper import QuickTestScalperStrategy
from reporting.performance import PerformanceMetrics
from reporting.report_generator import ReportGenerator

import sys
import os

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BinanceTrBot")

class BinanceTrBot:
    """
    Tüm motorları, stratejileri ve işlem döngüsünü yöneten ana bot koordinatörü.
    Otomatik piyasa radarı ile en çok dalgalanan coinleri bulur ve al-sat yapar.
    """
    def __init__(self, config: Optional[BotConfig] = None, *, clock=None, client=None, scanner=None, decision_provider=None):
        self._clock = clock or (lambda: time.time())
        self._decision_provider = decision_provider
        self._decision_controller = None
        self.config = config or load_config()
        if self.config.decision.engine not in ("legacy", "jev"):
            raise ValueError("decision.engine must be legacy or jev; no silent legacy fallback")
        if self.config.decision.engine == "jev":
            from decision.contracts import validate_config
            validate_config(self.config)
            if self.config.trading.mode != "simulation":
                raise ValueError("JEV live execution disabled: use simulation or replay")
        self.client = client or BinanceTrClient(
            api_key=self.config.api.api_key,
            secret_key=self.config.api.secret_key,
            base_url=self.config.api.base_url,
        )
        self.scanner = scanner or MarketScanner(
            clock=self._clock,
            client=self.client,
            max_allowed_spread_pct=self.config.trading.max_allowed_spread_pct,
            btc_dump_shield_pct=self.config.trading.btc_dump_shield_pct,
            btc_dump_cooldown_seconds=self.config.trading.btc_dump_cooldown_seconds,
            quote_asset="TRY",
            min_volume_try=getattr(self.config.trading, "min_24h_volume_try", 5000000.0),
            min_coin_price=getattr(self.config.trading, "min_coin_price", 0.05)
        )
        self.scanner.max_source_age_seconds = self.config.trading.max_radar_age_seconds
        obs_sec = getattr(self.config.trading, "candidate_observation_seconds", 15)
        self.scanner.watchlist.min_observation_seconds = obs_sec
        obs_gain = getattr(self.config.trading, "min_observation_gain_pct", 0.50)
        self.scanner.watchlist.min_gain_pct = obs_gain
        obs_cd = getattr(self.config.trading, "candidate_timeout_cooldown_seconds", 10)
        self.scanner.watchlist.timeout_cooldown_seconds = obs_cd
        obs_burst = getattr(self.config.trading, "candidate_min_burst_count", 1)
        self.scanner.watchlist.min_burst_count = obs_burst
        self.market_engines: Dict[str, MarketDataEngine] = {}
        default_sym = "SOL_TRY" if self.config.trading.symbol == "AUTO" else self.config.trading.symbol
        self.market_data = self.get_engine_for(default_sym)
        self.risk_manager = RiskManager(
            clock=self._clock,
            take_profit_pct=self.config.strategy.take_profit_pct,
            partial_tp_pct=getattr(self.config.strategy, "partial_tp_pct", 0.85),
            partial_tp_ratio=getattr(self.config.strategy, "partial_tp_ratio", 0.50),
            enable_partial_tp=getattr(self.config.strategy, "enable_partial_tp", True),
            stop_loss_pct=self.config.strategy.stop_loss_pct,
            trailing_stop_pct=self.config.strategy.trailing_stop_pct,
            trailing_activation_pct=getattr(self.config.strategy, "trailing_activation_pct", 0.75),
            breakeven_trigger_pct=getattr(self.config.strategy, "breakeven_trigger_pct", 0.45),
            max_holding_seconds=getattr(self.config.strategy, "max_holding_seconds", 300),
            cooldown_seconds=getattr(self.config.strategy, "cooldown_seconds", 5),
            symbol_cooldown_seconds=getattr(self.config.strategy, "symbol_cooldown_seconds", 30),
            loss_cooldown_seconds=getattr(self.config.strategy, "loss_cooldown_seconds", 180),
            max_open_positions=self.config.trading.max_open_positions,
            fee_rate_pct=self.config.trading.fee_rate_pct,
            portfolio_stop_loss_pct=getattr(self.config.strategy, "portfolio_stop_loss_pct", 2.5),
            prevent_rebuy_churn=getattr(self.config.trading, "prevent_rebuy_churn", False),
        )
        self.simulator = SimulatorEngine(
            clock=self._clock,
            initial_balance=self.config.trading.initial_virtual_balance,
            fee_rate_pct=self.config.trading.fee_rate_pct,
        )
        self.live_trader = LiveTraderEngine(
            self.client,
            sync_on_start=(self.config.decision.engine != "jev"),
            symbol=self.config.trading.symbol,
            max_open_positions=self.config.trading.max_open_positions,
        )
        self.report_generator = ReportGenerator()

        self.strategy = self._init_strategy()
        self.is_running = False
        self.stop_completed = False
        self.session_start_time: Optional[float] = None
        self.session_duration_seconds: int = self.config.test.duration_minutes * 60
        self.thread: Optional[threading.Thread] = None
        self.last_report: Optional[Dict[str, str]] = None
        self.latest_snapshot: Dict[str, Any] = {}
        self.logs: list[str] = []
        self.heartbeat_counter: int = 0
        self.step_count: int = 0
        self.session_start_time: Optional[float] = None
        dur_min = getattr(self.config.test, "duration_minutes", 15)
        self.startup_calibration_seconds: float = 20.0 if dur_min <= 5 else 60.0
        self.calibration_end_time: float = 0.0
        self.calibration_completed: bool = False
        self.current_round: int = 1
        self.round_start_time: float = 0.0
        self.round_duration_seconds: int = 60
        is_live = self.config.trading.mode == "live"
        self.current_status_text: str = "Hazır - Canlı İşlem Bekleniyor" if is_live else "Hazır - Test Bekleniyor"

    def _jev(self):
        if self._decision_controller is None:
            from decision.controller import JevController
            self._decision_controller = JevController(self, provider=self._decision_provider)
        return self._decision_controller

    def apply_config(self, new_config: Optional[BotConfig] = None) -> None:
        """
        Yeni veya güncellenmiş yapılandırmayı çalışan tüm bot alt bileşenlerine uygular.
        """
        candidate = new_config or self.config
        if candidate.decision.engine not in ("legacy", "jev"):
            raise ValueError("decision.engine must be legacy or jev; no silent fallback")
        if candidate.decision.engine == "jev":
            from decision.contracts import validate_config
            validate_config(candidate)
            if candidate.trading.mode != "simulation":
                raise ValueError("JEV live execution disabled")
        if self._decision_controller is not None:
            if self.is_running:
                raise ValueError("JEV oturumu sürerken ayarlar değiştirilemez; önce durdurun.")
            self._decision_controller.close()
            self._decision_controller = None
        if new_config:
            self.config = new_config
        if self.config.decision.engine == "jev":
            from decision.contracts import validate_config
            validate_config(self.config)

        # 1. API İstemcisi
        if hasattr(self, "client") and self.client:
            self.client.api_key = self.config.api.api_key
            self.client.secret_key = self.config.api.secret_key
            self.client.base_url = self.config.api.base_url.rstrip("/")
            if getattr(self.client, "session", None) is not None:
                self.client.session.headers.pop("X-MBX-APIKEY", None)

        # 2. Piyasa Radarı & Tarayıcı
        if hasattr(self, "scanner") and self.scanner:
            self.scanner.min_volume_try = getattr(self.config.trading, "min_24h_volume_try", 5000000.0)
            self.scanner.min_coin_price = getattr(self.config.trading, "min_coin_price", 0.05)
            self.scanner.max_source_age_seconds = self.config.trading.max_radar_age_seconds
            self.scanner.max_allowed_spread_pct = self.config.trading.max_allowed_spread_pct
            self.scanner.btc_dump_shield_pct = self.config.trading.btc_dump_shield_pct
            self.scanner.btc_dump_cooldown_seconds = self.config.trading.btc_dump_cooldown_seconds
            self.scanner.watchlist.min_observation_seconds = getattr(self.config.trading, "candidate_observation_seconds", 15)
            self.scanner.watchlist.min_gain_pct = getattr(self.config.trading, "min_observation_gain_pct", 0.50)
            self.scanner.watchlist.timeout_cooldown_seconds = getattr(self.config.trading, "candidate_timeout_cooldown_seconds", 10)
            self.scanner.watchlist.min_burst_count = getattr(self.config.trading, "candidate_min_burst_count", 1)

        # 3. Risk Yöneticisi
        if hasattr(self, "risk_manager") and self.risk_manager:
            self.risk_manager.take_profit_pct = self.config.strategy.take_profit_pct
            self.risk_manager.partial_tp_pct = getattr(self.config.strategy, "partial_tp_pct", 0.85)
            self.risk_manager.partial_tp_ratio = getattr(self.config.strategy, "partial_tp_ratio", 0.50)
            self.risk_manager.enable_partial_tp = getattr(self.config.strategy, "enable_partial_tp", True)
            self.risk_manager.stop_loss_pct = self.config.strategy.stop_loss_pct
            self.risk_manager.trailing_stop_pct = self.config.strategy.trailing_stop_pct
            self.risk_manager.trailing_activation_pct = getattr(self.config.strategy, "trailing_activation_pct", 0.75)
            self.risk_manager.breakeven_trigger_pct = getattr(self.config.strategy, "breakeven_trigger_pct", 0.45)
            self.risk_manager.max_holding_seconds = getattr(self.config.strategy, "max_holding_seconds", 300)
            self.risk_manager.cooldown_seconds = getattr(self.config.strategy, "cooldown_seconds", 5)
            self.risk_manager.symbol_cooldown_seconds = getattr(self.config.strategy, "symbol_cooldown_seconds", 30)
            self.risk_manager.loss_cooldown_seconds = getattr(self.config.strategy, "loss_cooldown_seconds", 180)
            self.risk_manager.max_open_positions = self.config.trading.max_open_positions
            self.risk_manager.fee_rate_pct = self.config.trading.fee_rate_pct
            self.risk_manager.portfolio_stop_loss_pct = getattr(self.config.strategy, "portfolio_stop_loss_pct", 2.5)
            self.risk_manager.prevent_rebuy_churn = getattr(self.config.trading, "prevent_rebuy_churn", False)

        # 4. Simülatör & Canlı İşlem Motoru
        if hasattr(self, "simulator") and self.simulator:
            self.simulator.fee_rate_pct = self.config.trading.fee_rate_pct
        if hasattr(self, "live_trader") and self.live_trader:
            self.live_trader.max_open_positions = self.config.trading.max_open_positions
            self.live_trader.symbol = self.config.trading.symbol

        for engine in self.market_engines.values():
            engine.configure(self.config.strategy, self.config.trading)

        # 5. Strateji
        self.strategy = self._init_strategy()

        # 6. Süre ve Parite
        if not self.is_running:
            self.session_duration_seconds = self.config.test.duration_minutes * 60
            dur_min = getattr(self.config.test, "duration_minutes", 15)
            self.startup_calibration_seconds = 20.0 if dur_min <= 5 else 60.0

        default_sym = "SOL_TRY" if self.config.trading.symbol == "AUTO" else self.config.trading.symbol
        self.market_data = self.get_engine_for(default_sym)
        is_live = self.config.trading.mode == "live"
        if not self.is_running:
            self.current_status_text = "Hazır - Canlı İşlem Bekleniyor" if is_live else "Hazır - Test Bekleniyor"

    def _init_strategy(self):
        strat_name = self.config.strategy.active
        fee_rate = getattr(self.config.trading, "fee_rate_pct", 0.10)
        params = {
            "fee_rate_pct": fee_rate,
            "fee_multiplier": 2.0,
            "rsi_period": self.config.strategy.rsi_period,
            "rsi_oversold": self.config.strategy.rsi_oversold,
            "rsi_overbought": self.config.strategy.rsi_overbought,
            "bollinger_period": self.config.strategy.bollinger_period,
            "bollinger_std_dev": self.config.strategy.bollinger_std_dev,
            "ema_fast": self.config.strategy.ema_fast,
            "ema_slow": self.config.strategy.ema_slow,
            "pullback_pct": 0.15,
            "adx_trend_threshold": 22.0,
        }
        if strat_name in ("fee_recovery", "komisyon_kurtaran"):
            # Komisyon oranını kurtaran risk parametrelerini uygula
            self.risk_manager.apply_fee_recovery_mode(
                fee_rate_pct=fee_rate,
                fee_multiplier=getattr(self.config.strategy, "fee_multiplier", 2.0),
                take_profit_pct=self.config.strategy.take_profit_pct,
                stop_loss_pct=self.config.strategy.stop_loss_pct,
            )
            return FeeRecoveryStrategy(params)
        elif strat_name == "adaptive_regime":
            return AdaptiveRegimeStrategy(params)
        elif strat_name == "momentum_ema":
            return MomentumEmaStrategy(params)
        elif strat_name == "grid_scalper":
            return GridScalperStrategy(params)
        elif strat_name == "quick_test_scalper":
            return QuickTestScalperStrategy(params)
        elif strat_name == "rsi_bollinger":
            return RsiBollingerStrategy(params)
        else:
            self.risk_manager.apply_fee_recovery_mode(
                fee_rate_pct=fee_rate,
                fee_multiplier=getattr(self.config.strategy, "fee_multiplier", 2.0),
                take_profit_pct=self.config.strategy.take_profit_pct,
                stop_loss_pct=self.config.strategy.stop_loss_pct,
            )
            return FeeRecoveryStrategy(params)

    def log(self, message: str) -> None:
        log_entry = f"[{time.strftime('%H:%M:%S')}] {message}"
        logger.info(message)
        self.logs.append(log_entry)
        if len(self.logs) > 300:
            self.logs.pop(0)

    def start(self, duration_minutes: Optional[int] = None) -> None:
        if self.is_running:
            return

        if duration_minutes is not None:
            self.session_duration_seconds = duration_minutes * 60

        if self.config.decision.engine == "jev":
            self._jev().prepare_start()

        # Yeni oturum başlatıldığında tamamlanan işlemleri temizle (oturum raporlaması için)
        if self.config.trading.mode == "live":
            self.live_trader.reset_session_trades()
        else:
            self.simulator.reset_session_trades()

        self.is_running = True
        self.stop_completed = False
        self.session_start_time = self._clock()
        self.calibration_end_time = self.session_start_time + self.startup_calibration_seconds
        self.calibration_completed = False
        self.current_round = 1
        self.round_start_time = self.session_start_time
        self.log(f"🚀 Bot başlatıldı! Mod: {self.config.trading.mode.upper()} | Parite: {self.config.trading.symbol} | Süre: {int(self.session_duration_seconds / 60)} dk")
        if self.config.decision.engine == "jev":
            self.log(f"JEV karar motoru: {self.config.decision.model} | Paper-only | kayıt: {self.config.decision.database_path}")
        else:
            self.log("🔬 [1-DK BAŞLANGIÇ PİYASA KALİBRASYONU] Piyasadaki tüm TRY çiftlerinin ilk 60 saniyelik mikro-fiyat hareketleri taranıyor...")

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self) -> Dict[str, Any]:
        if not self.is_running and self.stop_completed:
            return {"status": "already_stopped"}

        self.is_running = False
        if self.config.decision.engine == "jev":
            try:
                self._jev().end_session()
            except Exception as exc:
                self._jev().faulted = True
                self.log(f"JEV journal finalization failed: {type(exc).__name__}; open exposure may remain")
        self.log("🛑 Bot durduruldu. Kalan açık test pozisyonları realize ediliyor...")
        try:
            if self.config.decision.engine != "jev" and self.config.trading.mode == "simulation" and self.simulator.positions:
                self.force_close_all(reason="Test Oturumu Tamamlandı (Kapanış Realizasyonu)")
        except Exception as e:
            self.log(f"⚠️ Oturum sonu pozisyon kapatma hatası: {e}")

        try:
            report_files = self._generate_final_report()
            self.last_report = report_files
            self.log(f"📊 Rapor kaydedildi: {report_files.get('html')}")
            self.stop_completed = True
            return {"status": "stopped", "report": report_files}
        except Exception as e:
            self.log(f"⚠️ Rapor oluşturma hatası: {e}")
            self.stop_completed = True
            return {"status": "stopped", "error": str(e)}

    def _run_loop(self) -> None:
        while self.is_running:
            try:
                self.step()

                # Test süresi kontrolü
                if self.session_duration_seconds > 0 and self.session_start_time:
                    elapsed = self._clock() - self.session_start_time
                    if elapsed >= self.session_duration_seconds:
                        self.log("⏱️ Test süresi tamamlandı! Otomatik durduruluyor...")
                        self.stop()
                        break

            except Exception as e:
                self.log(f"⚠️ Hata: {str(e)}")

            # 1.5 saniye aralıkla canlı takip (durdurma sinyaline anında yanıt verir)
            for _ in range(15):
                if not self.is_running:
                    break
                time.sleep(0.1)

    def get_engine_for(self, symbol: str) -> MarketDataEngine:
        if symbol not in self.market_engines:
            self.market_engines[symbol] = MarketDataEngine(self.client, symbol=symbol, clock=self._clock)
            self.market_engines[symbol].configure(self.config.strategy, self.config.trading)
        return self.market_engines[symbol]

    def _fresh_quote(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        if not snapshot or snapshot.get("quote_valid") is not True:
            return False
        try:
            bid, ask = float(snapshot["bid"]), float(snapshot["ask"])
            age = self._clock() - float(snapshot["timestamp"])
            max_age = float(self.config.trading.max_market_data_age_seconds)
            return all(math.isfinite(v) for v in (bid, ask, age, max_age)) and 0 < bid <= ask and 0 <= age <= max_age
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def _entry_data_ok(self, snapshot: Optional[Dict[str, Any]], *, require_features: bool = True) -> bool:
        """Shared AUTO/single/manual entry boundary; missing data is not neutral data."""
        if not self._fresh_quote(snapshot):
            return False
        if require_features and snapshot.get("features_ready") is not True:
            return False
        try:
            maximum = float(self.config.trading.max_allowed_spread_pct)
            spread = (float(snapshot["ask"]) - float(snapshot["bid"])) / float(snapshot["bid"]) * 100.0
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return False
        return math.isfinite(maximum) and maximum >= 0 and (maximum == 0 or spread <= maximum)

    def _fresh_radar_pair(self, pair: Dict[str, Any]) -> bool:
        try:
            age = self._clock() - float(pair["updated_at"])
            if not math.isfinite(age) or not 0 <= age <= self.config.trading.max_radar_age_seconds:
                return False
            source_time = pair.get("source_close_time_ms")
            if source_time is not None:
                source_age = self._clock() - float(source_time) / 1000.0
                if not math.isfinite(source_age) or not 0 <= source_age <= self.config.trading.max_radar_age_seconds:
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _rollover_reference(position: Dict[str, Any], price: float) -> None:
        # Execution/cost-basis fields are immutable across a no-trade rollover.
        position["risk_reference_price"] = price
        position["risk_highest_price"] = price
        position["breakeven_locked"] = False

    def force_test_buy(self, symbol: Optional[str] = None, budget: Optional[float] = None, reason: str = "Manuel Test Alımı") -> Optional[Dict[str, Any]]:
        """
        Kullanıcının hemen pozisyon açıp TP/SL'i izleyebilmesi için anında alım yapar.
        Sembol belirtilmemişse radarın bulduğu 1 numaralı en hareketli coini otomatik seçer.
        """
        if self.config.decision.engine == "jev":
            return self._jev().manual_buy(symbol, budget, reason)
        target_symbol = symbol
        if not target_symbol or target_symbol == "AUTO":
            top = self.scanner.scan_top_active_pairs(limit=3)
            target_symbol = top[0]["symbol"] if top else "SOL_TRY"

        engine = self.get_engine_for(target_symbol)
        snapshot = engine.update_market_state()
        price = snapshot.get("ask", 0.0) if snapshot else 0.0

        if not self._entry_data_ok(snapshot, require_features=False):
            self.log(f"⚠️ {target_symbol} için anlık fiyat alınamadı.")
            return None

        can_open, _ = self.risk_manager.can_open_position(
            len(self.simulator.positions if self.config.trading.mode == "simulation" else self.live_trader.positions),
            is_basket_filling=True, symbol=target_symbol,
        )
        if not can_open:
            return None
        trade_budget = self.config.trading.budget_per_trade if budget is None else budget
        # Do not silently inject cash into a paper account when funds run out.
        if self.config.trading.mode == "simulation":
            trade_budget = min(trade_budget, self.simulator.cash)

        self.log(f"⚡ ANINDA TEST ALIMI TETİKLENDİ: Parite={target_symbol}, Fiyat={price:.4f} TL, Bütçe={trade_budget:.2f} TL")

        if self.config.trading.mode == "simulation":
            pos = self.simulator.buy(target_symbol, price, trade_budget, reason=reason)
            if pos:
                self.simulator.update_market_price(target_symbol, snapshot["bid"])
                self.risk_manager.record_trade_entry(target_symbol)
                self.log(f"🟢 Sanal Pozisyon Açıldı [{target_symbol}]: Miktar={pos['quantity']:.4f} | Maliyet={trade_budget:.2f} TL")
                return pos
        else:
            pos = self.live_trader.buy(target_symbol, price, trade_budget, reason=reason)
            if pos:
                self.risk_manager.record_trade_entry(target_symbol)
                self.log(f"🟢 CANLI Pozisyon Açıldı [{target_symbol}]: Miktar={pos['quantity']:.4f}")
                return pos
        return None

    def force_close_all(self, reason: str = "Manuel Pozisyon Kapatma") -> list:
        if self.config.decision.engine == "jev":
            return self._jev().manual_close_all(reason)
        closed = []
        if self.config.trading.mode == "simulation":
            for pos_id, pos in list(self.simulator.positions.items()):
                try:
                    sym = pos["symbol"]
                    engine = self.get_engine_for(sym)
                    snap = engine.update_market_state() if engine else None
                    if not self._fresh_quote(snap):
                        self.log(f"⚠️ [{sym}] Güncel tahta yok; pozisyon kapatılmadı.")
                        continue
                    price = snap["bid"]
                    order = self.simulator.sell(pos_id, price, reason=reason)
                    if order:
                        closed.append(order)
                        self.risk_manager.record_trade_exit(sym, is_loss=order.get("net_pnl", 0.0) < 0)
                        self.log(f"🛑 Pozisyon Kapatıldı [{sym}]: Net K/Z={order['net_pnl']:.2f} TL ({order['pnl_pct']:.2f}%)")
                except Exception as ex:
                    self.log(f"⚠️ Pozisyon kapatma hatası [{pos.get('symbol')}]: {ex}")
        else:
            for pos_id, pos in list(self.live_trader.positions.items()):
                try:
                    sym = pos["symbol"]
                    engine = self.get_engine_for(sym)
                    snap = engine.update_market_state() if engine else None
                    if not self._fresh_quote(snap):
                        self.log(f"⚠️ [{sym}] Güncel tahta yok; pozisyon kapatılmadı.")
                        continue
                    price = snap["bid"]
                    order = self.live_trader.sell(pos_id, price, reason=reason)
                    if order:
                        closed.append(order)
                        self.risk_manager.record_trade_exit(sym, is_loss=order.get("net_pnl", 0.0) < 0)
                        self.log(f"🛑 Canlı Pozisyon Kapatıldı [{sym}]: Net K/Z={order['net_pnl']:.2f} TL")
                except Exception as ex:
                    self.log(f"⚠️ Canlı pozisyon kapatma hatası [{pos.get('symbol')}]: {ex}")
        return closed

    def close_single_position(self, position_id: str, reason: str = "Manuel Satış") -> Optional[Dict[str, Any]]:
        if self.config.decision.engine == "jev":
            return self._jev().manual_close(position_id, reason)
        mode = self.config.trading.mode
        if mode == "simulation":
            pos = self.simulator.positions.get(position_id)
            if not pos:
                return None
            sym = pos["symbol"]
            engine = self.get_engine_for(sym)
            snap = engine.update_market_state()
            if not self._fresh_quote(snap):
                return None
            price = snap["bid"]
            order = self.simulator.sell(position_id, price, reason=reason)
            if order:
                self.risk_manager.record_trade_exit(sym, is_loss=order.get("net_pnl", 0.0) < 0)
                self.log(f"🛑 Manuel Pozisyon Kapatıldı [{sym}]: Net K/Z={order['net_pnl']:.2f} TL (%{order['pnl_pct']:.2f})")
                return order
        else:
            pos = self.live_trader.positions.get(position_id)
            if not pos:
                return None
            sym = pos["symbol"]
            engine = self.get_engine_for(sym)
            snap = engine.update_market_state()
            if not self._fresh_quote(snap):
                return None
            price = snap["bid"]
            order = self.live_trader.sell(position_id, price, reason=reason)
            if order:
                self.risk_manager.record_trade_exit(sym, is_loss=order.get("net_pnl", 0.0) < 0)
                self.log(f"🛑 Canlı Manuel Pozisyon Kapatıldı [{sym}]: Net K/Z={order['net_pnl']:.2f} TL")
                return order
        return None

    def step(self) -> None:
        """
        Her döngü adımı:
        1. Açık pozisyonların anlık fiyatlarını ve TP/SL durumlarını kontrol et
        2. Bütçe ve pozisyon limitine göre yeni işlem hakkı var mı bak
        3. 'auto_select_coin' aktifse en hareketli coinleri tara ve uygun olanı yakala
        """
        if self.config.decision.engine == "jev":
            self._jev().step()
            return
        self.step_count += 1
        mode = self.config.trading.mode

        # 1. Açık Pozisyonları Güncelle & Kâr Al / Stop Loss Değerlendir
        top_leaders = [p["symbol"] for p in getattr(self.scanner, "cached_top_pairs", [])[:3] if self._fresh_radar_pair(p)]
        if mode == "simulation":
            open_positions = list(self.simulator.positions.values())
            for pos in open_positions:
                sym = pos["symbol"]
                engine = self.get_engine_for(sym)
                snap = engine.update_market_state()
                if not self._fresh_quote(snap):
                    continue
                cur_p = snap["bid"]
                if cur_p > 0:
                    self.simulator.update_market_price(sym, cur_p)

                if cur_p <= 0 or pos.get("entry_price", 0.0) <= 0:
                    continue

                should_close, reason, pnl_pct = self.risk_manager.evaluate_exit(pos, cur_p)
                if should_close:
                    is_profitable = pnl_pct > 0 and ("TAKE-PROFIT" in reason or "TRAILING" in reason or "BREAKEVEN" in reason or "KADEMELİ KÂR AL" in reason)
                    strat_sig, _ = self.strategy.evaluate(snap, len(open_positions)) if snap and snap.get("features_ready") else ("HOLD", "")
                    is_top = sym in top_leaders and bool(snap and snap.get("features_ready"))
                    rollover_ok, rollover_msg = self.risk_manager.should_rollover_position(
                        pos, cur_p, is_profitable_exit=is_profitable, strategy_signal=strat_sig, is_top_leader=is_top
                    )
                    if rollover_ok and "KADEMELİ KÂR AL" not in reason:
                        self._rollover_reference(pos, cur_p)
                        self.log(f"🔄 POZİSYON DEVRİ [{sym}]: Kâr seviyesine ulaşıldı (+%{pnl_pct:.2f}), trend sürdüğü ve coin lider kaldığı için satılmadan pozisyon devredildi. Yeni taban fiyat: {cur_p:.4f} TL")
                        continue

                    sell_fraction = 1.0
                    if "KADEMELİ KÂR AL" in reason or "TP1" in reason:
                        sell_fraction = getattr(self.config.strategy, "partial_tp_ratio", 0.50)

                    self.log(f"🎯 POZİSYON KAPANIŞI [{sym}]: {reason}")
                    trade = self.simulator.sell(pos["position_id"], cur_p, fraction=sell_fraction, reason=reason)
                    if trade:
                        is_loss_exit = trade.get("net_pnl", 0.0) < 0 or "STOP-LOSS" in reason
                        if sell_fraction >= 1.0:
                            self.risk_manager.record_trade_exit(sym, is_loss=is_loss_exit)
                        self.log(f"✅ Satış Gerçekleşti [{sym}]: Fiyat={cur_p:.4f} TL | Net K/Z={trade['net_pnl']:.2f} TL (%{trade['pnl_pct']:.2f})")

            open_count = len(self.simulator.positions)
            # Portföy Düzeyinde Toplam Kâr/Zarar ve %2 Zarar Kes Kontrolü (Simülasyon)
            summary = self.simulator.get_summary()
            port_pnl_pct = summary.get("total_pnl_pct", 0.0)
            port_sl_triggered, port_reason = self.risk_manager.evaluate_portfolio_stop_loss(port_pnl_pct)
            if port_sl_triggered:
                self.log(f"🚨 {port_reason}! Sermaye kaybını sınırlamak için TÜM AÇIK POZİSYONLAR SATILIYOR VE BOT DURDURULUYOR...")
                self.force_close_all(reason=port_reason)
                self.current_status_text = f"🚨 Portföy Stop-Loss Tetiklendi (%{port_pnl_pct:.2f}) - Bot Durduruldu"
                self.stop()
                return
        else:
            open_positions = list(self.live_trader.positions.values())
            for pos in open_positions:
                sym = pos["symbol"]
                engine = self.get_engine_for(sym)
                snap = engine.update_market_state()
                if not self._fresh_quote(snap):
                    continue
                cur_p = snap["bid"]
                if cur_p > 0:
                    self.live_trader.update_market_price(sym, cur_p)

                if cur_p <= 0 or pos.get("entry_price", 0.0) <= 0:
                    continue

                should_close, reason, pnl_pct = self.risk_manager.evaluate_exit(pos, cur_p)
                if should_close:
                    is_profitable = pnl_pct > 0 and ("TAKE-PROFIT" in reason or "TRAILING" in reason or "BREAKEVEN" in reason or "KADEMELİ KÂR AL" in reason)
                    strat_sig, _ = self.strategy.evaluate(snap, len(open_positions)) if snap and snap.get("features_ready") else ("HOLD", "")
                    is_top = sym in top_leaders and bool(snap and snap.get("features_ready"))
                    rollover_ok, rollover_msg = self.risk_manager.should_rollover_position(
                        pos, cur_p, is_profitable_exit=is_profitable, strategy_signal=strat_sig, is_top_leader=is_top
                    )
                    if rollover_ok and "KADEMELİ KÂR AL" not in reason:
                        self._rollover_reference(pos, cur_p)
                        self.log(f"🔄 CANLI POZİSYON DEVRİ [{sym}]: Kâr (+%{pnl_pct:.2f}) sonrası pozisyon devredildi. Yeni taban: {cur_p:.4f} TL")
                        continue

                    sell_fraction = 1.0
                    if "KADEMELİ KÂR AL" in reason or "TP1" in reason:
                        sell_fraction = getattr(self.config.strategy, "partial_tp_ratio", 0.50)

                    self.log(f"🎯 CANLI KAPANIŞ [{sym}]: {reason}")
                    trade = self.live_trader.sell(pos["position_id"], cur_p, fraction=sell_fraction, reason=reason)
                    if trade:
                        is_loss_exit = trade.get("net_pnl", 0.0) < 0 or "STOP-LOSS" in reason
                        if sell_fraction >= 1.0:
                            self.risk_manager.record_trade_exit(sym, is_loss=is_loss_exit)
                        self.log(f"✅ Canlı Satış [{sym}]: Fiyat={cur_p:.4f} TL | Net K/Z={trade['net_pnl']:.2f} TL")

            open_count = len(self.live_trader.positions)
            # Canlı Portföy Düzeyinde %2 Zarar Kes Kontrolü (Canlı Hesap)
            summary = self.live_trader.get_summary()
            port_pnl_pct = summary.get("total_pnl_pct", 0.0)
            port_sl_triggered, port_reason = self.risk_manager.evaluate_portfolio_stop_loss(port_pnl_pct)
            if port_sl_triggered:
                self.log(f"🚨 {port_reason}! Canlı hesap sermaye kaybını sınırlamak için TÜM CANLI POZİSYONLAR SATILIYOR VE BOT DURDURULUYOR...")
                self.force_close_all(reason=port_reason)
                self.current_status_text = f"🚨 Canlı Portföy Stop-Loss Tetiklendi (%{port_pnl_pct:.2f}) - Bot Durduruldu"
                self.stop()
                return

        # 2. Yeni İşlem Açılabilir mi?
        can_open, open_reason = self.risk_manager.can_open_position(open_count)

        # 3. Otomatik Tarama veya Tekil Parite İncelemesi
        is_auto = self.config.trading.auto_select_coin or self.config.trading.symbol == "AUTO"

        if is_auto:
            target_count = getattr(self.config.trading, "target_coins_count", 5)
            only_up = getattr(self.config.trading, "only_uptrend", True)
            min_gain = getattr(self.config.trading, "min_24h_gain_pct", 0.0)
            
            # Tüm piyasayı anlık tara ve mikro tickleri kaydet
            top_pairs = self.scanner.scan_top_active_pairs(
                limit=getattr(self.config.trading, "top_coins_limit", 0),
                only_uptrend=only_up,
                min_gain_pct=min_gain,
            )
            open_positions = list(self.simulator.positions.values() if mode == "simulation" else self.live_trader.positions.values())
            open_symbols = {p["symbol"] for p in open_positions}
            slots_needed = max(0, target_count - open_count)

            now = self._clock()
            if self.round_start_time <= 0:
                self.round_start_time = now

            round_elapsed = int(now - self.round_start_time)
            
            is_calibrating = (
                self.is_running 
                and self.session_start_time is not None 
                and (now < self.calibration_end_time) 
                and not self.calibration_completed
                and open_count == 0
            )
            calib_elapsed = int(now - self.session_start_time) if self.session_start_time else 0
            calib_rem = max(0, int(self.calibration_end_time - now))

            # 1. BAŞLANGIÇ 1-DAKİKALIK KALİBRASYON FAZI
            if is_calibrating:
                self.current_status_text = f"⏳ 1-Dk Piyasa Kalibrasyonu: {calib_elapsed}/60s (Mikro-momentum haritası çıkarılıyor)"
                self.heartbeat_counter += 1
                if self.heartbeat_counter % 3 == 0:
                    leaders = [f"{p['symbol']} (%{p.get('velocity_1m_pct', 0.0):+.2f} 1m)" for p in top_pairs[:4]]
                    self.log(f"⏳ [1-DK KALİBRASYON ({calib_elapsed}/60s)] Piyasa mikro-akışı taranıyor... Anlık Liderler: {' | '.join(leaders)}")
                return

            if not self.calibration_completed and self.session_start_time and open_count == 0:
                self.calibration_completed = True
                self.log("🚀 [1-DK KALİBRASYON TAMAMLANDI] 60 saniyelik mikro-momentum analizi sonuçlandı. Gerçek pozitif ivmeye (%+0.10 ve üzeri) sahip lider coinler alınıyor...")

            # Durum metnini güncelle
            if open_count > 0:
                self.current_status_text = f"Dinamik Portföy: {open_count}/{target_count} Coin Aktif | Tur #{self.current_round} ({round_elapsed}/60s)"
            else:
                top_names = [f"{p['symbol']} (%{p.get('velocity_1m_pct', p['change_pct']):+.1f} 1m)" for p in top_pairs[:3]]
                self.current_status_text = f"Tur #{self.current_round} ({round_elapsed}/60s): 1m Liderler [{', '.join(top_names)}]"

            # Periyodik Log
            self.heartbeat_counter += 1
            if self.heartbeat_counter % 4 == 0:
                if open_count > 0:
                    pos_summaries = [f"{pos['symbol']}: {pos.get('unrealized_pnl', 0):+.2f}TL (%{pos.get('unrealized_pnl_pct', 0):+.2f})" for pos in open_positions[:5]]
                    self.log(f"📊 [PORTFÖY DURUMU ({open_count}/{target_count} COİN - TUR #{self.current_round})] {' | '.join(pos_summaries)}")
                else:
                    obs_summaries = [f"{p['symbol']}: %{p.get('velocity_1m_pct', 0.0):+.2f} (1m Hız | Skor: {p.get('quant_score', 0)})" for p in top_pairs[:4]]
                    self.log(f"🔍 [1-DK QUANT RADARI - TUR #{self.current_round} ({round_elapsed}/60s)] {' | '.join(obs_summaries)}")

            if slots_needed > 0:
                # Oturum Sonu Koruması: Test süresinin bitimine 60 saniyeden az kaldıysa yeni pozisyon açma
                if self.session_duration_seconds > 0 and self.session_start_time:
                    remaining_session_sec = (self.session_start_time + self.session_duration_seconds) - now
                    if remaining_session_sec < 60.0:
                        slots_needed = 0

                # BTC Market Beta Shield Kontrolü (BTC ani düşüşte altcoin alımlarını dondurur)
                if slots_needed > 0 and hasattr(self.scanner, "is_btc_dumping"):
                    is_btc_dump, btc_msg = self.scanner.is_btc_dumping()
                    if is_btc_dump:
                        if self.heartbeat_counter % 3 == 0:
                            self.log(f"{btc_msg}")
                        return

                if mode == "simulation":
                    available_cash = self.simulator.cash
                else:
                    available_cash = self.live_trader.get_real_balances().get("TRY", 0.0)

                per_coin_budget = min(self.config.trading.budget_per_trade, available_cash / max(1, slots_needed))
                obs_sec = getattr(self.config.trading, "candidate_observation_seconds", 60)
                if hasattr(self.scanner, "watchlist"):
                    obs_sec = min(obs_sec, getattr(self.scanner.watchlist, "min_observation_seconds", obs_sec))
                min_req_gain = getattr(self.config.trading, "min_observation_gain_pct", 0.10) if obs_sec > 0 else 0.0

                require_strict = getattr(self.config.trading, "require_strict_buy_signal", True)
                # Radarı pozitif 1m ivmesi olan, en az 5 farklı coini doldurabilecek taze liderlere odakla
                candidate_pairs = [
                    p for p in top_pairs 
                    if p["symbol"] not in open_symbols
                    and self._fresh_radar_pair(p)
                    and (obs_sec == 0 or p.get("is_qualified_1m", False)) 
                    and not p.get("is_dumping", False)
                    and not self.scanner.tracker.is_cooling_down(p["symbol"])
                    and (p.get("velocity_1m_pct", 0.0) >= min_req_gain or obs_sec == 0)
                    and p.get("quant_score", 10.0) >= 6.3
                ][:max(10, slots_needed * 2)]

                for pair in candidate_pairs:
                    if slots_needed <= 0:
                        break

                    sym = pair["symbol"]
                    if self.scanner.tracker.is_cooling_down(sym):
                        continue

                    velo_1m = pair.get("velocity_1m_pct", 0.0)
                    if obs_sec > 0 and velo_1m < min_req_gain:
                        continue

                    can_buy_slot, _ = self.risk_manager.can_open_position(open_count, is_basket_filling=True, symbol=sym)
                    if not can_buy_slot:
                        continue

                    engine = self.get_engine_for(sym)
                    raw_snap = engine.update_market_state()
                    if not self._entry_data_ok(raw_snap):
                        continue
                    snap = dict(raw_snap)
                    cur_p = snap["bid"]
                    buy_price = snap["ask"]

                    # Radar mikro-metriklerini strateji snapshot'ına aktar (Rejim ve Kırılım Teyidi için)
                    snap["velocity_1m_pct"] = velo_1m
                    snap["quant_score"] = pair.get("quant_score", 0.0)
                    snap["volume_surge_ratio"] = pair.get("volume_surge_ratio", 1.0)
                    snap["burst_ratio"] = pair.get("burst_ratio", 0.0)
                    snap["change_24h_pct"] = pair.get("change_pct", 0.0)
                    snap["is_squeeze_breakout"] = pair.get("is_squeeze_breakout", False)
                    snap["radar"] = dict(pair)
                    snap["radar_source"] = pair.get("source", "client_unspecified")
                    snap["volume_surge_is_proxy"] = pair.get("volume_surge_is_proxy", True)
                    self.latest_snapshot = snap

                    signal, signal_reason = self.strategy.evaluate(snap, open_count)
                    require_strict = getattr(self.config.trading, "require_strict_buy_signal", True)
                    auto_fill = getattr(self.config.trading, "auto_fill_portfolio", True)
                    
                    if require_strict:
                        should_buy = (signal == "BUY")
                    else:
                        is_overbought_or_dump = (
                            "Aşırı Alım" in signal_reason 
                            or "ÇÖKÜŞ" in signal_reason 
                            or "DEFENSIVE" in signal_reason 
                            or snap.get("rsi", 50.0) > 68.0
                        )
                        should_buy = (signal == "BUY") or (auto_fill and not is_overbought_or_dump)

                    if not should_buy:
                        continue

                    # 10 Saniyelik Ön-Alım Düşüş Koruması (Pre-Buy Sniper Anti-Dump Filter)
                    prebuy_sec = getattr(self.config.trading, "candidate_prebuy_seconds", 10) if obs_sec > 0 else 0
                    if hasattr(self.scanner, "watchlist") and hasattr(self.scanner.watchlist, "observe_prebuy"):
                        is_prebuy_ok, prebuy_msg, prebuy_el = self.scanner.watchlist.observe_prebuy(
                            sym, cur_p, observation_seconds=prebuy_sec, max_allowed_drop_pct=0.30
                        )
                        if not is_prebuy_ok:
                            if "iptal edildi" in prebuy_msg or "düşüş" in prebuy_msg or "Durgunluk" in prebuy_msg:
                                self.log(f"🛑 [{sym}] {prebuy_msg}")
                            continue

                    quant_score = pair.get("quant_score", 0.0)
                    buy_reason = f"{signal_reason} (1m Hız: %{velo_1m:+.2f} | 10s Teyitli | Quant Skor: {quant_score})"

                    if per_coin_budget < 10.0:
                        self.log(f"⚠️ [{sym}] Alım bütçesi yetersiz ({per_coin_budget:.2f} TL < 10 TL minimum). Alım atlandı.")
                        continue

                    if mode == "simulation":
                        pos = self.simulator.buy(sym, buy_price, per_coin_budget, reason=buy_reason)
                        if pos:
                            self.simulator.update_market_price(sym, cur_p)
                            open_symbols.add(sym)
                            open_count += 1
                            slots_needed -= 1
                            available_cash -= per_coin_budget
                            self.risk_manager.record_trade_entry(sym)
                            self.log(f"🟢 Portföye Eklendi [{sym}]: Miktar={pos['quantity']:.4f} | Maliyet={per_coin_budget:.2f} TL | Sepet: {open_count}/{target_count} Coin Dolu | Neden: {buy_reason}")
                    else:
                        pos = self.live_trader.buy(sym, buy_price, per_coin_budget, reason=buy_reason)
                        if pos:
                            open_symbols.add(sym)
                            open_count += 1
                            slots_needed -= 1
                            available_cash -= per_coin_budget
                            self.risk_manager.record_trade_entry(sym)
                            self.log(f"🟢 CANLI Portföye Eklendi [{sym}]: Miktar={pos['quantity']:.4f} | Maliyet={per_coin_budget:.2f} TL | Sepet: {open_count}/{target_count} Coin Dolu | Neden: {buy_reason}")

            # Her 60 saniyede bir 1-dakikalık tur sıfırlanır ve taze referans fiyatlar atanır
            if round_elapsed >= self.round_duration_seconds and (not is_calibrating):
                self.current_round += 1
                self.round_start_time = now
                self.scanner.reset_round()
                self.log(f"🔄 [1-DK TUR #{self.current_round-1} TAMAMLANDI] Tüm piyasa referans fiyatları sıfırlandı. Taze referanslarla Tur #{self.current_round} başlatıldı.")
        else:
            # Tekil parite modu (örn. USDT_TRY)
            sym = self.config.trading.symbol
            engine = self.get_engine_for(sym)
            snap = engine.update_market_state()
            if not self._entry_data_ok(snap):
                self.current_status_text = "Güncel ve geçerli piyasa verisi bekleniyor"
                return

            self.latest_snapshot = snap
            cur_p = snap["ask"]
            can_open, open_reason = self.risk_manager.can_open_position(open_count, symbol=sym)
            signal, signal_reason = self.strategy.evaluate(snap, open_count)

            if open_count > 0:
                self.current_status_text = f"Pozisyon Takip Ediliyor ({open_count} açık)"
            else:
                self.current_status_text = f"Piyasa Taranıyor: {signal_reason}"

            if can_open and signal == "BUY":
                budget = self.config.trading.budget_per_trade
                self.log(f"⚡ ALIŞ SİNYALİ [{sym}]: {signal_reason}")
                if mode == "simulation":
                    pos = self.simulator.buy(sym, cur_p, budget, reason=signal_reason)
                    if pos:
                        self.simulator.update_market_price(sym, snap["bid"])
                        self.risk_manager.record_trade_entry(sym)
                        self.log(f"🟢 Sanal Alış Açıldı [{sym}]: Fiyat={cur_p:.4f} TL | Maliyet={budget:.2f} TL")
                else:
                    pos = self.live_trader.buy(sym, cur_p, budget, reason=signal_reason)
                    if pos:
                        self.risk_manager.record_trade_entry(sym)
                        self.log(f"🟢 CANLI Alış Açıldı [{sym}]: Fiyat={cur_p:.4f} TL")

    def _generate_final_report(self) -> Dict[str, str]:
        current_price = self.latest_snapshot.get("price", 0.0)
        if self.config.trading.mode == "live":
            summary = self.live_trader.get_summary(current_price)
            closed_trades = self.live_trader.closed_trades
            open_positions = list(self.live_trader.positions.values())
            equity_curve = []
        else:
            summary = self.simulator.get_summary(current_price)
            closed_trades = self.simulator.closed_trades
            open_positions = list(self.simulator.positions.values())
            equity_curve = self.simulator.equity_curve

        metrics = PerformanceMetrics.calculate(
            initial_balance=summary["initial_balance"],
            final_equity=summary["total_equity"],
            closed_trades=closed_trades,
            equity_curve=equity_curve,
        )

        cfg_dict = {
            "trading": {
                "mode": self.config.trading.mode,
                "symbol": self.config.trading.symbol,
                "budget_per_trade": self.config.trading.budget_per_trade,
            },
            "strategy": {
                "active": self.config.strategy.active,
                "take_profit_pct": self.config.strategy.take_profit_pct,
                "stop_loss_pct": self.config.strategy.stop_loss_pct,
            },
            "test": {
                "duration_minutes": int(self.session_duration_seconds / 60) if self.session_duration_seconds else 0,
            }
        }

        return self.report_generator.generate_report(
            config_data=cfg_dict,
            metrics=metrics,
            closed_trades=closed_trades,
            equity_curve=equity_curve,
            logs=list(self.logs),
            open_positions=open_positions,
        )

    def get_dashboard_state(self) -> Dict[str, Any]:
        """
        Web paneli için anlık durum bilgisi.
        Ağ çağrısı yapmadan tamamen bellek içi (in-memory) çalışarak sunucu ve arayüz donmasını önler.
        """
        is_live = self.config.trading.mode == "live"
        current_price = self.latest_snapshot.get("price", 0.0)
        if is_live:
            summary = self.live_trader.get_summary(current_price)
        else:
            summary = self.simulator.get_summary(current_price)

        now = self._clock()
        elapsed_seconds = 0
        remaining_seconds = 0
        if self.is_running and self.session_start_time:
            elapsed_seconds = int(now - self.session_start_time)
            if self.session_duration_seconds > 0:
                remaining_seconds = max(0, self.session_duration_seconds - elapsed_seconds)

        is_calibrating = (
            self.is_running
            and self.config.decision.engine != "jev"
            and self.session_start_time is not None 
            and (now < self.calibration_end_time)
            and not self.calibration_completed
        )
        calib_elapsed = min(self.startup_calibration_seconds, elapsed_seconds)
        calib_rem = max(0, int(self.calibration_end_time - now)) if is_calibrating else 0
        round_elapsed = int(now - self.round_start_time) if (self.is_running and self.round_start_time > 0) else 0

        radar_pairs = getattr(self.scanner, "cached_top_pairs", []) or []
        if not radar_pairs and hasattr(self, "scanner") and self.config.decision.engine != "jev":
            radar_pairs = self.scanner.scan_top_active_pairs(limit=0, force_refresh=False)

        return {
            "decision_engine": self.config.decision.engine,
            "decision": self._decision_controller.status() if self._decision_controller else {"engine": self.config.decision.engine, "model": self.config.decision.model},
            "is_running": self.is_running,
            "is_calibrating": is_calibrating,
            "current_round": self.current_round,
            "round_elapsed": round_elapsed,
            "round_duration": self.round_duration_seconds,
            "calibration": {
                "active": is_calibrating,
                "elapsed": calib_elapsed,
                "total": self.startup_calibration_seconds,
                "remaining": calib_rem,
                "progress_pct": min(100, int((calib_elapsed / max(1, self.startup_calibration_seconds)) * 100)),
            },
            "mode": self.config.trading.mode,
            "symbol": self.config.trading.symbol,
            "strategy": self.config.strategy.active,
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": remaining_seconds,
            "session_duration_minutes": int(self.session_duration_seconds / 60),
            "market": self.latest_snapshot,
            "portfolio": summary,
            "config": {
                **config_to_dict(self.config, mask_secrets=True).get("trading", {}),
                **config_to_dict(self.config, mask_secrets=True).get("strategy", {}),
                "loaded_config_path": getattr(self.config, "loaded_config_path", "config.test.yaml"),
            },
            "full_config": config_to_dict(self.config, mask_secrets=True),
            "loaded_config_path": getattr(self.config, "loaded_config_path", "config.test.yaml"),
            "status_text": self.current_status_text,
            "current_regime": getattr(self.strategy, "current_regime", "RANGING"),
            "step_count": self.step_count,
            "radar_top_coins": radar_pairs,
            "logs": self.logs[-60:],
            "last_report": self.last_report,
        }
