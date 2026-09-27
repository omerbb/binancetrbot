import time
import math
import hmac
import hashlib
import requests
import json
from urllib.parse import urlencode
from typing import Dict, Any, Optional, List
from requests.adapters import HTTPAdapter

class BinanceTrClient:
    """
    Binance TR (trbinance.com) REST API İstemcisi.
    Hem public piyasa verilerini hem de imzalı private emir işlemlerini yönetir.
    """
    def __init__(self, api_key: str = "", secret_key: str = "", base_url: str = "https://www.binance.tr"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=1)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "BinanceTrBot/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        # Public/global fallback requests must never inherit account credentials.
        self.kline_sources: Dict[str, str] = {}

    def _sign_payload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query_string = urlencode(params)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, endpoint: str, signed: bool = False, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        params = params or {}

        headers = {}
        if signed:
            if not self.api_key or not self.secret_key:
                raise ValueError("İmzalı işlem için API Key ve Secret Key tanımlanmalıdır.")
            headers["X-MBX-APIKEY"] = self.api_key
            params = self._sign_payload(params)

        try:
            if method.upper() == "GET":
                response = self.session.get(url, params=params, headers=headers, timeout=2.5)
            elif method.upper() == "POST":
                response = self.session.post(url, data=params, headers=headers, timeout=2.5)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, params=params, headers=headers, timeout=2.5)
            else:
                raise ValueError(f"Desteklenmeyen HTTP metodu: {method}")

            response.raise_for_status()
            data = response.json()
            return data
        except requests.exceptions.RequestException as e:
            return {"code": -1, "msg": f"Bağlantı hatası: {str(e)}", "data": None}

    # ==================== PUBLIC ENDPOINTS ====================

    def ping(self) -> bool:
        """
        Binance TR sunucu erişilebilirliğini kontrol eder.
        """
        try:
            res = self._request("GET", "/open/v1/common/time")
            return res.get("code") == 0 or "timestamp" in res
        except Exception:
            return False

    def get_server_time(self) -> int:
        res = self._request("GET", "/open/v1/common/time")
        if res.get("code") == 0:
            return res.get("timestamp", int(time.time() * 1000))
        return int(time.time() * 1000)

    def get_symbols(self) -> List[Dict[str, Any]]:
        res = self._request("GET", "/open/v1/common/symbols")
        if res.get("code") == 0 and "data" in res and "list" in res["data"]:
            return res["data"]["list"]
        return []

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        symbols = self.get_symbols()
        for s in symbols:
            if s.get("symbol") == symbol:
                return s
        return None

    def get_depth(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """
        Derinlik (Order Book): Alış ve satış tekliflerini döndürür.
        """
        params = {"symbol": symbol, "limit": limit}
        res = self._request("GET", "/open/v1/market/depth", params=params)
        return res

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> List[Any]:
        """
        Mum verileri (candlesticks):
        Hızlı ve stabil api.binance.com uç noktası üzerinden çekilir.
        """
        clean_symbol = symbol.replace("_", "").upper()
        urls = [
            f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={interval}&limit={limit}",
            f"https://api.binance.me/api/v1/klines?symbol={clean_symbol}&interval={interval}&limit={limit}",
        ]
        for url in urls:
            try:
                resp = self.session.get(url, timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        self.kline_sources[symbol] = url.split("/api/")[0]
                        return data
                    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                        self.kline_sources[symbol] = url.split("/api/")[0]
                        return data["data"]
            except Exception:
                continue

        # Son alternatif: doğrudan binance.tr open/v1
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        res = self._request("GET", "/open/v1/market/klines", params=params)
        if res.get("code") == 0 and "data" in res:
            data = res["data"]
            if isinstance(data, dict) and "list" in data:
                self.kline_sources[symbol] = self.base_url
                return data["list"]
            if isinstance(data, list):
                self.kline_sources[symbol] = self.base_url
                return data
        return []

    def get_recent_trades(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        params = {"symbol": symbol, "limit": limit}
        res = self._request("GET", "/open/v1/market/trades", params=params)
        if res.get("code") == 0 and "data" in res and "list" in res["data"]:
            return res["data"]["list"]
        return []

    def get_best_prices(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        En iyi alış (bid) ve en iyi satış (ask) fiyatlarını hesaplar.
        """
        depth = self.get_depth(symbol, limit=5)
        if not isinstance(depth, dict) or depth.get("code") != 0:
            return None
        data = depth.get("data")
        if not isinstance(data, dict):
            return None
        try:
            best_bid = float(data["bids"][0][0])
            best_ask = float(data["asks"][0][0])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if not all(math.isfinite(p) and p > 0 for p in (best_bid, best_ask)) or best_ask < best_bid:
            return None
        return {
            "bid": best_bid,
            "ask": best_ask,
            "mid": (best_bid + best_ask) / 2.0,
            "spread": best_ask - best_bid,
            "spread_pct": ((best_ask - best_bid) / best_bid) * 100.0,
        }

    # ==================== PRIVATE ENDPOINTS (Canlı Hesap İçin) ====================

    def get_account_spot(self) -> Dict[str, Any]:
        """
        Spot hesap bakiyeleri ve detayları.
        """
        return self._request("GET", "/open/v1/account/spot", signed=True)

    def get_account_info(self) -> Dict[str, Any]:
        """
        Spot hesap bilgisi için takma ad.
        """
        return self.get_account_spot()

    def get_asset(self, asset: str) -> Dict[str, Any]:
        """
        Belirli bir varlığın bakiyesi (örn: TRY, USDT).
        """
        return self._request("GET", "/open/v1/account/spot/asset", signed=True, params={"asset": asset})

    def create_order(self, symbol: str, side: int, order_type: int, quantity: float, price: Optional[float] = None) -> Dict[str, Any]:
        """
        Emir iletimi.
        side: 0 (BUY - Alış), 1 (SELL - Satış)
        order_type: 1 (LIMIT), 2 (MARKET)
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
        }
        if price is not None and order_type == 1:
            params["price"] = f"{price:.8f}".rstrip("0").rstrip(".")
        return self._request("POST", "/open/v1/orders", signed=True, params=params)

    def cancel_order(self, order_id: int, symbol: str) -> Dict[str, Any]:
        params = {"orderId": order_id, "symbol": symbol}
        return self._request("POST", "/open/v1/orders/cancel", signed=True, params=params)

    def get_order_detail(self, order_id: int, symbol: str) -> Dict[str, Any]:
        params = {"orderId": order_id, "symbol": symbol}
        return self._request("GET", "/open/v1/orders/detail", signed=True, params=params)

    def get_open_orders(self, symbol: str) -> Dict[str, Any]:
        params = {"symbol": symbol}
        return self._request("GET", "/open/v1/orders", signed=True, params=params)
