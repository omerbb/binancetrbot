"""Expected-value arithmetic shared by the controller and the offline evaluation.

The model supplies only a probability distribution over 5-minute mid-price buckets; the
provider turns it into an expected mid return with fixed per-bucket means measured on the
training split. Costs are deterministic here, mirroring the journal's markout label:
buy at the ask, sell at the bid later, fees and slippage on both sides.
"""
from __future__ import annotations

import math


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def entry_costs_pct(market: dict, fee_rate_pct: float, slippage_bps: float):
    """Round-trip cost in percent for buying now and selling later, or None without a valid quote."""
    bid, ask = (market or {}).get("bid"), (market or {}).get("ask")
    if not (_finite(bid) and _finite(ask)) or not 0 < bid <= ask:
        return None
    spread = (ask / bid - 1) * 100
    return {"spread_pct": spread, "round_trip_fee_pct": 2 * fee_rate_pct,
            "round_trip_slippage_pct": 2 * slippage_bps / 100,
            "total_pct": spread + 2 * fee_rate_pct + 2 * slippage_bps / 100}


def entry_edge_pct(expected_mid_return_pct, market, fee_rate_pct, slippage_bps):
    """Expected net return of an entry held 5 minutes, or None when it cannot be computed."""
    costs = entry_costs_pct(market, fee_rate_pct, slippage_bps)
    if costs is None or not _finite(expected_mid_return_pct):
        return None, costs
    return expected_mid_return_pct - costs["total_pct"], costs
