import time
from typing import Dict, Any, Optional, Tuple

class RiskManager:
    """
    Kurumsal Seviye Risk ve Pozisyon Yönetim Motoru (Institutional Quant Risk Manager).
    
    Özellikler:
    1. Pozitif Asimetrik Risk/Ödül (R:R >= 1.5:1): TP: %2.0, SL: %1.0
    2. Breakeven Kilidi: +%0.90 kâr görüldüğünde stop maliyet + komisyona çekilir (Sıfır Risk).
    3. Dinamik Trailing Stop: Trend güçlendikçe zirveden %0.50 takip mesafesi ile kârı maksimize eder.
    4. Stagnation / Zaman Aşımı Çıkışı: 15 dakika hareketsiz kalan pozisyonu kapatıp taze fırsata geçer.
    5. Anti-Revenge Loss Cooldown: Zarar kesilen coine 3 dakika soğuma vererek tekrar düşen bıçak tutmayı engeller.
    6. Portföy Devre Kesici: Toplam portföy sermaye kaybı %2.5'e ulaştığında acil durdurma yapar.
    """
    def __init__(
        self,
        take_profit_pct: float = 1.60,
        partial_tp_pct: float = 0.85,
        partial_tp_ratio: float = 0.50,
        enable_partial_tp: bool = False,
        stop_loss_pct: float = 1.0,
        trailing_stop_pct: float = 0.35,
        trailing_activation_pct: float = 0.75,
        breakeven_trigger_pct: float = 0.45,
        max_holding_seconds: int = 300,
        cooldown_seconds: int = 5,
        symbol_cooldown_seconds: int = 30,
        loss_cooldown_seconds: int = 180,
        max_open_positions: int = 5,
        fee_rate_pct: float = 0.10,
        portfolio_stop_loss_pct: float = 2.5,
        prevent_rebuy_churn: bool = False,
        clock=None,
    ):
        self._clock = clock or (lambda: time.time())
        self.take_profit_pct = take_profit_pct
        self.partial_tp_pct = partial_tp_pct
        self.partial_tp_ratio = partial_tp_ratio
        self.enable_partial_tp = enable_partial_tp
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.trailing_activation_pct = trailing_activation_pct
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.max_holding_seconds = max_holding_seconds
        self.cooldown_seconds = cooldown_seconds
        self.symbol_cooldown_seconds = symbol_cooldown_seconds
        self.loss_cooldown_seconds = loss_cooldown_seconds
        self.max_open_positions = max_open_positions
        self.fee_rate_pct = fee_rate_pct
        self.portfolio_stop_loss_pct = portfolio_stop_loss_pct
        self.prevent_rebuy_churn = prevent_rebuy_churn
        self.last_trade_time: float = 0.0
        self.symbol_exit_times: Dict[str, float] = {}
        self.symbol_loss_exit_times: Dict[str, float] = {}

    def apply_fee_recovery_mode(
        self,
        fee_rate_pct: Optional[float] = None,
        fee_multiplier: float = 2.0,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
    ) -> None:
        """Risk parametrelerini strateji yapılandırmasına göre senkronize eder."""
        if fee_rate_pct is not None:
            self.fee_rate_pct = fee_rate_pct
        self.take_profit_pct = take_profit_pct if take_profit_pct is not None else 2.0
        self.stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else 1.0
        self.trailing_activation_pct = max(0.60, self.take_profit_pct * 0.50)
        self.trailing_stop_pct = max(0.30, self.take_profit_pct * 0.25)
        self.breakeven_trigger_pct = max(0.50, self.take_profit_pct * 0.45)

    def should_rollover_position(
        self,
        position: Dict[str, Any],
        current_price: float,
        is_profitable_exit: bool,
        strategy_signal: str,
        is_top_leader: bool
    ) -> Tuple[bool, str]:
        if not self.prevent_rebuy_churn:
            return False, "Devir koruması devre dışı (Doğrudan kâr satışı)"
        if not is_profitable_exit:
            return False, "Zarar kes durumunda devir yapılamaz"
        if strategy_signal == "BUY" or is_top_leader:
            return True, "Pozisyon devredildi"
        return False, "Devir koşulu sağlanmadı"

    def evaluate_portfolio_stop_loss(self, total_pnl_pct: float) -> Tuple[bool, str]:
        """Tüm portföy düzeyinde sermaye koruma devresini kontrol eder."""
        if total_pnl_pct <= -self.portfolio_stop_loss_pct:
            return True, f"🚨 PORTFÖY STOP-LOSS: Portföy %{total_pnl_pct:.2f} zararda (Eşik: -%{self.portfolio_stop_loss_pct:.2f})"
        return False, "Portföy seviyesi güvenli"

    def can_open_position(
        self,
        current_positions_count: int,
        is_basket_filling: bool = False,
        symbol: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Yeni bir pozisyon açılabilir mi kontrol eder."""
        if current_positions_count >= self.max_open_positions:
            return False, f"Maksimum açık pozisyon limitine ulaşıldı ({self.max_open_positions})"

        now = self._clock()

        # Zarar kes sonrası ceza beklemesi (Düşen bıçağı tekrar tutmama)
        if symbol and symbol in self.symbol_loss_exit_times:
            elapsed_loss = now - self.symbol_loss_exit_times[symbol]
            if elapsed_loss < self.loss_cooldown_seconds:
                remaining = int(self.loss_cooldown_seconds - elapsed_loss)
                return False, f"{symbol} için zarar kes sonrası ceza beklemesi aktif ({remaining}s kaldı)"

        # Sembol bazlı standart cooldown
        if symbol and symbol in self.symbol_exit_times:
            elapsed_sym = now - self.symbol_exit_times[symbol]
            if elapsed_sym < self.symbol_cooldown_seconds:
                remaining = int(self.symbol_cooldown_seconds - elapsed_sym)
                return False, f"{symbol} için bekleme süresi aktif ({remaining}s kaldı)"

        # Genel cooldown
        if not is_basket_filling:
            elapsed = now - self.last_trade_time
            if elapsed < self.cooldown_seconds:
                return False, f"Genel bekleme süresi ({int(self.cooldown_seconds - elapsed)}s kaldı)"

        return True, "Uygun"

    def record_trade_entry(self, symbol: Optional[str] = None) -> None:
        self.last_trade_time = self._clock()

    def record_trade_exit(self, symbol: str, is_loss: bool = False) -> None:
        """Bir pozisyon kapandığında zaman damgasını kaydeder."""
        if symbol:
            now = self._clock()
            self.symbol_exit_times[symbol] = now
            if is_loss:
                self.symbol_loss_exit_times[symbol] = now

    def evaluate_exit(self, position: Dict[str, Any], current_price: float) -> Tuple[bool, str, float]:
        """
        Açık pozisyonun kapatılması gerekip gerekmediğini profesyonel kurallarla değerlendirir.
        Döner: (kapatılmalı_mı, neden, anlık_kâr_yüzdesi)
        """
        entry_price = float(position.get("risk_reference_price", position.get("entry_price", 0.0)))
        if current_price <= 0 or entry_price <= 0:
            return False, "Geçersiz fiyat verisi (0 veya negatif), çıkış değerlendirilmedi", 0.0

        highest_price = float(position.get("risk_highest_price", position.get("highest_price", entry_price)))
        if highest_price <= 0:
            highest_price = entry_price

        if current_price > highest_price:
            highest_price = current_price
            if "risk_reference_price" in position:
                position["risk_highest_price"] = highest_price
            else:
                position["highest_price"] = highest_price

        # Anlık brüt kâr/zarar yüzdesi
        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        peak_gain_pct = ((highest_price - entry_price) / entry_price) * 100.0

        eff_tp = float(position.get("target_tp_pct", self.take_profit_pct))
        eff_sl = float(position.get("target_sl_pct", self.stop_loss_pct))
        eff_trailing_act = float(position.get("trailing_activation_pct", self.trailing_activation_pct))
        eff_trailing_stop = float(position.get("trailing_stop_pct", self.trailing_stop_pct))
        min_clean_exit = self.fee_rate_pct * 2.2  # Borsa komisyonunu + ufak kârı kurtaran taban

        # Breakeven Lock Kontrolü
        if peak_gain_pct >= self.breakeven_trigger_pct:
            position["breakeven_locked"] = True

        # 1. Zarar Kes (Stop-Loss)
        if position.get("breakeven_locked", False):
            # Breakeven kilitliyse stop noktası maliyet + komisyon tabanıdır
            if pnl_pct <= min_clean_exit:
                return True, f"🛡️ BREAKEVEN KORUMASI: Maliyet ve komisyon korundu (Net Kâr: +%{pnl_pct:.2f})", pnl_pct
        else:
            if pnl_pct <= -eff_sl + 1e-5:
                return True, f"🛑 STOP-LOSS tetiklendi ({pnl_pct:.2f}% <= -{eff_sl:.2f}%)", pnl_pct

        # 2. Tam Kâr Al (Full Take-Profit)
        if pnl_pct >= eff_tp - 1e-5:
            return True, f"🎯 TAKE-PROFIT tetiklendi (+{pnl_pct:.2f}% >= +{eff_tp:.2f}%)", pnl_pct

        # 3. Kademeli Kâr Al (Partial Take-Profit / TP1)
        if self.enable_partial_tp and not position.get("partial_tp_taken", False):
            if pnl_pct >= self.partial_tp_pct:
                return True, f"🎯 KADEMELİ KÂR AL (TP1: +{pnl_pct:.2f}% >= +{self.partial_tp_pct:.2f}% | %{int(self.partial_tp_ratio*100)} Realize Edildi)", pnl_pct

        # 4. Akıllı Dinamik Trailing Stop (Kalan Pozisyonu Zirveye Sürme)
        if peak_gain_pct >= eff_trailing_act:
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0
            if drop_from_peak_pct >= eff_trailing_stop:
                if pnl_pct >= min_clean_exit:
                    return True, f"🎯 TRAILING STOP tetiklendi (Zirve: %{peak_gain_pct:.2f}, Düşüş: %{drop_from_peak_pct:.2f}, Net Kâr: +%{pnl_pct:.2f})", pnl_pct
                else:
                    return True, f"🛡️ BREAKEVEN / İZ SÜREN STOP tetiklendi (+%{pnl_pct:.2f})", pnl_pct


        # 5. Erken Momentum İptali (Early Invalidation Cut - Sahte Kırılım Koruması)
        entry_time = position.get("entry_time", 0.0)
        holding_sec = self._clock() - entry_time if entry_time > 0 else 0
        early_cut_sl = max(0.65, eff_sl * 0.70)
        if holding_sec >= 45.0 and peak_gain_pct < 0.20 and pnl_pct <= -early_cut_sl:
            return True, f"🛑 ERKEN MOMENTUM KESİMİ: Sahte kırılım sınırlandı ({pnl_pct:.2f}% <= -{early_cut_sl:.2f}%), tam stop-loss'tan kaçınıldı", pnl_pct

        # 6. Hareketsizlik / Durgunluk Tahliyesi (Stagnation Exit)
        if entry_time > 0 and (holding_sec >= self.max_holding_seconds):
            if -0.40 <= pnl_pct <= 0.40:
                return True, f"⏱️ DURGUNLUK TAHLİYESİ ({int(holding_sec/60)} dk hareketsizlik): Sermaye aktif liderlere aktarılıyor (K/Z: %{pnl_pct:+.2f})", pnl_pct

        return False, "Pozisyon devam ediyor", pnl_pct
