import time
import math
import uuid
from typing import List, Dict, Any, Optional

class SimulatorEngine:
    """
    Sanal Para (Paper Trading) Motoru.
    Binance TR gerçek tahta fiyatları üzerinde sıfır riskle al-sat simülasyonu yapar.
    Komisyon ve net tasfiye değerini hesaplar; likidite/kayma modeli içermez.
    """
    def __init__(self, initial_balance: float = 10000.0, fee_rate_pct: float = 0.1, *, clock=None):
        self._clock = clock or (lambda: time.time())
        if not math.isfinite(initial_balance) or initial_balance <= 0:
            raise ValueError("Başlangıç bakiyesi pozitif ve sonlu olmalı")
        self.initial_balance = initial_balance
        self.cash = initial_balance
        self.fee_rate_pct = fee_rate_pct
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.all_orders: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = [
            {"time": self._clock(), "equity": initial_balance}
        ]

    @property
    def fee_rate_pct(self) -> float:
        return self.fee_rate * 100.0

    @fee_rate_pct.setter
    def fee_rate_pct(self, value: float) -> None:
        if not math.isfinite(value) or not 0 <= value < 100:
            raise ValueError("Komisyon oranı 0 <= oran < 100 aralığında sonlu olmalı")
        self.fee_rate = value / 100.0
        for pos in getattr(self, "positions", {}).values():
            self._mark_position(pos)

    def _mark_position(self, pos: Dict[str, Any]) -> None:
        net = pos["quantity"] * pos.get("current_price", pos["entry_price"]) * (1 - self.fee_rate)
        pos["unrealized_pnl"] = net - pos["invested_cost"]
        pos["unrealized_pnl_pct"] = pos["unrealized_pnl"] / pos["invested_cost"] * 100.0

    def reset_session_trades(self) -> None:
        self.closed_trades = []
        self.all_orders = []
        self.equity_curve = [
            {"time": self._clock(), "equity": self.cash}
        ]

    def buy(self, symbol: str, price: float, budget_try: float, reason: str = "") -> Optional[Dict[str, Any]]:
        """
        Sanal alış emri simülasyonu.
        """
        if not all(math.isfinite(v) and v > 0 for v in (price, budget_try)):
            return None

        # Yeterli nakit var mı?
        available_budget = min(self.cash, budget_try)
        if available_budget < 10.0:  # Minimum işlem tutarı (Binance TR minNotional ~10 TL)
            return None

        fee = available_budget * self.fee_rate
        net_budget = available_budget - fee
        quantity = net_budget / price

        pos_id = str(uuid.uuid4())[:8]
        order = {
            "order_id": pos_id,
            "symbol": symbol,
            "side": "BUY",
            "price": price,
            "quantity": quantity,
            "gross_amount": available_budget,
            "net_amount": net_budget,
            "fee": fee,
            "timestamp": self._clock(),
            "reason": reason,
        }
        self.all_orders.append(order)

        position = {
            "position_id": pos_id,
            "symbol": symbol,
            "entry_price": price,
            "current_price": price,
            "highest_price": price,
            "quantity": quantity,
            "invested_cost": available_budget,
            "entry_fee": fee,
            "entry_time": self._clock(),
            "entry_reason": reason,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
        }
        self._mark_position(position)
        self.positions[pos_id] = position
        self.cash -= available_budget

        self._record_equity(price)
        return position

    def sell(self, position_id: str, price: float, fraction: float = 1.0, reason: str = "") -> Optional[Dict[str, Any]]:
        """
        Açık bir sanal pozisyonu satma emri simülasyonu.
        fraction: 1.0 ise tamamı satılır, < 1.0 ise kademeli kısmi satış (Partial TP) yapılır.
        """
        position = self.positions.get(position_id)
        if not position or not math.isfinite(price) or price <= 0 or not math.isfinite(fraction) or not 0 < fraction <= 1:
            return None
        total_quantity = position["quantity"]
        sold_quantity = total_quantity * fraction
        sold_cost = position["invested_cost"] * fraction
        sold_entry_fee = position.get("entry_fee", 0.0) * fraction

        gross_return = sold_quantity * price
        exit_fee = gross_return * self.fee_rate
        net_return = gross_return - exit_fee

        total_fees = sold_entry_fee + exit_fee
        net_pnl = net_return - sold_cost
        pnl_pct = ((net_return - sold_cost) / sold_cost) * 100.0 if sold_cost > 0 else 0.0

        is_full_close = fraction == 1.0
        order = {
            "order_id": str(uuid.uuid4())[:8],
            "position_id": position_id,
            "symbol": position["symbol"],
            "side": "SELL" if is_full_close else "PARTIAL_SELL",
            "entry_price": position["entry_price"],
            "exit_price": price,
            "quantity": sold_quantity,
            "gross_return": gross_return,
            "net_return": net_return,
            "invested_cost": sold_cost,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "hold_time_seconds": self._clock() - position["entry_time"],
            "timestamp": self._clock(),
            "reason": reason,
            "is_win": net_pnl > 0,
            "is_partial": not is_full_close,
        }
        self.all_orders.append(order)
        self.closed_trades.append(order)

        self.cash += net_return

        if is_full_close:
            del self.positions[position_id]
        else:
            position["quantity"] -= sold_quantity
            position["invested_cost"] -= sold_cost
            position["entry_fee"] -= sold_entry_fee
            position["partial_tp_taken"] = True
            # Kısmi satış sonrası maliyeti koru
            position["breakeven_locked"] = True
            position["current_price"] = price
            self._mark_position(position)

        self._record_equity(price)
        return order

    def update_market_price(self, symbol: str, current_price: float) -> None:
        """
        Açık pozisyonların anlık kâr/zararlarını günceller.
        """
        for pos in self.positions.values():
            if pos["symbol"] == symbol and math.isfinite(current_price) and current_price > 0:
                pos["current_price"] = current_price
                if current_price > pos.get("highest_price", pos["entry_price"]):
                    pos["highest_price"] = current_price

                current_gross = pos["quantity"] * current_price
                est_exit_fee = current_gross * self.fee_rate
                est_net = current_gross - est_exit_fee
                pos["unrealized_pnl"] = est_net - pos["invested_cost"]
                pos["unrealized_pnl_pct"] = ((est_net - pos["invested_cost"]) / pos["invested_cost"]) * 100.0

        self._record_equity(current_price)

    def _record_equity(self, current_price: float = 0.0) -> None:
        invested_value = sum(
            pos["quantity"] * pos.get("current_price", pos["entry_price"])
            for pos in self.positions.values()
        )
        total_equity = self.cash + invested_value * (1 - self.fee_rate)
        self.equity_curve.append({
            "time": self._clock(),
            "equity": round(total_equity, 2),
        })
        if len(self.equity_curve) > 200:
            self.equity_curve.pop(0)

    def get_summary(self, current_price: float = 0.0) -> Dict[str, Any]:
        """
        Portföy durumu ve anlık kâr/zarar özetini döner.
        Çoklu coin sepetinde her coinin kendi anlık fiyatını kullanır.
        """
        invested_value = sum(
            pos["quantity"] * pos.get("current_price", pos["entry_price"])
            for pos in self.positions.values()
        )
        total_equity = self.cash + invested_value * (1 - self.fee_rate)
        realized_pnl = sum(trade["net_pnl"] for trade in self.closed_trades)
        unrealized_pnl = sum(pos.get("unrealized_pnl", 0.0) for pos in self.positions.values())
        total_pnl = total_equity - self.initial_balance
        total_pnl_pct = (total_pnl / self.initial_balance) * 100.0

        total_trades = len(self.closed_trades)
        winning_trades = [t for t in self.closed_trades if t["is_win"]]
        losing_trades = [t for t in self.closed_trades if not t["is_win"]]
        win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0

        return {
            "initial_balance": round(self.initial_balance, 2),
            "cash": round(self.cash, 2),
            "invested_value": round(invested_value, 2),
            "estimated_exit_fees": round(invested_value * self.fee_rate, 2),
            "equity_basis": "estimated_net_liquidation",
            "total_equity": round(total_equity, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "open_positions_count": len(self.positions),
            "open_positions": list(self.positions.values()),
            "total_closed_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": round(win_rate, 2),
            "closed_trades": list(reversed(self.closed_trades)),
        }
