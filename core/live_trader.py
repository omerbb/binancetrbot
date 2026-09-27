import os
import json
import time
import math
import logging
from typing import Dict, Any, Optional, List
from core.binance_client import BinanceTrClient

logger = logging.getLogger("LiveTraderEngine")

class LiveTraderEngine:
    """
    Binance TR Gerçek Hesap İşlem Motoru.
    API Key ve Secret ile gerçek alım ve satım emirlerini iletir, bakiye ve pozisyonları kontrol eder.
    Cüzdan ile çift yönlü senkronizasyon ve disk kalıcılığı sağlar.
    """
    def __init__(self, client: BinanceTrClient, symbol: str = "AUTO", max_open_positions: int = 5, *, sync_on_start: bool = True):
        self.client = client
        self.symbol = symbol
        self.max_open_positions = max_open_positions
        self.storage_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "live_positions.json")
        self.trades_storage_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "live_trades.json")
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.session_initial_balance: Optional[float] = None
        self._symbol_info_cache: Dict[str, Dict[str, Any]] = {}
        self._cached_balances: Dict[str, float] = {}
        self._last_balance_time: float = 0.0

        # 1. Kayıtlı pozisyon ve işlem geçmişini diskten yükle
        if sync_on_start:
            self._load_from_disk()

        # 2. Binance TR cüzdanındaki gerçek varlıklarla senkronize et
        if sync_on_start:
            self.sync_with_wallet()

    def _save_to_disk(self) -> None:
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
            with open(self.trades_storage_path, "w", encoding="utf-8") as f:
                json.dump(self.closed_trades, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Pozisyonlar diske kaydedilemedi: {e}")

    def _load_from_disk(self) -> None:
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.positions = json.load(f)
            if os.path.exists(self.trades_storage_path):
                with open(self.trades_storage_path, "r", encoding="utf-8") as f:
                    self.closed_trades = json.load(f)
        except Exception as e:
            logger.error(f"Pozisyonlar diskten okunamadı: {e}")

    def reset_session_trades(self) -> None:
        """
        Yeni bir canlı oturum başladığında tamamlanan işlemleri sıfırlar.
        Eski işlemleri data/history/ klasörüne arşivler.
        """
        if self.closed_trades:
            try:
                archive_dir = os.path.join(os.path.dirname(self.trades_storage_path), "history")
                os.makedirs(archive_dir, exist_ok=True)
                archive_file = os.path.join(archive_dir, f"trades_{int(time.time())}.json")
                with open(archive_file, "w", encoding="utf-8") as f:
                    json.dump(self.closed_trades, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"İşlem geçmişi arşivlenirken hata: {e}")

        self.closed_trades = []
        self.session_initial_balance = None
        self._save_to_disk()
        logger.info("🧹 Yeni canlı oturum için tamamlanan işlemler sıfırlandı.")

    def sync_with_wallet(self) -> None:
        """
        Binance TR spot cüzdanındaki coin varlıklarını tarar.
        Bot yeniden başlatıldığında açık olan veya daha önce alınmış canlı pozisyonları
        otomatik olarak tespit edip 'positions' listesine dahil eder.
        """
        if not getattr(self.client, "api_key", None) or not getattr(self.client, "secret_key", None):
            return
        try:
            res = self.client.get_account_spot()
            if res.get("code") != 0 or "data" not in res:
                return

            assets = res["data"].get("accountAssets") or []
            existing_symbols = {p.get("symbol"): p_id for p_id, p in self.positions.items()}
            active_wallet_symbols = set()

            for a in assets:
                asset_name = a.get("asset", "")
                if not asset_name or asset_name == "TRY":
                    continue

                free_qty = float(a.get("free", 0.0))
                if free_qty <= 0:
                    continue

                pair_symbol = f"{asset_name}_TRY"
                prices = self.client.get_best_prices(pair_symbol)
                if not prices or prices.get("bid", 0) <= 0:
                    continue

                cur_price = prices["bid"]
                total_val = free_qty * cur_price

                # Binance TR minimum işlem değeri 10 TL altındaki tozları dahil etme
                if total_val < 10.0:
                    continue

                active_wallet_symbols.add(pair_symbol)

                if pair_symbol in existing_symbols:
                    p_id = existing_symbols[pair_symbol]
                    self.positions[p_id]["quantity"] = free_qty
                    self.positions[p_id]["current_price"] = cur_price
                    entry = self.positions[p_id].get("entry_price", cur_price)
                    if entry <= 0:
                        entry = cur_price
                        self.positions[p_id]["entry_price"] = entry
                    self.positions[p_id]["unrealized_pnl"] = round((cur_price - entry) * free_qty, 2)
                    self.positions[p_id]["unrealized_pnl_pct"] = round(((cur_price - entry) / entry * 100.0), 2) if entry > 0 else 0.0
                else:
                    p_id = f"wallet_{asset_name}_{int(time.time())}"
                    self.positions[p_id] = {
                        "position_id": p_id,
                        "symbol": pair_symbol,
                        "entry_price": cur_price,
                        "current_price": cur_price,
                        "highest_price": cur_price,
                        "quantity": free_qty,
                        "invested_cost": round(total_val, 2),
                        "entry_time": time.time(),
                        "entry_reason": "Binance TR Cüzdanından Devralındı",
                        "unrealized_pnl": 0.0,
                        "unrealized_pnl_pct": 0.0,
                    }
                    logger.info(f"🔄 Binance TR Cüzdanından Canlı Pozisyon Devralındı: {pair_symbol} (Miktar: {free_qty}, Değer: {total_val:.2f} TL)")

            # Cüzdanda artık sıfır olan ama bellekte kalmış pozisyonları temizle
            to_remove = []
            for p_id, p in self.positions.items():
                sym = p.get("symbol")
                if sym and sym.endswith("_TRY") and sym not in active_wallet_symbols:
                    to_remove.append(p_id)
            for p_id in to_remove:
                del self.positions[p_id]

            self._save_to_disk()
        except Exception as e:
            logger.error(f"Cüzdan pozisyon senkronizasyonu hatası: {e}")

    def get_symbol_rules(self, symbol: str) -> Optional[Dict[str, Any]]:
        if symbol not in self._symbol_info_cache:
            info = self.client.get_symbol_info(symbol)
            if info:
                self._symbol_info_cache[symbol] = info
        return self._symbol_info_cache.get(symbol)

    def format_quantity(self, symbol: str, qty: float) -> float:
        """
        Sembolün LOT_SIZE kurallarına göre miktarı yuvarlar.
        """
        info = self.get_symbol_rules(symbol)
        step_size = 0.0001
        min_qty = 0.0001
        if info and "filters" in info:
            for f in info["filters"]:
                if f.get("filterType") == "LOT_SIZE":
                    step_size = float(f.get("stepSize", "0.0001"))
                    min_qty = float(f.get("minQty", "0.0001"))
                    break
        precision = max(0, int(round(-math.log10(step_size)))) if step_size < 1 else 0
        formatted = round(math.floor(qty / step_size) * step_size, precision)
        return formatted if formatted >= min_qty else 0.0

    def get_real_balances(self) -> Dict[str, float]:
        """
        Binance TR'den gerçek bakiye durumunu çeker.
        (Binance TR API 'accountAssets' alanı kullanır)
        """
        try:
            res = self.client.get_account_spot()
            balances: Dict[str, float] = {"TRY": 0.0, "USDT": 0.0}
            if res.get("code") == 0 and "data" in res:
                asset_list = res["data"].get("accountAssets") or res["data"].get("balances") or []
                for b in asset_list:
                    asset = b.get("asset")
                    free = float(b.get("free", 0.0))
                    locked = float(b.get("locked", 0.0))
                    total = free + locked
                    if asset:
                        balances[asset] = total
            return balances
        except Exception as e:
            logger.error(f"Gerçek bakiye sorgulama hatası: {e}")
            return self._cached_balances or {"TRY": 0.0, "USDT": 0.0}

    def buy(self, symbol: str, price: float, budget_try: float, reason: str = "") -> Optional[Dict[str, Any]]:
        """
        Gerçek Binance TR alış emri gönderir.
        """
        if price <= 0 or budget_try < 10.0:
            return None

        # Gerçek TRY bakiyesini kontrol et
        balances = self.get_real_balances()
        available_try = balances.get("TRY", 0.0)
        actual_budget = min(available_try, budget_try)
        if actual_budget < 10.0:
            logger.warning(f"Yetersiz TRY bakiyesi: {available_try:.2f} TL (Gereken: {budget_try:.2f} TL)")
            return None

        raw_qty = actual_budget / price
        qty = self.format_quantity(symbol, raw_qty)
        if qty <= 0:
            logger.warning(f"Geçersiz lot miktarı [{symbol}]: raw={raw_qty}, formatted={qty}")
            return None

        # side=0 (BUY), type=2 (MARKET)
        res = self.client.create_order(
            symbol=symbol,
            side=0,  # BUY
            order_type=2,  # MARKET
            quantity=qty
        )

        if res.get("code") == 0 and "data" in res:
            order_id = str(res["data"].get("orderId"))
            actual_cost = round(qty * price, 2)
            pos = {
                "position_id": order_id,
                "symbol": symbol,
                "entry_price": price,
                "current_price": price,
                "highest_price": price,
                "quantity": qty,
                "invested_cost": actual_cost,
                "entry_time": time.time(),
                "entry_reason": reason,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
            }
            self.positions[order_id] = pos
            self._save_to_disk()
            # Bakiye önbelleğini sıfırla ki güncel çekilsin
            self._last_balance_time = 0.0
            return pos
        else:
            logger.error(f"Binance TR Alış Emri Başarısız [{symbol}]: {res}")
        return None

    def sell(self, position_id: str, price: float, fraction: float = 1.0, reason: str = "") -> Optional[Dict[str, Any]]:
        """
        Gerçek Binance TR satış emri gönderir.
        fraction: 1.0 ise tamamı satılır, < 1.0 ise kademeli kısmi satış (Partial TP) yapılır.
        """
        pos = self.positions.get(position_id)
        if not pos or price <= 0:
            return None

        sym = pos.get("symbol", self.symbol)
        base_asset = sym.replace("_TRY", "").replace("TRY", "")

        fraction = max(0.01, min(1.0, fraction))
        is_full_close = fraction >= 0.999

        # Cüzdandaki reel serbest bakiyeyi kontrol et (Borsanın komisyon kestiği net miktar)
        real_balances = self.get_real_balances()
        wallet_free = real_balances.get(base_asset, 0.0)

        # Cüzdandaki reel miktar ile kayıtlı miktarın uygun olanını al
        available_qty = wallet_free if wallet_free > 0 else pos.get("quantity", 0.0)
        target_qty = available_qty * fraction
        qty = self.format_quantity(sym, target_qty)

        if qty <= 0:
            logger.warning(f"Satış için bakiye bulunamadı veya coin zaten satılmış [{sym}]. Cüzdan Serbest: {wallet_free}")
            if wallet_free <= 0.001 and is_full_close:
                del self.positions[position_id]
                self._save_to_disk()
            return None

        res = self.client.create_order(
            symbol=sym,
            side=1,  # SELL
            order_type=2,  # MARKET
            quantity=qty
        )

        if res.get("code") == 0:
            gross_return = price * qty
            entry_cost = pos.get("invested_cost", pos["entry_price"] * available_qty) * fraction
            pnl = gross_return - entry_cost
            pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100.0 if pos["entry_price"] > 0 else 0.0
            trade_record = {
                "order_id": position_id,
                "symbol": sym,
                "side": "SELL" if is_full_close else "PARTIAL_SELL",
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "quantity": qty,
                "invested_cost": entry_cost,
                "net_pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "timestamp": time.time(),
                "reason": reason,
                "is_win": pnl > 0,
                "is_partial": not is_full_close,
            }
            self.closed_trades.append(trade_record)
            if is_full_close:
                del self.positions[position_id]
            else:
                pos["quantity"] = max(0.0, available_qty - qty)
                pos["invested_cost"] = max(0.0, pos.get("invested_cost", 0.0) - entry_cost)
                pos["partial_tp_taken"] = True
                pos["breakeven_locked"] = True
            self._save_to_disk()
            self._last_balance_time = 0.0
            logger.info(f"✅ Satış Başarılı [{sym}]: Miktar={qty} (Oran: %{int(fraction*100)}) | Fiyat={price:.4f} TL | K/Z={pnl:.2f} TL")
            return trade_record
        else:
            logger.error(f"Binance TR Satış Emri Başarısız [{sym}]: {res}")
            # Eğer bakiye hatası (Insufficient balance) alınırsa, anlık cüzdanı doğrudan çekip tekrar dene
            fresh_account = self.client.get_account_spot()
            if fresh_account.get("code") == 0 and "data" in fresh_account:
                for a in fresh_account["data"].get("accountAssets", []):
                    if a.get("asset") == base_asset:
                        fresh_free = float(a.get("free", 0.0))
                        retry_qty = self.format_quantity(sym, fresh_free)
                        if retry_qty > 0 and retry_qty != qty:
                            logger.info(f"🔄 Satış Emri Düzeltilerek Tekrar Deneniyor [{sym}]: {retry_qty}")
                            retry_res = self.client.create_order(
                                symbol=sym,
                                side=1,
                                order_type=2,
                                quantity=retry_qty
                            )
                            if retry_res.get("code") == 0:
                                gross_return = price * retry_qty
                                entry_cost = pos.get("invested_cost", pos["entry_price"] * retry_qty)
                                pnl = gross_return - entry_cost
                                pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100.0 if pos["entry_price"] > 0 else 0.0
                                trade_record = {
                                    "order_id": position_id,
                                    "symbol": sym,
                                    "side": "SELL",
                                    "entry_price": pos["entry_price"],
                                    "exit_price": price,
                                    "quantity": retry_qty,
                                    "invested_cost": entry_cost,
                                    "net_pnl": round(pnl, 2),
                                    "pnl_pct": round(pnl_pct, 2),
                                    "timestamp": time.time(),
                                    "reason": reason,
                                    "is_win": pnl > 0,
                                }
                                self.closed_trades.append(trade_record)
                                del self.positions[position_id]
                                self._save_to_disk()
                                self._last_balance_time = 0.0
                                return trade_record
        return None

    def update_market_price(self, symbol: str, current_price: float) -> None:
        """
        Açık canlı pozisyonların anlık fiyat ve kâr/zararlarını günceller.
        """
        if current_price <= 0:
            return
        for pos in self.positions.values():
            if pos.get("symbol") == symbol:
                pos["current_price"] = current_price
                if current_price > pos.get("highest_price", pos.get("entry_price", current_price)):
                    pos["highest_price"] = current_price
                entry = float(pos.get("entry_price", current_price))
                if entry <= 0:
                    entry = current_price
                    pos["entry_price"] = entry
                qty = float(pos.get("quantity", 0.0))
                pos["unrealized_pnl"] = round((current_price - entry) * qty, 2)
                pos["unrealized_pnl_pct"] = round(((current_price - entry) / entry * 100.0), 2) if entry > 0 else 0.0

    def get_summary(self, current_price: float = 0.0) -> Dict[str, Any]:
        """
        Binance TR gerçek hesap portföy ve bakiye özeti.
        """
        now = time.time()
        if now - self._last_balance_time > 4.0 or not self._cached_balances:
            self._cached_balances = self.get_real_balances()
            self._last_balance_time = now

        balances = self._cached_balances
        try_cash = balances.get("TRY", 0.0)

        invested_value = 0.0
        open_pos_list = []
        for pos_id, pos in self.positions.items():
            sym = pos.get("symbol", self.symbol)
            qty = float(pos.get("quantity", 0.0))
            entry = float(pos.get("entry_price", 0.0))
            cur = float(pos.get("current_price", 0.0))
            if cur <= 0:
                cur = entry
            if entry <= 0:
                entry = cur
                pos["entry_price"] = entry
            if pos.get("current_price", 0.0) <= 0:
                pos["current_price"] = cur

            val = qty * cur
            invested_value += val
            unrealized = (cur - entry) * qty if entry > 0 else 0.0
            unrealized_pct = ((cur - entry) / entry * 100.0) if entry > 0 else 0.0
            open_pos_list.append({
                "position_id": pos_id,
                "symbol": sym,
                "entry_price": entry,
                "current_price": cur,
                "quantity": qty,
                "invested_cost": pos.get("invested_cost", entry * qty),
                "unrealized_pnl": round(unrealized, 2),
                "unrealized_pnl_pct": round(unrealized_pct, 2),
                "highest_price": pos.get("highest_price", entry),
                "breakeven_locked": pos.get("breakeven_locked", False),
            })

        if self.session_initial_balance is None and (try_cash > 0 or invested_value > 0):
            self.session_initial_balance = try_cash + invested_value

        initial_balance = self.session_initial_balance or (try_cash + invested_value)
        realized_pnl = sum(t.get("net_pnl", 0.0) for t in self.closed_trades)
        unrealized_pnl = sum(p["unrealized_pnl"] for p in open_pos_list)
        
        # REST API bakiye güncelleme gecikmesinden (settlement lag) etkilenmeyen sağlam K/Z hesabı
        total_pnl = realized_pnl + unrealized_pnl
        total_pnl_pct = (total_pnl / initial_balance * 100.0) if initial_balance > 0 else 0.0
        total_equity = initial_balance + total_pnl

        win_count = sum(1 for t in self.closed_trades if t.get("is_win", False))
        total_trades = len(self.closed_trades)
        win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0

        return {
            "initial_balance": round(initial_balance, 2),
            "cash": round(try_cash, 2),
            "invested_value": round(invested_value, 2),
            "total_equity": round(total_equity, 2),
            "open_positions": open_pos_list,
            "closed_trades": self.closed_trades,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "win_rate": round(win_rate, 1),
            "total_trades": total_trades,
            "winning_trades": win_count,
            "losing_trades": total_trades - win_count,
            "real_balances": balances,
        }
