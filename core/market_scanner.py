import time
import requests
import logging
import math
from typing import List, Dict, Any, Optional, Set, Tuple
from collections import deque
from core.binance_client import BinanceTrClient

logger = logging.getLogger("MarketScanner")

# Hariç tutulacak sabit koinler ve itibari para birimleri
STABLE_ASSETS = {
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "EUR", "EURI", "AEUR", "TRY", "PAX", "DAI"
}

class MicroMomentumTracker:
    """
    3 Katmanlı Kantitatif Kırılım ve Mikro-Momentum Motoru (3-Layer Breakout Engine).
    
    Analiz Katmanları:
    1. Hacim Patlaması Dalgası (Volume Surge Index / RVOL): Anlık hacim akışının hızlanması.
    2. Volatilite Sıkışması ve Patlaması (Volatility Squeeze -> Breakout): Sıkışan fiyatın yukarı patlaması.
    3. Mikro-İvme ve Geri Çekilmeme Gücü (Micro-Velocity & Pullback Resistance).
    """
    def __init__(
        self,
        window_seconds: int = 60,
        cooldown_seconds: int = 20,
        min_momentum_pct: float = 0.25,
        clock=None,
    ):
        self._clock = clock or (lambda: time.time())
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.min_momentum_pct = min_momentum_pct
        self.min_observation_seconds = 0
        self.min_burst_count = 1
        
        # symbol -> deque of (timestamp, price, volume_try)
        self.history: Dict[str, deque] = {}
        # symbol -> first seen timestamp
        self.first_seen: Dict[str, float] = {}
        # symbol -> cooldown end timestamp
        self.cooldowns: Dict[str, float] = {}
        # symbol -> 10s prebuy tracking state
        self.prebuy_tracking: Dict[str, Dict[str, Any]] = {}

    @property
    def min_gain_pct(self) -> float:
        return self.min_momentum_pct

    @min_gain_pct.setter
    def min_gain_pct(self, value: float) -> None:
        self.min_momentum_pct = value

    @property
    def timeout_cooldown_seconds(self) -> float:
        return self.cooldown_seconds

    @timeout_cooldown_seconds.setter
    def timeout_cooldown_seconds(self, value: float) -> None:
        self.cooldown_seconds = value

    def is_cooling_down(self, symbol: str) -> bool:
        """Coinin soğuma/dinlenme süresinde olup olmadığını kontrol eder."""
        now = self._clock()
        if symbol in self.cooldowns:
            if now < self.cooldowns[symbol]:
                return True
            else:
                del self.cooldowns[symbol]
        return False

    def record_tick(self, symbol: str, price: float, volume_try: float = 0.0, timestamp: Optional[float] = None) -> None:
        """Yeni bir fiyat ve hacim tick'ini kaydeder ve 60 saniyeden eski verileri temizler."""
        now = self._clock() if timestamp is None else timestamp
        if not all(math.isfinite(v) for v in (price, volume_try, now)) or price <= 0 or volume_try < 0:
            return
        if symbol in self.history and self.history[symbol] and now <= self.history[symbol][-1][0]:
            return
        if symbol not in self.first_seen:
            self.first_seen[symbol] = now
            self.history[symbol] = deque(maxlen=300)

        dq = self.history[symbol]
        dq.append((now, price, volume_try))

        # Son window_seconds (60s) dışındakileri temizle
        cutoff = now - (self.window_seconds * 1.5)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def get_micro_metrics(self, symbol: str) -> Dict[str, Any]:
        """
        Son 60 saniyelik mikro-hareketten 3 katmanlı kırılım metriklerini hesaplar.
        """
        now = self._clock()
        if symbol in self.cooldowns and now < self.cooldowns[symbol]:
            rem = int(self.cooldowns[symbol] - now)
            return {
                "status": "cooldown",
                "velocity_1m_pct": 0.0,
                "volume_surge_ratio": 1.0,
                "is_squeeze_breakout": False,
                "burst_ratio": 0.0,
                "elapsed": 0,
                "cooldown_remaining": rem,
                "is_qualified": False,
                "is_dumping": False,
            }

        if symbol not in self.history or len(self.history[symbol]) < 2:
            elapsed = int(now - self.first_seen.get(symbol, now))
            return {
                "status": "observing",
                "velocity_1m_pct": 0.0,
                "volume_surge_ratio": 1.0,
                "is_squeeze_breakout": False,
                "burst_ratio": 0.0,
                "elapsed": elapsed,
                "cooldown_remaining": 0,
                "is_qualified": False,
                "is_dumping": False,
            }

        dq = self.history[symbol]
        cutoff_60 = now - self.window_seconds
        recent_items = [item for item in dq if cutoff_60 <= item[0] <= now]
        if len(recent_items) < 2:
            return {"status": "stale", "velocity_1m_pct": 0.0, "volume_surge_ratio": 1.0,
                    "is_squeeze_breakout": False, "burst_ratio": 0.0, "elapsed": int(now - self.first_seen.get(symbol, now)),
                    "cooldown_remaining": 0, "is_qualified": False, "is_dumping": False}

        prices = [item[1] for item in recent_items]
        vols = [item[2] for item in recent_items if len(item) > 2]

        first_p = prices[0]
        current_p = prices[-1]
        highest_p = max(prices)
        lowest_p = min(prices)
        elapsed = int(now - self.first_seen.get(symbol, now))

        # 1. Mikro-Fiyat Hızı (Velocity)
        velocity_pct = ((current_p - first_p) / first_p) * 100.0 if first_p > 0 else 0.0
        
        # Tepe noktadan çekilme (Pullback / Dump kontrolü)
        drop_from_high = ((highest_p - current_p) / highest_p) * 100.0 if highest_p > 0 else 0.0
        
        # Ardışık yukarı tick oranı
        up_ticks = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i-1])
        down_ticks = sum(1 for i in range(1, len(prices)) if prices[i] < prices[i-1])
        total_transitions = max(1, up_ticks + down_ticks)
        burst_ratio = up_ticks / total_transitions

        # Legacy proxy: change in a rolling 24h total, NOT measured 1m volume or true RVOL.
        vol_surge_ratio = 1.0
        if len(vols) >= 4 and vols[-1] > vols[0] > 0:
            vol_surge_ratio = min(5.0, max(1.0, (vols[-1] - vols[0]) / max(1.0, vols[0]) * 100.0))

        # 3. Volatilite Sıkışması ve Kırılımı (Volatility Squeeze -> Breakout)
        base_range_pct = ((max(prices[:-1]) - min(prices[:-1])) / first_p) * 100.0 if len(prices) > 3 and first_p > 0 else 1.0
        is_squeeze_breakout = (base_range_pct < 0.25 and velocity_pct >= 0.25 and current_p >= highest_p)

        # 4. Durgunluk ve Seyrek İşlem Tespiti (Stagnant / Low Tick Frequency Filter)
        unique_prices = set(prices)
        is_stagnant = (
            (len(prices) >= 3 and len(unique_prices) <= 1)
            or (elapsed >= 25 and len(unique_prices) < 2)
        )

        # Düşen Bıçak Tespiti: Belirgin düşüş serisi ve en az -%0.30 kayıp
        is_dumping = (velocity_pct <= -0.30 and down_ticks > up_ticks * 2) or (drop_from_high >= 0.80 and velocity_pct < 0)

        # Kalifikasyon: Yeterli pozitif ivme, tutarlılık, dump olmaması ve DURGUN OLMAMASI
        is_qualified = (
            not is_stagnant
            and recent_items[-1][0] - recent_items[0][0] >= self.min_observation_seconds
            and up_ticks >= self.min_burst_count
            and not is_dumping
            and (
                (velocity_pct >= self.min_momentum_pct and burst_ratio >= 0.40)
                or is_squeeze_breakout
            )
        )

        return {
            "status": "active",
            "velocity_1m_pct": round(velocity_pct, 2),
            "burst_ratio": round(burst_ratio, 2),
            "volume_surge_ratio": round(vol_surge_ratio, 2),
            "volume_surge_is_proxy": True,
            "true_rvol_available": False,
            "sample_span_seconds": recent_items[-1][0] - recent_items[0][0],
            "is_squeeze_breakout": is_squeeze_breakout,
            "is_stagnant": is_stagnant,
            "unique_ticks": len(unique_prices),
            "up_ticks": up_ticks,
            "down_ticks": down_ticks,
            "drop_from_high": round(drop_from_high, 2),
            "elapsed": elapsed,
            "cooldown_remaining": 0,
            "is_qualified": is_qualified,
            "is_dumping": is_dumping,
        }

    def observe(self, symbol: str, current_price: float, rsi: float = 50.0) -> Tuple[bool, str, int]:
        """
        Geriye dönük uyumluluk ve anlık karar motoru.
        Döner: (is_approved: bool, reason: str, elapsed_seconds: int)
        """
        self.record_tick(symbol, current_price)
        metrics = self.get_micro_metrics(symbol)
        now = self._clock()
        elapsed = metrics.get("elapsed", 0)

        if metrics.get("status") == "cooldown":
            return False, f"⏳ Dinlenmede ({metrics.get('cooldown_remaining', 0)}sn kaldı)", 0

        # Düşen bıçak / Dump koruması
        if metrics.get("is_dumping"):
            self.cooldowns[symbol] = now + self.cooldown_seconds
            return False, f"⚠️ Düşüş eğilimi / Dump riski saptandı (1m Hız: %{metrics.get('velocity_1m_pct', 0.0):+.2f})", elapsed

        # Kırılım veya İvme onayı
        if metrics.get("is_squeeze_breakout"):
            return True, f"💥 SIKIŞMA VE HACİMLİ KIRILIM (1m Hız: %{metrics.get('velocity_1m_pct', 0.0):+.2f})", elapsed

        if metrics.get("is_qualified"):
            return True, f"🚀 1-DK MİKRO-MOMENTUM TEYİDİ (Hız: %{metrics.get('velocity_1m_pct', 0.0):+.2f}, Tutarlılık: %{int(metrics.get('burst_ratio', 0)*100)})", elapsed

        # İzleme devam ediyor
        if elapsed < self.window_seconds:
            return False, f"1-Dk Kalibrasyon İzlemesi ({elapsed}/{self.window_seconds}s | 1m Hız: %{metrics.get('velocity_1m_pct', 0.0):+.2f})", elapsed

        # Zaman aşımı (60sn doldu ve ivme yok) -> Soğumaya al
        self.cooldowns[symbol] = now + self.cooldown_seconds
        return False, f"🔄 Zaman aşımı ({elapsed}s): 1-Dk mikro ivme yetersiz, taze adaylara geçiliyor", elapsed

    def observe_prebuy(
        self,
        symbol: str,
        current_price: float,
        observation_seconds: int = 10,
        max_allowed_drop_pct: float = 0.30
    ) -> Tuple[bool, str, int]:
        """
        Coini almadan önce 10 saniye boyunca mikro fiyat hareketini izler.
        Bu sürede sert düşüş (%0.30+) veya dump eğilimi varsa alımı derhal iptal eder.
        Döner: (is_approved: bool, reason: str, elapsed_seconds: int)
        """
        if observation_seconds <= 0:
            return True, "Doğrudan Alım Onaylandı", 0

        now = self._clock()
        # Cooldown kontrolü
        if symbol in self.cooldowns:
            if now < self.cooldowns[symbol]:
                rem = int(self.cooldowns[symbol] - now)
                return False, f"⏳ Dinlenmede ({rem}sn kaldı)", 0
            else:
                del self.cooldowns[symbol]

        if not hasattr(self, "prebuy_tracking"):
            self.prebuy_tracking: Dict[str, Dict[str, Any]] = {}

        if symbol not in self.prebuy_tracking:
            self.prebuy_tracking[symbol] = {
                "start_time": now,
                "start_price": current_price,
                "highest_price": current_price,
                "ticks": [(now, current_price)],
            }
            return False, f"👀 10s Ön-Alım İzlemesi Başlatıldı (0/{observation_seconds}s | Fiyat: {current_price:.4f})", 0

        info = self.prebuy_tracking[symbol]
        info["ticks"].append((now, current_price))
        if current_price > info["highest_price"]:
            info["highest_price"] = current_price

        elapsed = int(now - info["start_time"])
        start_p = info["start_price"]
        high_p = info["highest_price"]

        # Başlangıç ve tepe fiyata göre düşüş
        drop_from_start = ((start_p - current_price) / start_p) * 100.0 if start_p > 0 else 0.0
        drop_from_high = ((high_p - current_price) / high_p) * 100.0 if high_p > 0 else 0.0

        # Düşüş Tespiti: Başlangıca göre > %0.25 veya tepeden > %0.35 düşüş varsa ALIMI İPTAL ET
        if drop_from_start >= max_allowed_drop_pct or drop_from_high >= (max_allowed_drop_pct * 1.4):
            del self.prebuy_tracking[symbol]
            self.cooldowns[symbol] = now + self.cooldown_seconds
            return False, f"⚠️ Ön-alımda düşüş saptandı (Düşüş: %{-drop_from_start:.2f}) -> Alım iptal edildi, tuzaktan kaçınıldı", elapsed

        # Belirlenen süre tamamlandı ve düşüş yok -> Teyit ve Giriş
        if elapsed >= observation_seconds:
            del self.prebuy_tracking[symbol]
            gain = ((current_price - start_p) / start_p) * 100.0 if start_p > 0 else 0.0
            ticks_prices = [p for _, p in info["ticks"]]
            unique_ticks_count = len(set(ticks_prices))

            # Gerçek Durgunluk Kontrolü: 10s boyunca tek fiyat ve genel geçmişte de hiç hareket yoksa dinlendir
            history_unique = len(set(p for _, p, *_ in self.history.get(symbol, []))) if symbol in self.history else unique_ticks_count
            if unique_ticks_count <= 1 and history_unique <= 1:
                self.cooldowns[symbol] = now + 45.0
                return False, f"⚠️ Ön-Gözlemde Durgunluk Saptandı (Fiyat Hareketsiz) -> Alım iptal edildi, {symbol} 45s dinlenmeye alındı", elapsed

            # Negatif İvme Kontrolü: Ön gözlemde net düşüş olduysa alma
            if gain < -0.10:
                self.cooldowns[symbol] = now + self.cooldown_seconds * 2
                return False, f"⚠️ Ön-Gözlemde İvme Negatife Döndü (Değişim: %{gain:+.2f}) -> Alım iptal edildi", elapsed

            return True, f"✅ Ön-Alım Teyidi Tamamlandı (İvme: %{gain:+.2f}) -> Güvenli Giriş", elapsed

        # Süre dolana kadar izleme devam ediyor
        return False, f"⏳ Ön-Alım İzleniyor ({elapsed}/{observation_seconds}s | Fiyat: {current_price:.4f} | Fark: %{-drop_from_start:+.2f})", elapsed

    def reset_round(self, now: Optional[float] = None) -> None:
        """
        Kesintisiz kayan pencere (smooth rolling window) akışı için
        60 saniyeden eski tickleri temizler, taze momentumu bölmez.
        """
        t = self._clock() if now is None else now
        cutoff = t - self.window_seconds
        for sym, dq in list(self.history.items()):
            if dq:
                cleaned = [item for item in dq if item[0] >= cutoff]
                if cleaned:
                    self.history[sym] = deque(cleaned, maxlen=300)
                else:
                    self.history[sym] = deque([dq[-1]], maxlen=300)


    def get_info(self, symbol: str) -> Dict[str, Any]:
        """Arayüz ve durum logları için formatlanmış bilgi döner."""
        m = self.get_micro_metrics(symbol)
        return {
            "status": "cooling_down" if m.get("status") == "cooldown" else "tracking",
            "elapsed": m.get("elapsed", 0),
            "min_sec": self.window_seconds,
            "change_pct": m.get("velocity_1m_pct", 0.0),
            "target_gain_pct": self.min_momentum_pct,
            "burst_count": m.get("up_ticks", 0),
            "min_burst_count": self.min_burst_count,
            "is_ready": m.get("is_qualified", False),
            "is_squeeze_breakout": m.get("is_squeeze_breakout", False),
            "volume_surge_ratio": m.get("volume_surge_ratio", 1.0),
            "cooldown_remaining": m.get("cooldown_remaining", 0),
        }


# Geriye dönük test uyumluluğu için alias
CandidateWatchlist = MicroMomentumTracker


class MarketScanner:
    """
    Binance TR 3-Katmanlı Quant Kırılım ve Momentum Radarı.
    
    Özellikler:
    1. 1-Dakikalık Hacim ve Fiyat Mikro-İvmesi (Micro Velocity & RVOL)
    2. Volatilite Sıkışması ve Patlaması (Squeeze Breakout Detection)
    3. 60-Saniyelik Başlangıç Kalibrasyon Modu (Initial 1m Warmup)
    4. Anti-Dump & Düşen Bıçak Koruması
    """
    def __init__(
        self,
        client: Optional[BinanceTrClient] = None,
        quote_asset: str = "TRY",
        min_volume_try: float = 2500000.0,
        min_coin_price: float = 0.05,
        max_allowed_spread_pct: float = 0.20,
        btc_dump_shield_pct: float = 0.35,
        btc_dump_cooldown_seconds: int = 120,
        calibration_window_seconds: int = 60,
        clock=None,
    ):
        self._clock = clock or (lambda: time.time())
        self.feature_only = False
        self.client = client or BinanceTrClient()
        self.quote_asset = quote_asset.upper()
        self.min_volume_try = min_volume_try
        self.min_coin_price = min_coin_price
        self.max_allowed_spread_pct = max_allowed_spread_pct
        self.btc_dump_shield_pct = btc_dump_shield_pct
        self.btc_dump_cooldown_seconds = btc_dump_cooldown_seconds
        self.btc_dump_until: float = 0.0
        self.calibration_window_seconds = calibration_window_seconds
        
        self.cached_top_pairs: List[Dict[str, Any]] = []
        self.tr_listed_symbols: Set[str] = set()
        self.last_scan_time: float = 0.0
        self.scan_cache_ttl_seconds: int = 4
        self.max_source_age_seconds: float = 10.0
        
        # 1-Dakikalık 3-Katmanlı Kırılım Takipçisi
        self.tracker = MicroMomentumTracker(
            window_seconds=calibration_window_seconds,
            cooldown_seconds=20,
            min_momentum_pct=0.25,
            clock=self._clock,
        )
        self.watchlist = self.tracker
        self._load_tr_symbols()

    def is_btc_dumping(self) -> Tuple[bool, str]:
        """
        BTC/TRY'nin son 1 dakikalık mikro-momentumunu denetler.
        Eğer BTC sert satış dalgasına (dump) girmişse tüm altcoin alımlarını dondurur.
        """
        now = self._clock()
        if now < self.btc_dump_until:
            rem = int(self.btc_dump_until - now)
            return True, f"🛡️ BTC Piyasa Çöküş Kalkanı Aktif ({rem}sn donduruldu)"

        btc_metrics = self.tracker.get_micro_metrics(f"BTC_{self.quote_asset}")
        velo_1m = btc_metrics.get("velocity_1m_pct", 0.0)
        drop_from_high = btc_metrics.get("drop_from_high", 0.0)

        if velo_1m <= -self.btc_dump_shield_pct or drop_from_high >= (self.btc_dump_shield_pct * 1.5):
            self.btc_dump_until = now + self.btc_dump_cooldown_seconds
            return True, f"🛑 BTC Ani Satış Dalgası Saptandı (1m Hız: %{velo_1m:+.2f}) -> Altcoin alımları donduruldu"

        return False, ""

    def reset_round(self) -> None:
        """1 dakikalık tarama turunu sıfırlar ve taze referansla başlatır."""
        self.tracker.reset_round()
        self.cached_top_pairs = []
        self.last_scan_time = 0.0

    def _load_tr_symbols(self) -> None:
        try:
            symbols = self.client.get_symbols()
            if symbols:
                self.tr_listed_symbols = {
                    s.get("symbol") for s in symbols
                    if s.get("spotTradingEnable", 1) == 1
                }
        except Exception as e:
            logger.warning(f"Binance TR sembol listesi yüklenemedi: {e}")

    def calculate_quant_score(self, pair: Dict[str, Any], micro: Optional[Dict[str, Any]] = None) -> float:
        """
        3-Katmanlı Quant Skorlama Motoru:
        
        - 1-Dakikalık Mikro-Hız (%35): Anlık 60s fiyat artış ivmesi
        - 24-Saatlik Makro-Trend (%30): Genel piyasa yönü
        - Hacim & Likidite Gücü (%20): Logaritmik TRY hacmi ve anlık hacim dalgası (RVOL)
        - Volatilite / Kırılım Gücü (%15): Günlük volatilite + Sıkışma Kırılımı Bonusu
        """
        micro_data = micro or {}
        chg_24h = pair.get("change_pct", 0.0)
        vol_try = pair.get("volume_try", 0.0)
        high = pair.get("high", 0.0)
        low = pair.get("low", 1.0)
        
        velo_1m = micro_data.get("velocity_1m_pct", 0.0)
        burst_ratio = micro_data.get("burst_ratio", 0.5)
        is_squeeze = micro_data.get("is_squeeze_breakout", False)
        vol_surge = micro_data.get("volume_surge_ratio", 1.0)

        # 1. Mikro-Hız Skoru
        micro_score = max(0.0, min(10.0, velo_1m * 5.0)) if velo_1m > 0 else (velo_1m * 2.0)

        # 2. Makro Trend Skoru (Pozitif 24h değişim)
        trend_score = max(0.0, min(10.0, chg_24h * 2.0)) if chg_24h > 0 else (chg_24h * 0.5)

        # 3. Hacim Skoru (Logaritmik 100K - 1B TRY) + RVOL Patlama Bonusu
        vol_score = max(0.0, min(10.0, math.log10(max(vol_try, 1.0)) * 1.3))
        if vol_surge >= 1.5:
            vol_score = min(10.0, vol_score + 2.0)  # Legacy rolling-24h-volume proxy bonus (not true RVOL)

        # 4. Volatilite & Sıkışma Kırılımı Skoru
        volatility_pct = ((high - low) / low) * 100.0 if (low and low > 0) else 0.0
        volat_score = min(10.0, volatility_pct * 2.0) if volatility_pct > 0 else (burst_ratio * 10.0)
        if is_squeeze:
            volat_score = min(10.0, volat_score + 2.0)  # Sıkışma kırılımı bonusu

        total_score = (
            (micro_score * 0.35) +
            (trend_score * 0.30) +
            (vol_score * 0.20) +
            (volat_score * 0.15)
        )
        return round(total_score, 2)

    def scan_top_active_pairs(
        self,
        limit: int = 8,
        force_refresh: bool = False,
        only_uptrend: bool = True,
        min_gain_pct: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Binance TR'de işlem gören tüm TRY çiftlerini tarar, 3-katmanlı mikro-kırılım
        metrikleriyle birleştirir ve quant skoruna göre sıralar.
        """
        now = self._clock()
        if not force_refresh and (0 <= now - self.last_scan_time < self.scan_cache_ttl_seconds):
            pairs = self.cached_top_pairs
            if only_uptrend and not self.feature_only:
                filtered = [p for p in pairs if p["change_pct"] >= min_gain_pct and not p.get("is_dumping", False)]
                return filtered[:limit] if (limit and limit > 0) else filtered

            return pairs[:limit] if (limit and limit > 0) else pairs


        if not self.tr_listed_symbols:
            self._load_tr_symbols()
        if not self.tr_listed_symbols:
            return []

        endpoints = [
            "https://api.binance.com/api/v3/ticker/24hr",
            "https://api.binance.me/api/v1/ticker/24hr",
        ]

        raw_data = None
        for ep in endpoints:
            try:
                resp = requests.get(ep, timeout=5)
                if resp.status_code == 200:
                    candidate_data = resp.json()
                    if isinstance(candidate_data, list):
                        raw_data = candidate_data
                        break
            except Exception:
                continue

        if not isinstance(raw_data, list):
            # Old leaders may remain visible, but cannot be returned as fresh candidates.
            return []

        now = self._clock()  # Receipt time; source timestamps are checked separately below.
        valid_pairs = []
        suffix = self.quote_asset

        for item in raw_data:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol", "")
            if not isinstance(sym, str):
                continue
            if not sym.endswith(suffix):
                continue

            base = sym[:-len(suffix)]
            if base in STABLE_ASSETS and not self.feature_only:
                continue

            tr_symbol = f"{base}_{suffix}"
            if self.tr_listed_symbols and tr_symbol not in self.tr_listed_symbols:
                continue

            try:
                if item.get("closeTime") is not None:
                    source_age = now - float(item["closeTime"]) / 1000.0
                    if not math.isfinite(source_age) or not 0 <= source_age <= self.max_source_age_seconds:
                        continue
                vol = float(item.get("quoteVolume", 0.0))
                chg_pct = float(item.get("priceChangePercent", 0.0))
                last_price = float(item.get("lastPrice", 0.0))
                high_price = float(item.get("highPrice", 0.0))
                low_price = float(item.get("lowPrice", 0.0))
                if not all(math.isfinite(v) for v in (vol, chg_pct, last_price, high_price, low_price)):
                    continue
                if vol < 0 or last_price <= 0 or (not self.feature_only and (vol < self.min_volume_try or last_price < self.min_coin_price)):
                    continue
            except (TypeError, ValueError, OverflowError):
                continue

            # 3-Katmanlı mikro tick ve hacim kaydı
            self.tracker.record_tick(tr_symbol, last_price, volume_try=vol, timestamp=now)
            micro_metrics = self.tracker.get_micro_metrics(tr_symbol)

            pair_dict = {
                "symbol": tr_symbol,
                "clean_symbol": sym,
                "base": base,
                "quote": suffix,
                "price": last_price,
                "change_pct": round(chg_pct, 2),
                "abs_change": abs(chg_pct),
                "volume_try": vol,
                "high": high_price,
                "low": low_price,
                "updated_at": now,
                "source": ep,
                "source_close_time_ms": item.get("closeTime"),
                "volume_surge_is_proxy": True,
                "true_rvol_available": False,
                "velocity_1m_pct": micro_metrics.get("velocity_1m_pct", 0.0),
                "burst_ratio": micro_metrics.get("burst_ratio", 0.0),
                "volume_surge_ratio": micro_metrics.get("volume_surge_ratio", 1.0),
                "is_squeeze_breakout": micro_metrics.get("is_squeeze_breakout", False),
                "is_stagnant": micro_metrics.get("is_stagnant", False),
                "unique_ticks": micro_metrics.get("unique_ticks", 1),
                "is_dumping": micro_metrics.get("is_dumping", False),
                "is_qualified_1m": micro_metrics.get("is_qualified", False),
                "observation": self.tracker.get_info(tr_symbol),
                "micro_metrics": dict(micro_metrics),
            }
            pair_dict["quant_score"] = self.calculate_quant_score(pair_dict, micro_metrics)
            valid_pairs.append(pair_dict)

        # Dinlenmede / Dump / Durgun olanları arkaya al, quant skoruna göre sırala
        valid_pairs.sort(key=lambda x: (
            1 if (self.tracker.is_cooling_down(x["symbol"]) or x.get("is_dumping", False) or x.get("is_stagnant", False)) else 0,
            -x.get("quant_score", 0.0)
        ))

        self.cached_top_pairs = valid_pairs
        self.last_scan_time = now

        pairs = self.cached_top_pairs or []
        if only_uptrend and pairs and not self.feature_only:
            filtered = [p for p in pairs if p["change_pct"] >= min_gain_pct and not p.get("is_dumping", False)]
            return filtered[:limit] if (limit and limit > 0) else filtered

        return pairs[:limit] if (limit and limit > 0) else pairs


