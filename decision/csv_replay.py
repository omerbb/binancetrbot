"""Convert 1m OHLCV CSV to explicitly synthetic quotes at the NEXT bar's open.

Current bar's high/low/close/volume never enter the frame. There is no reconstruction of
real historical bid/ask or intra-minute ticks. Input bars must be contiguous, sorted UTC.
"""
from __future__ import annotations
import csv
import json
import math
import re
from pathlib import Path
from decision.contracts import finite_number


def convert_ohlcv_csv(input_path, output_path, *, symbol, spread_bps):
    if not re.fullmatch(r"[A-Z0-9]+_TRY", symbol): raise ValueError("Expected a symbol such as SOL_TRY")
    spread_bps = finite_number(spread_bps, 0, 1000)
    if Path(input_path).resolve() == Path(output_path).resolve(): raise ValueError("Input and output must differ")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    previous = None
    count = 0
    with open(input_path, newline="", encoding="utf-8-sig") as src, open(output_path, "w", encoding="utf-8") as out:
        reader = csv.DictReader(src)
        required = {"open_time_ms", "open", "high", "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []): raise ValueError("CSV columns required: " + ",".join(sorted(required)))
        for number, row in enumerate(reader, 2):
            try:
                ot = int(row["open_time_ms"])
                if ot < 0 or ot % 60000: raise ValueError("open_time_ms must be a UTC minute boundary")
                o, h, l, c = [finite_number(float(row[k]), 1e-10) for k in ("open", "high", "low", "close")]
                vol = finite_number(float(row["volume"]), 0)
                if not l <= min(o, c) <= max(o, c) <= h: raise ValueError("Invalid OHLC")
                if previous is not None and ot != previous[0] + 60000: raise ValueError("Bars must be contiguous and sorted; gaps are not fabricated")
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"Invalid CSV row {number}: {exc}") from exc
            half = spread_bps / 20000
            frame = {"as_of": ot / 1000, "markets": {symbol: {
                "bid": o * (1 - half), "ask": o * (1 + half), "quote_timestamp": ot / 1000,
                "quote_source": "synthetic:current_bar_open_with_assumed_spread",
                "sample_kind": "synthetic_minute_open_not_real_orderbook",
                "closed_candles": [previous] if previous is not None else [],
                "synthetic_assumptions": {"spread_bps": spread_bps, "intrabar_path_unknown": True}}}}
            out.write(json.dumps(frame, allow_nan=False, separators=(",", ":")) + "\n")
            previous = [ot, o, h, l, c, vol, ot + 59999]
            count += 1
    return {"frames": count, "synthetic_quotes": True, "spread_bps": spread_bps,
            "current_bar_hlc_never_visible_to_same_frame": True}
