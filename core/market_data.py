import time
import math
from typing import List, Dict, Any, Optional, Callable
from core.binance_client import BinanceTrClient

class MarketDataEngine:
    """
    Binance TR için anlık piyasa verisi, mum geçmişi ve teknik gösterge (RSI, Bollinger, EMA) hesaplayıcı motor.
    """
    def __init__(self, client: BinanceTrClient, symbol: str = "USDT_TRY", *, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or (lambda: time.time())
        self.client = client
        self.symbol = symbol
        self.price_history: List[float] = []
        self.candle_closes: List[float] = []
        self.closed_candle_rows: List[Dict[str, Any]] = []
        self.candle_highs: List[float] = []
        self.candle_lows: List[float] = []
        self.last_price: float = 0.0
        self.last_bid: float = 0.0
        self.last_ask: float = 0.0
        self.last_spread_pct: float = 0.0
        self.last_update_time: float = 0.0
        self.last_klines_update_time: float = 0.0
        self.cache_ttl_seconds: float = 0.8
        self.klines_cache_ttl_seconds: float = 12.0
        self._cached_snapshot: Optional[Dict[str, Any]] = None
        self.candle_close_times: List[float] = []
        self.rsi_period = 14
        self.bollinger_period = 20
        self.bollinger_std_dev = 2.0
        self.ema_fast_period = 9
        self.ema_slow_period = 21
        self.max_quote_age_seconds = 5.0
        self.max_candle_age_seconds = 90.0
        self._book_error = "missing_order_book"
        self._candle_error = "missing_candles"
        self.candle_source = "unknown"
        self.quote_source = self.client.base_url

    def configure(self, strategy: Any, trading: Any) -> None:
        """Use configured indicator periods rather than hidden hard-coded defaults."""
        periods = (strategy.rsi_period, strategy.bollinger_period, strategy.ema_fast, strategy.ema_slow)
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 2 or v > 499 for v in periods):
            raise ValueError("Gösterge periyotları 2..499 aralığında tam sayı olmalı")
        if not math.isfinite(strategy.bollinger_std_dev) or strategy.bollinger_std_dev <= 0:
            raise ValueError("Bollinger standart sapma çarpanı pozitif ve sonlu olmalı")
        self.rsi_period, self.bollinger_period, self.ema_fast_period, self.ema_slow_period = periods
        self.bollinger_std_dev = strategy.bollinger_std_dev
        self.max_quote_age_seconds = trading.max_market_data_age_seconds
        self.max_candle_age_seconds = trading.max_candle_age_seconds
        for value in (self.max_quote_age_seconds, self.max_candle_age_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Veri yaşı sınırları pozitif ve sonlu olmalı")
        self._cached_snapshot = None

    @property
    def required_candles(self) -> int:
        # ADX(14) needs 28 candles for its first fully seeded value.
        return max(28, self.rsi_period + 1, self.bollinger_period, self.ema_fast_period, self.ema_slow_period)

    def _read_closed_candles(self, klines: List[Any], as_of: float) -> None:
        """Accept chronological, contiguous, timestamped 1m OHLC only.

        An open/future candle is not available at as_of and is never a feature.
        Unknown/malformed time or OHLC data invalidates this batch; no tick fallback.
        Binance REST timestamps are milliseconds since epoch.
        """
        parsed = []
        full_rows = []
        try:
            for k in klines:
                if isinstance(k, (list, tuple)) and len(k) >= 7:
                    ot, o, h, l, c, ct = float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[6])
                elif isinstance(k, dict):
                    ot = float(k.get("openTime", k.get("open_time")))
                    ct = float(k.get("closeTime", k.get("close_time")))
                    o, h, l, c = (float(k[key]) for key in ("open", "high", "low", "close"))
                else:
                    raise ValueError("missing timestamped OHLC")
                if not all(math.isfinite(v) for v in (ot, ct)) or ot < 0 or ct < ot or abs(ct - ot - 59999) > 1:
                    raise ValueError("invalid 1m candle time")
                if ct / 1000.0 >= as_of:
                    continue
                if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) or h < max(o, c) or l > min(o, c) or l > h:
                    raise ValueError("invalid OHLC")
                if parsed and abs(ot - parsed[-1][0] - 60000) > 1:
                    raise ValueError("non-contiguous, duplicate or unordered candles")
                parsed.append((ot, h, l, c, ct / 1000.0))
                volume = k[5] if isinstance(k, (list, tuple)) else k.get("volume")
                try:
                    volume = float(volume)
                    if not math.isfinite(volume) or volume < 0: volume = None
                except (ValueError, TypeError):
                    volume = None
                full_rows.append({"open_time": ot / 1000.0, "close_time": ct / 1000.0,
                                  "open": o, "high": h, "low": l, "close": c, "volume": volume})
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self._candle_error = f"invalid_candles: {exc}"
            return
        if not parsed:
            self._candle_error = "no_closed_candles"
            return
        self.closed_candle_rows = full_rows
        self.candle_highs = [row[1] for row in parsed]
        self.candle_lows = [row[2] for row in parsed]
        self.candle_closes = [row[3] for row in parsed]
        self.candle_close_times = [row[4] for row in parsed]
        self.last_klines_update_time = as_of
        self.candle_source = getattr(self.client, "kline_sources", {}).get(self.symbol, "client_unspecified")
        self._candle_error = ""

    def update_market_state(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """
        Binance TR'den anlık derinlik ve fiyatı çeker, geçmişi günceller.
        Önbellek mekanizması ile sunucuyu ve arayüzü kilitlemeyi/kasmayı engeller.
        """
        now = self._clock()
        if not force and 0 <= now - self.last_update_time < self.cache_ttl_seconds and self._cached_snapshot:
            return self.get_snapshot()

        prices = self.client.get_best_prices(self.symbol)
        try:
            bid, ask = float(prices["bid"]), float(prices["ask"])
            quote_at = float(prices.get("timestamp", self._clock()))
            valid = (all(math.isfinite(v) and v > 0 for v in (bid, ask)) and ask >= bid
                     and math.isfinite(quote_at) and 0 <= self._clock() - quote_at <= self.max_quote_age_seconds)
        except (KeyError, TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            self._book_error = "missing_or_invalid_order_book"
            return self.get_snapshot() if self._cached_snapshot else None

        self.last_bid, self.last_ask = bid, ask
        self.last_price = bid  # Mark/liquidation side; a BUY executes at ask.
        self.last_spread_pct = (ask - bid) / bid * 100.0
        self.last_update_time = quote_at
        self.quote_source = prices.get("source", self.client.base_url)
        self._book_error = ""
        self.price_history.append(self.last_price)
        self.price_history = self.price_history[-500:]

        if force or now - self.last_klines_update_time >= self.klines_cache_ttl_seconds or len(self.candle_closes) < self.required_candles:
            klines = self.client.get_klines(self.symbol, interval="1m", limit=max(50, self.required_candles + 1))
            if klines:
                self._read_closed_candles(klines, self._clock())
            else:
                self._candle_error = "candle_refresh_failed"
        self._cached_snapshot = self.get_snapshot()
        return dict(self._cached_snapshot)

    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """
        Göreceli Güç Endeksi (RSI) hesaplar.
        """
        if len(prices) < period + 1:
            return 50.0  # Yeterli veri yoksa nötr

        changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        avg_gain = sum(max(d, 0.0) for d in changes[:period]) / period
        avg_loss = sum(max(-d, 0.0) for d in changes[:period]) / period
        for delta in changes[period:]:
            avg_gain = ((period - 1) * avg_gain + max(delta, 0.0)) / period
            avg_loss = ((period - 1) * avg_loss + max(-delta, 0.0)) / period
        # Preserve the project's neutral convention for an entirely flat series.
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2)

    def calculate_bollinger_bands(self, prices: List[float], period: int = 20, num_std: float = 2.0) -> Dict[str, float]:
        """
        Bollinger Bantlarını hesaplar (Alt bant, Orta bant/SMA, Üst bant).
        """
        if len(prices) < period:
            current = prices[-1] if prices else self.last_price
            return {"upper": current * 1.01, "middle": current, "lower": current * 0.99}

        recent = prices[-period:]
        middle = sum(recent) / period
        variance = sum((x - middle) ** 2 for x in recent) / period
        std_dev = math.sqrt(variance)

        upper = middle + (std_dev * num_std)
        lower = middle - (std_dev * num_std)

        return {
            "upper": round(upper, 6),
            "middle": round(middle, 6),
            "lower": round(lower, 6),
        }

    def calculate_ema(self, prices: List[float], period: int) -> float:
        """
        Üssel Hareketli Ortalama (EMA) hesaplar.
        """
        if not prices:
            return self.last_price
        if len(prices) < period:
            return sum(prices) / len(prices)

        multiplier = 2.0 / (period + 1.0)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return round(ema, 6)

    def calculate_atr(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """
        Ortalama Gerçek Aralık (Average True Range - ATR) hesaplar.
        """
        n = min(len(highs), len(lows), len(closes))
        if n < period + 1:
            return 0.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, n)]
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = ((period - 1) * atr + tr) / period
        return round(atr, 6)

    def calculate_adx(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Dict[str, float]:
        """
        Average Directional Index (ADX) hesaplar.
        ADX >= 22 -> Güçlü Trend
        ADX < 20 -> Yatay Kanal
        """
        n = min(len(highs), len(lows), len(closes))
        if n < period + 1:
            return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
        trs, pdms, mdms = [], [], []
        for i in range(1, n):
            up, down = highs[i] - highs[i-1], lows[i-1] - lows[i]
            pdms.append(up if up > down and up > 0 else 0.0)
            mdms.append(down if down > up and down > 0 else 0.0)
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        tr_s = sum(trs[:period-1])
        pdm_s, mdm_s = sum(pdms[:period-1]), sum(mdms[:period-1])
        dxs = []
        plus_di = minus_di = 0.0
        for i in range(period - 1, len(trs)):
            tr_s = tr_s - tr_s / period + trs[i]
            pdm_s = pdm_s - pdm_s / period + pdms[i]
            mdm_s = mdm_s - mdm_s / period + mdms[i]
            plus_di = 100.0 * pdm_s / tr_s if tr_s > 0 else 0.0
            minus_di = 100.0 * mdm_s / tr_s if tr_s > 0 else 0.0
            di_sum = plus_di + minus_di
            dxs.append(100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0)
        adx = sum(dxs[:period]) / period if len(dxs) >= period else 0.0
        for dx in dxs[period:]:
            adx = ((period - 1) * adx + dx) / period
        return {"adx": round(adx, 2), "plus_di": round(plus_di, 2), "minus_di": round(minus_di, 2)}

    def get_snapshot(self) -> Dict[str, Any]:
        """
        Strateji ve arayüz için anlık piyasa ve gösterge özetini döner.
        """
        series = self.candle_closes
        highs = self.candle_highs
        lows = self.candle_lows

        rsi = self.calculate_rsi(series, period=self.rsi_period)
        bb = self.calculate_bollinger_bands(series, period=self.bollinger_period, num_std=self.bollinger_std_dev)
        ema_fast = self.calculate_ema(series, period=self.ema_fast_period)
        ema_slow = self.calculate_ema(series, period=self.ema_slow_period)

        # Bollinger Bandwidth (%)
        bb_width_pct = 0.0
        if bb["middle"] > 0:
            bb_width_pct = ((bb["upper"] - bb["lower"]) / bb["middle"]) * 100.0

        # EMA Trend Slope (%)
        ema_trend_pct = 0.0
        if ema_slow > 0:
            ema_trend_pct = ((ema_fast - ema_slow) / ema_slow) * 100.0

        adx_data = self.calculate_adx(highs, lows, series, period=14)
        atr = self.calculate_atr(highs, lows, series, period=14)

        as_of = self._clock()
        quote_age = as_of - self.last_update_time if self.last_update_time else None
        candle_age = as_of - self.candle_close_times[-1] if self.candle_close_times else None
        quote_valid = not self._book_error and quote_age is not None and 0 <= quote_age <= self.max_quote_age_seconds
        reasons = []
        if not quote_valid:
            reasons.append(self._book_error or "stale_order_book")
        if self._candle_error:
            reasons.append(self._candle_error)
        if len(series) < self.required_candles:
            reasons.append("insufficient_closed_candles")
        if candle_age is None or not 0 <= candle_age <= self.max_candle_age_seconds:
            reasons.append("stale_or_missing_closed_candle")

        return {
            "symbol": self.symbol,
            "price": self.last_price,
            "bid": self.last_bid,
            "ask": self.last_ask,
            "spread_pct": self.last_spread_pct,
            "rsi": rsi,
            "bb_upper": bb["upper"],
            "bb_middle": bb["middle"],
            "bb_lower": bb["lower"],
            "bb_width_pct": round(bb_width_pct, 2),
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_trend_pct": round(ema_trend_pct, 2),
            "adx": adx_data["adx"],
            "plus_di": adx_data["plus_di"],
            "minus_di": adx_data["minus_di"],
            "atr": atr,
            "timestamp": self.last_update_time,
            "as_of": as_of,
            "quote_received_at": self.last_update_time,
            "quote_age_seconds": quote_age,
            "quote_valid": quote_valid,
            "last_closed_candle_at": self.candle_close_times[-1] if self.candle_close_times else None,
            "candle_age_seconds": candle_age,
            "closed_candles_count": len(series),
            "features_ready": not reasons,
            "data_quality_reasons": reasons,
            "feature_version": "closed-1m-wilder-v1",
            "quote_source": self.quote_source,
            "candle_source": self.candle_source,
            "candle_interval": "1m",
            "indicator_periods": {"rsi": self.rsi_period, "bb": self.bollinger_period,
                                  "ema_fast": self.ema_fast_period, "ema_slow": self.ema_slow_period,
                                  "adx": 14, "atr": 14},
            "history_len": len(self.price_history),
        }
