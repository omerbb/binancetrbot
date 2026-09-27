"""Forward-only historical replay using the SAME Jev controller and simulator.

JSONL frames contain observed quotes and completed 1m candles. Future candles are ignored,
not exposed to a model or feature calculator. There are no external market-data calls.
Quote fills assume zero inference/execution latency; this is disclosed in each run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from core.market_scanner import MarketScanner
from decision.contracts import finite_number


class ReplayClock:
    def __init__(self, now=0.0):
        self.now = float(now)
    def __call__(self):
        return self.now
    def advance_to(self, timestamp):
        value = finite_number(timestamp, 0)
        if value < self.now:
            raise ValueError("Replay timestamps must be nondecreasing")
        self.now = value


class ReplayFeed:
    def __init__(self, clock):
        self.clock = clock
        self.markets = {}
        self.candles = {}
        self.last_frame = None

    def apply(self, frame):
        timestamp = finite_number(frame.get("as_of"), 0)
        if self.last_frame is not None and timestamp <= self.last_frame:
            raise ValueError("Replay frames must have strictly increasing as_of; group symbols into one frame")
        markets = frame.get("markets")
        if not isinstance(markets, dict) or not markets:
            raise ValueError("Replay frame needs a markets map")
        parsed = {}
        updates = {}
        for symbol, raw in markets.items():
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+_TRY", symbol) or not isinstance(raw, dict):
                raise ValueError("Expected a TRY spot symbol and a market object")
            bid, ask = finite_number(raw.get("bid"), 0.0000000001), finite_number(raw.get("ask"), 0.0000000001)
            quote_at = finite_number(raw.get("quote_timestamp", timestamp), 0, timestamp)
            if ask < bid: raise ValueError("Replay ask must be >= bid")
            ticker = copy.deepcopy(raw.get("ticker_24h"))
            if ticker is not None:
                if not isinstance(ticker, dict): raise ValueError("ticker_24h must be an object")
                finite_number(ticker.get("as_of"), 0, timestamp)
                finite_number(ticker.get("volume_try"), 0)
                finite_number(ticker.get("change_pct"))
                lo, hi = finite_number(ticker.get("low"), 1e-10), finite_number(ticker.get("high"), 1e-10)
                if lo > hi: raise ValueError("ticker_24h low exceeds high")
            parsed[symbol] = {"bid": bid, "ask": ask, "timestamp": quote_at,
                              "source": raw.get("quote_source", "replay:provided_historical_bid_ask"),
                              "ticker": ticker,
                              "sample_kind": raw.get("sample_kind", "historical_quote"), "observed_at": timestamp}
            if not isinstance(parsed[symbol]["source"], str): raise ValueError("quote_source must be a string")
            current = dict(self.candles.get(symbol, {}))
            for candle in raw.get("closed_candles", []):
                if not isinstance(candle, (list, tuple)) or len(candle) < 7:
                    raise ValueError("Replay candle must use Binance's timestamped 1m row format")
                ot, ct = finite_number(candle[0], 0), finite_number(candle[6], 0)
                if ct / 1000 >= timestamp:
                    continue  # Strict information boundary, including during warmup.
                if ct < ot or abs(ct - ot - 59999) > 1:
                    raise ValueError("Only timestamped 1m candles are supported")
                o, h, l, c = [finite_number(float(candle[i]), 0.0000000001) for i in (1, 2, 3, 4)]
                vol = finite_number(float(candle[5]), 0)
                if h < max(o, c) or l > min(o, c) or l > h: raise ValueError("Invalid replay OHLC")
                row = [ot, o, h, l, c, vol, ct]
                if ot in current and current[ot] != row:
                    raise ValueError("Historical candle revisions require a new dataset version")
                current[ot] = row
            # No future full-file history is loaded into the runtime.
            keys = sorted(current)
            current = {k: current[k] for k in keys[-1500:]}
            updates[symbol] = current
        self.clock.advance_to(timestamp)
        self.markets.update(parsed)
        self.candles.update(updates)
        self.last_frame = timestamp


class ReplayClient:
    api_key = ""
    secret_key = ""
    base_url = "replay://local"

    def __init__(self, feed):
        self.feed = feed
        self.kline_sources = {}

    def get_symbols(self):
        # Universe membership is revealed frame by frame, never by reading future listings.
        return [{"symbol": s, "spotTradingEnable": 1, "baseAsset": s[:-4], "quoteAsset": "TRY"} for s in self.feed.markets]

    def get_best_prices(self, symbol):
        row = self.feed.markets.get(symbol)
        return {k: row[k] for k in ("bid", "ask", "timestamp", "source")} if row else {}

    def get_klines(self, symbol, interval="1m", limit=50):
        if interval != "1m": raise ValueError("Only 1m replay candles are supported")
        self.kline_sources[symbol] = "replay:closed_candles_only"
        data = self.feed.candles.get(symbol, {})
        return [data[t] for t in sorted(data)[-limit:]]

    def create_order(self, *args, **kwargs):
        raise RuntimeError("Replay must never send exchange orders")


class ReplayScanner(MarketScanner):
    def __init__(self, client, clock):
        super().__init__(client=client, clock=clock)
        self.feature_only = True
        self.feed = client.feed

    def scan_top_active_pairs(self, limit=0, force_refresh=False, only_uptrend=False, min_gain_pct=0):
        pairs = []
        now = self._clock()
        for symbol, data in sorted(self.feed.markets.items()):
            age = now - data["timestamp"]
            if not 0 <= age <= self.max_source_age_seconds: continue
            ticker = data.get("ticker") or {}
            macro_fresh = bool(ticker) and 0 <= now - ticker["as_of"] <= self.max_source_age_seconds
            if not macro_fresh: ticker = {}
            vol = float(ticker.get("volume_try", 0.0))
            self.tracker.record_tick(symbol, data["bid"], volume_try=vol, timestamp=data["timestamp"])
            micro = self.tracker.get_micro_metrics(symbol)
            pair = {"symbol": symbol, "price": data["bid"], "change_pct": float(ticker.get("change_pct", 0.0)),
                    "volume_try": vol, "high": float(ticker.get("high", data["bid"])), "low": float(ticker.get("low", data["bid"])),
                    "updated_at": data["timestamp"], "source_close_time_ms": data["timestamp"] * 1000,
                    "source": "replay:provided_past_information", "macro_features_available": macro_fresh,
                    "macro_as_of": ticker.get("as_of"),
                    "volume_surge_is_proxy": True, "true_rvol_available": False,
                    "sample_kind": data["sample_kind"], "micro_metrics": micro,
                    "observation": self.tracker.get_info(symbol), **micro}
            # Missing macro data is explicit; these legacy numeric fields are not factual 24h measurements.
            if not macro_fresh: pair["unavailable_features"] = ["change_pct", "volume_try", "high", "low"]
            pair["quant_score"] = self.calculate_quant_score(pair, micro)
            pair["is_qualified_1m"] = micro.get("is_qualified", False)
            pairs.append(pair)
        self.cached_top_pairs = pairs
        self.last_scan_time = now
        return pairs[:limit] if limit else pairs


def iter_frames(path):
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip(): continue
            try:
                frame = json.loads(line)
                if not isinstance(frame, dict): raise ValueError("Frame must be an object")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid replay JSONL on line {number}: {exc}") from exc
            yield frame


def run_replay(config, path, *, provider=None):
    from bot import BinanceTrBot
    if config.trading.mode != "simulation" or config.decision.engine != "jev":
        raise ValueError("Replay requires trading.mode=simulation and decision.engine=jev")
    clock = ReplayClock()
    feed = ReplayFeed(clock)
    frames = iter_frames(path)
    first = next(frames, None)
    if first is None: raise ValueError("Empty replay input")
    feed.apply(first)
    client = ReplayClient(feed)
    scanner = ReplayScanner(client, clock)
    bot = BinanceTrBot(config, clock=clock, client=client, scanner=scanner, decision_provider=provider)
    ctl = bot._jev()
    ctl.environment = "historical_replay"
    h = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""): h.update(chunk)
    ctl.extra_metadata = {"historical_data_sha256": h.hexdigest(), "historical_data_name": Path(path).name,
                          "execution_latency_seconds": 0, "inference_latency_in_virtual_time_seconds": 0,
                          "liquidity_depth_modeled": False, "survivorship_bias_depends_on_input_universe": True}
    bot.session_start_time = clock()
    bot.session_duration_seconds = 0
    bot.is_running = True
    ctl.prepare_start()
    count = 0
    end_reason = "historical_data_end"
    try:
        import itertools
        for frame in itertools.chain([None], frames):
            if frame is not None: feed.apply(frame)
            bot.step()
            count += 1
            if getattr(provider, "call_budget_reached", False):
                end_reason = "http_call_budget_reached"
                break
            if not bot.is_running: break
        bot.is_running = False
        ctl.end_session(reason=end_reason, liquidate=True)
        return {"run_id": ctl.run_id, "frames_processed": count, "provider": ctl.provider.name,
                "fixture_not_jev": bool(getattr(ctl.provider, "is_fixture", False)),
                "end_reason": end_reason,
                "portfolio": bot.simulator.get_summary(), "journal": ctl.store.stats(ctl.run_id),
                "database_path": config.decision.database_path}
    except Exception:
        bot.is_running = False
        try: ctl.end_session(reason="replay_interrupted", liquidate=False)
        except Exception: pass
        raise
    finally:
        # A budget/stop can break before the JSONL generator is exhausted.
        # Close it explicitly; Windows cannot unlink an open replay input.
        frames.close()
        ctl.close()
