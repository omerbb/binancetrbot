"""Model-owned discretionary decisions, deterministic guardrails, recorded paper fills.

The legacy bot.step is NEVER entered when decision.engine is jev or laya. Legacy thresholds
and signals are input features only (except explicit hard safety boundaries below).
The engines differ only in the DecisionProvider; a local provider additionally unlocks
batched evaluation and the outcome-trained expected-value policy.
"""
from __future__ import annotations

import copy
import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from decision.contracts import (SCHEMA_VERSION, ENGINE_LABELS, clean, code_digest, digest, policy_version_for,
                                public_config, validate_config, validate_response, dumps)
from decision.journal import DecisionJournal
from decision.openrouter import ProviderError
from decision.expected_value import entry_edge_pct
from decision.providers import make_provider
from decision.inference import InferenceRunner
from decision.questions import FORECAST_ID, portfolio_questions, symbol_questions, prebuy_questions


class JevController:
    def __init__(self, bot, provider=None):
        validate_config(bot.config)
        self.bot = bot
        self.cfg = copy.deepcopy(bot.config)
        self.clock = bot._clock
        self.engine = self.cfg.decision.engine
        self.label = ENGINE_LABELS.get(self.engine, self.engine)
        self.policy_version = policy_version_for(self.engine)
        self.forecast = self.engine == "laya"  # the outcome-trained forecast question rides along
        self.provider = provider or make_provider(self.cfg.decision)
        self.inference = InferenceRunner(self.provider)
        self.store = DecisionJournal(self.cfg.decision.database_path, clock=self.clock)
        self.store.policy_version = self.policy_version
        self.entry_edge_threshold = self.cfg.decision.min_expected_edge_pct
        self.threshold_source = "configuration" if self.entry_edge_threshold is not None else None
        self.evaluation_times = deque()
        self.lock = threading.RLock()
        self.run_id = None
        self.finished = False
        self.ending = False
        self.expect_running = False
        self.faulted = False
        self.closed = False
        self.portfolio_policy = "PAUSE_ENTRIES"
        self.last_portfolio = float("-inf")
        self.last_evaluated = {}
        self.cursor = 0
        self.quotes = {}
        self.radar = {}
        self.last_decision = None
        self.config_hash = digest(public_config(self.cfg))
        self.bot.scanner.feature_only = True
        self.environment = "paper_live_data"
        self.extra_metadata = {}

    def _ensure_run(self):
        if self.run_id is not None and not self.finished:
            return
        run_metadata = getattr(self.provider, "run_metadata", None)
        metadata = {"schema_version": SCHEMA_VERSION, "policy_version": self.policy_version,
                    "engine": self.engine, "code_sha256": code_digest(), "config_sha256": self.config_hash,
                    "configuration": public_config(self.cfg), "provider": self.provider.name,
                    "requested_model": self.provider.model, "environment": self.environment,
                    "fixture": bool(getattr(self.provider, "is_fixture", False)),
                    "execution": "paper_only", "fill_model": "ask_buy_bid_sell_with_configured_fees_slippage",
                    "confidence_is_profit_probability": False,
                    "outcome_attribution": "observational_not_causal",
                    "scheduling": "batched" if self._batched() else "sequential",
                    "entry_policy": self.cfg.decision.entry_policy, "exit_policy": self.cfg.decision.exit_policy,
                    "entry_min_expected_edge_pct": self.entry_edge_threshold, "entry_threshold_source": self.threshold_source,
                    **(clean(run_metadata()) if callable(run_metadata) else {}),
                    "starting_portfolio": self._portfolio(), **self.extra_metadata}
        self.run_id = self.store.create_run(metadata)
        self.finished = False
        self.ending = False
        self.last_portfolio = float("-inf")
        self.last_evaluated = {}
        self.portfolio_policy = "PAUSE_ENTRIES"
        for pos in self.bot.simulator.positions.values():
            self.store.link_position(self.run_id, [], pos)
            self.store.event(self.run_id, None, "inherited_position", {"position": pos, "original_entry_outcome_not_reconstructed": True})

    def prepare_start(self):
        with self.lock:
            if self.inference.busy:
                raise ValueError("Previous inference is still draining; wait before starting a new session.")
            if self.cfg.trading.mode != "simulation":
                raise ValueError("JEV execution is paper/replay only until live fills and commissions are reconciled; no live orders are enabled.")
            self.provider.check_ready()
            self._resolve_entry_threshold()
            if self.faulted:
                raise ValueError("Decision controller faulted; inspect journal and restart the process.")
            if self.finished and self.bot.simulator.positions:
                raise ValueError("Unclosed paper positions remain; close them with fresh data before a new bot session.")
            self._ensure_run()
            self.expect_running = True

    def _fresh(self, market):
        try:
            now = self.clock()
            values = [float(market[x]) for x in ("bid", "ask", "timestamp")]
            if not all(math.isfinite(v) for v in values): return False
            return (market.get("quote_valid") is True and 0 < values[0] <= values[1]
                    and 0 <= now - values[2] <= self.cfg.trading.max_market_data_age_seconds)
        except (KeyError, TypeError, ValueError):
            return False

    def _resolve_entry_threshold(self):
        """An expected-value entry needs a threshold chosen on held-out data or set explicitly."""
        if self.cfg.decision.entry_policy != "expected_value" or self.cfg.decision.min_expected_edge_pct is not None:
            return
        defaults = getattr(self.provider, "policy_defaults", dict)()
        value = defaults.get("entry_min_expected_edge_pct")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("expected_value entry policy needs decision.min_expected_edge_pct or a checkpoint "
                             "calibrated by tools/laya_evaluate.py --write-policy")
        self.entry_edge_threshold = float(value)
        self.threshold_source = "checkpoint_calibration_split"

    def _batched(self):
        return bool(self.cfg.decision.batch_inference and callable(getattr(self.provider, "evaluate_batch", None)))

    def _fetch(self, symbol, force=False):
        """Order book + candle refresh. Thread-safe per symbol; never touches the journal."""
        try:
            return self.bot.get_engine_for(symbol).update_market_state(force=force), None
        except Exception as exc:
            return None, type(exc).__name__

    def _prefetch(self, symbols):
        for symbol in symbols:
            self.bot.get_engine_for(symbol)  # engines are created on the controller thread
        workers = min(self.cfg.decision.market_fetch_workers, len(symbols))
        if workers <= 1 or self.environment == "historical_replay":
            return {symbol: self._fetch(symbol) for symbol in symbols}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-prefetch") as pool:
            return dict(zip(symbols, pool.map(self._fetch, symbols)))

    def _quote(self, symbol, *, force=False, prefetched=None):
        snap, error = prefetched if prefetched is not None else self._fetch(symbol, force)
        if error and self.run_id:
            self.store.event(self.run_id, None, "market_data_error", {"symbol": symbol, "error_type": error})
        market = dict(snap or {"symbol": symbol, "quote_valid": False, "features_ready": False,
                               "data_quality_reasons": ["missing_market_data"]})
        market["symbol"] = symbol
        market["quote_valid"] = self._fresh(market)
        if market.get("last_closed_candle_at") is not None:
            age = self.clock() - market["last_closed_candle_at"]
            if not 0 < age <= self.cfg.trading.max_candle_age_seconds:
                market["features_ready"] = False
                market["data_quality_reasons"] = list(market.get("data_quality_reasons", [])) + ["candle_age_at_decision"]
        elif market.get("features_ready"):
            market["features_ready"] = False
            market["data_quality_reasons"] = ["missing_candle_timestamp"]
        if not market["quote_valid"]:
            market["features_ready"] = False
        self.quotes[symbol] = market
        if self.run_id:
            self.store.observe(self.run_id, symbol=symbol, market=market, max_lateness=self.cfg.decision.outcome_max_lateness_seconds)
        return market

    def _portfolio(self):
        raw = self.bot.simulator.get_summary()
        return {k: copy.deepcopy(raw[k]) for k in (
            "initial_balance", "cash", "total_equity", "total_pnl", "total_pnl_pct", "realized_pnl", "unrealized_pnl",
            "estimated_exit_fees", "equity_basis", "open_positions_count", "open_positions", "total_closed_trades") if k in raw}

    def _all_marks_fresh(self):
        return all(self._fresh(self.quotes.get(p["symbol"], {})) for p in self.bot.simulator.positions.values())

    def _budget(self):
        return max(0.0, min(self.cfg.trading.budget_per_trade, self.bot.simulator.cash))

    def _entry_blocks(self, symbol, market, *, ignore_policy=False, manual=False):
        blocks = []
        if self.cfg.trading.mode != "simulation": blocks.append("live_fill_reconciliation_not_implemented")
        if self.faulted: blocks.append("journal_or_controller_fault")
        if self.expect_running and not self.bot.is_running and not manual: blocks.append("operator_or_session_stop")
        if self.ending and not manual: blocks.append("session_ending")
        if not self._fresh(market): blocks.append("stale_or_invalid_quote")
        if market.get("features_ready") is not True and not manual: blocks.append("features_not_ready")
        if not self._all_marks_fresh(): blocks.append("unpriced_existing_exposure")
        if (self.cfg.trading.auto_select_coin or self.cfg.trading.symbol == "AUTO") and not manual:
            radar = self.radar.get(symbol, {})
            try:
                radar_age = self.clock() - float(radar["updated_at"])
                source_time = radar.get("source_close_time_ms")
                source_age = self.clock() - float(source_time) / 1000 if source_time is not None else radar_age
                if not (0 <= radar_age <= self.cfg.trading.max_radar_age_seconds and 0 <= source_age <= self.cfg.trading.max_radar_age_seconds):
                    blocks.append("stale_radar_features")
            except (KeyError, TypeError, ValueError):
                blocks.append("missing_radar_features")
        if self._fresh(market):
            spread = (market["ask"] / market["bid"] - 1) * 100
            cap = self.cfg.trading.max_allowed_spread_pct
            if cap > 0 and spread > cap + 1e-10: blocks.append("spread_limit")
        if any(p["symbol"] == symbol for p in self.bot.simulator.positions.values()): blocks.append("duplicate_symbol_exposure")
        allowed, reason = self.bot.risk_manager.can_open_position(len(self.bot.simulator.positions), symbol=symbol)
        if not allowed: blocks.append("position_or_cooldown_limit: " + reason)
        if self._budget() < 10: blocks.append("insufficient_cash_or_budget")
        if not ignore_policy and self.portfolio_policy != "CONTINUE": blocks.append("jev_portfolio_pause")
        if self._all_marks_fresh() and self._portfolio()["total_pnl_pct"] <= -self.cfg.strategy.portfolio_stop_loss_pct:
            blocks.append("portfolio_hard_loss_limit")
        if self.bot.session_start_time is not None and self.bot.session_duration_seconds > 0:
            remaining = self.bot.session_start_time + self.bot.session_duration_seconds - self.clock()
            if remaining < self.cfg.decision.entry_blackout_seconds:
                blocks.append("session_entry_blackout")
        return blocks

    def _state(self, symbol=None, market=None, position=None, *, proposal=None):
        now = self.clock()
        state = {"as_of": now, "schema_version": SCHEMA_VERSION, "policy_version": self.policy_version,
                 "portfolio": self._portfolio(), "costs": {"fee_rate_pct": self.cfg.trading.fee_rate_pct,
                 "slippage_bps": self.cfg.decision.slippage_bps},
                 "session": {"start_time": self.bot.session_start_time,
                             "remaining_seconds": (max(0, self.bot.session_start_time + self.bot.session_duration_seconds - now)
                                                   if self.bot.session_start_time is not None and self.bot.session_duration_seconds > 0 else None)},
                 "btc_market": self.bot.scanner.tracker.get_micro_metrics("BTC_TRY"),
                 "portfolio_marks_fresh": self._all_marks_fresh()}
        if symbol is None:
            # Portfolio gets a compact aggregate, NOT a momentum-ranked shortlist.
            pairs = list(self.radar.values())
            state["market_overview"] = {"universe_size": len(pairs), "rising_count": sum(p.get("change_pct", 0) > 0 for p in pairs),
                                        "falling_count": sum(p.get("change_pct", 0) < 0 for p in pairs),
                                        "dump_flag_count": sum(bool(p.get("is_dumping")) for p in pairs),
                                        "source": "current_scanner_feature_universe", "symbols": sorted(self.radar)}
            state["guardrails"] = {"portfolio_loss_limit_pct": self.cfg.strategy.portfolio_stop_loss_pct,
                                   "max_open_positions": self.cfg.trading.max_open_positions, "live_execution_enabled": False}
            return clean(state)
        market = dict(market or self._quote(symbol))
        radar = copy.deepcopy(self.radar.get(symbol, {}))
        eng = self.bot.get_engine_for(symbol)
        candles = [r for r in getattr(eng, "closed_candle_rows", []) if r.get("close_time", float("inf")) < now]
        ticks = [list(r) for r in self.bot.scanner.tracker.history.get(symbol, []) if r[0] <= now]
        nc, nt = self.cfg.decision.history_candles, self.cfg.decision.history_ticks
        features = {**market, **{k: radar[k] for k in ("velocity_1m_pct", "quant_score", "volume_surge_ratio", "burst_ratio", "is_squeeze_breakout") if k in radar}}
        if "change_pct" in radar: features["change_24h_pct"] = radar["change_pct"]
        signal, reason = "HOLD", "not_evaluated_missing_features"
        if market.get("features_ready"):
            try:
                signal, reason = copy.deepcopy(self.bot.strategy).evaluate(copy.deepcopy(features), len(self.bot.simulator.positions))
            except Exception as exc:
                reason = "legacy_advisory_error:" + type(exc).__name__
        legacy_exit = {"would_exit": False, "reason": "no_position"}
        risk = {"configured_thresholds": asdict(self.cfg.strategy), "advisory_only": True}
        if position:
            poscopy = copy.deepcopy(position)
            if self._fresh(market):
                close, why, pnl = self.bot.risk_manager.evaluate_exit(poscopy, market["bid"])
                legacy_exit = {"would_exit": close, "reason": why, "reference_gross_pnl_pct": pnl,
                               "reference_state": poscopy}
                risk.update({"holding_seconds": now - position["entry_time"],
                             "absolute_entry_price_change_pct": (market["bid"] / position["entry_price"] - 1) * 100,
                             "net_liquidation_pnl_pct": position.get("unrealized_pnl_pct"),
                             "absolute_loss_limit_pct": self.cfg.strategy.stop_loss_pct})
        blocks = self._entry_blocks(symbol, market)
        state.update({"market": features, "radar": radar, "position": copy.deepcopy(position),
                      "history": {"closed_candles": candles[-nc:] if nc else [], "micro_ticks": ticks[-nt:] if nt else [],
                                  "available_closed_candles": len(candles), "available_micro_ticks": len(ticks),
                                  "tick_fields": ["timestamp", "price", "rolling_24h_volume_try"], "history_window_is_bounded": True},
                      "legacy_advisory": {"strategy": self.cfg.strategy.active, "signal": signal, "reason": reason,
                                          "exit": legacy_exit, "never_an_execution_gate": True},
                      "risk_reference": risk,
                      "guardrails": {"entry_block_reasons": blocks, "maximum_entry_budget_try": self._budget(),
                                     "max_open_positions": self.cfg.trading.max_open_positions,
                                     "partial_sell_fraction": self.cfg.strategy.partial_tp_ratio,
                                     "portfolio_policy": self.portfolio_policy,
                                     "max_spread_pct": self.cfg.trading.max_allowed_spread_pct,
                                     "hard_boundaries_are_not_model_overridable": True},
                      "reference_filters": {"only_uptrend": self.cfg.trading.only_uptrend,
                                            "min_24h_gain_pct": self.cfg.trading.min_24h_gain_pct,
                                            "min_24h_volume_try": self.cfg.trading.min_24h_volume_try,
                                            "min_coin_price": self.cfg.trading.min_coin_price,
                                            "min_observation_gain_pct": self.cfg.trading.min_observation_gain_pct,
                                            "observation_seconds": self.cfg.trading.candidate_observation_seconds,
                                            "prebuy_seconds": self.cfg.trading.candidate_prebuy_seconds,
                                            "btc_dump_threshold_pct": self.cfg.trading.btc_dump_shield_pct,
                                            "advisory_only": True}})
        # Quantitative comparisons are evaluated here, not delegated to numeric reasoning.
        state["computed_facts"] = {"rsi_zone": ("oversold" if market.get("rsi", 50) <= self.cfg.strategy.rsi_oversold else
                                                "overbought" if market.get("rsi", 50) >= self.cfg.strategy.rsi_overbought else "middle"),
                                   "ema_fast_above_slow": market.get("ema_fast", 0) > market.get("ema_slow", 0),
                                   "features_usable": market.get("features_ready") is True,
                                   "quote_usable": self._fresh(market),
                                   "volume_surge_is_proxy": radar.get("volume_surge_is_proxy", True)}
        if proposal is not None: state["proposal"] = copy.deepcopy(proposal)
        return clean(state)

    def _decision_block(self, decision):
        """Recheck immediately before model side effects; safety/operator exits bypass this."""
        if self.finished or self.ending or self.closed or self.faulted:
            return "session_ending"
        if decision.get("run_id", self.run_id) != self.run_id:
            return "decision_session_changed"
        if self.expect_running and not self.bot.is_running:
            return "operator_or_session_stop"
        age = self.clock() - decision["as_of"]
        if (age < 0 or age >= self.cfg.decision.max_decision_age_seconds
                or time.monotonic() >= decision.get("deadline", float("inf"))):
            return "decision_expired"
        return None

    def _infer(self, state, questions, decision, evaluation):
        task = self.inference.submit(state, questions, decision["deadline"])
        return self._await(task, [decision], evaluation, position=state.get("position"))

    def _infer_many(self, requests, decisions):
        task = self.inference.submit_batch(requests, min(d["deadline"] for d in decisions))
        return self._await(task, decisions, 1)

    def _await(self, task, decisions, evaluation, *, position=None):
        next_safety = time.monotonic() + .25
        # The batch is cancelled as soon as its most urgent decision can no longer be applied.
        urgent = min(decisions, key=lambda d: d["deadline"])
        extra = {"batch_size": len(decisions)} if len(decisions) > 1 else {}

        def drain_attempts():
            while True:
                try:
                    attempt = task.attempts.get_nowait()
                except queue.Empty:
                    break
                for decision in decisions:
                    self.store.event(decision["run_id"], decision["id"], "inference_attempt",
                                     {**attempt, **extra, "evaluation": evaluation})

        try:
            while not task.done.wait(.05):
                drain_attempts()
                block = self._decision_block(urgent)
                if block or task.cancelled.is_set():
                    raise ProviderError(block or "inference_cancelled")
                if time.monotonic() >= next_safety:
                    # Provider worker only owns immutable request data. All risk,
                    # execution and journal operations stay on this controller thread.
                    if not self._safety(force=True):
                        raise ProviderError("hard_safety_override")
                    if position and position["position_id"] not in self.bot.simulator.positions:
                        raise ProviderError("position_closed_by_safety")
                    next_safety = time.monotonic() + .25
            drain_attempts()
            if task.cancelled.is_set():
                raise ProviderError("inference_cancelled")
            if task.error is not None:
                raise task.error
            return task.response
        finally:
            if not task.done.is_set():
                task.cancelled.set()
                for decision in decisions:
                    self.store.event(decision["run_id"], decision["id"], "inference_abandoned",
                                     {"late_response_will_be_discarded": True, "evaluation": evaluation, **extra})

    def _new_decision(self, stage, state, questions, *, parent_id=None):
        source = "fixture" if getattr(self.provider, "is_fixture", False) else "model"
        did = self.store.begin_decision(self.run_id, stage, state, questions, self.provider.model,
                                        source=source, parent_id=parent_id, horizons=self.cfg.decision.outcome_horizons_seconds)
        started = time.monotonic()
        return {"id": did, "run_id": self.run_id, "as_of": state["as_of"], "state": state,
                "valid": False, "response": None, "error": None,
                "deadline": started + max(0, self.cfg.decision.max_decision_age_seconds
                                          - max(0, self.clock() - state["as_of"]))}

    def _record_response(self, decision, questions, response, evaluation, started, extra=None):
        validation_error = None
        try:
            decision["response"] = validate_response(response, questions)
            decision["valid"] = True
            decision["error"] = None
        except (ValueError, TypeError, KeyError) as exc:
            validation_error = str(exc)
            decision["error"] = "invalid_response_schema"
        self.store.event(self.run_id, decision["id"], "model_response", {"raw": clean(response), "valid": decision["valid"],
                         "validation_error": validation_error, "provider": self.provider.name,
                         "requested_model": self.provider.model, "evaluation": evaluation,
                         "latency_ms": round((time.monotonic() - started) * 1000, 3), **(extra or {})})
        return validation_error

    def _decide_batch(self, prepared):
        """Journal every request first, then ONE provider call for all of them."""
        decisions = [self._new_decision(p["stage"], p["state"], p["questions"]) for p in prepared]
        live = []
        for decision, p in zip(decisions, prepared):
            block = self._decision_block(decision)
            if block:
                decision["error"] = block
                self.store.event(self.run_id, decision["id"], "inference_failed", {"error_code": block, "fallback": "WAIT_OR_HOLD"})
            else:
                live.append((decision, p["questions"]))
        if live:
            started = time.monotonic()
            try:
                responses = self._infer_many([(d["state"], q) for d, q in live], [d for d, _ in live])
            except (ProviderError, ValueError) as exc:
                responses = [exc] * len(live)
            for (decision, questions), response in zip(live, responses):
                if isinstance(response, Exception):
                    decision["error"] = getattr(response, "code", type(response).__name__)
                    self.store.event(self.run_id, decision["id"], "inference_failed", {"error_code": decision["error"], "fallback": "WAIT_OR_HOLD"})
                    continue
                self._record_response(decision, questions, response, 1, started, {"batch_size": len(live)})
        if decisions:
            last, p = decisions[-1], prepared[-1]
            self.last_decision = {"id": last["id"], "stage": p["stage"], "symbol": p["symbol"],
                                  "valid": last["valid"], "error": last["error"], "batch_size": len(decisions)}
        return decisions

    def _decide(self, stage, state, questions, *, parent_id=None):
        result = self._new_decision(stage, state, questions, parent_id=parent_id)
        did = result["id"]
        started = time.monotonic()
        for evaluation in range(2):
            try:
                block = self._decision_block(result)
                if block:
                    raise ProviderError(block)
                response = self._infer(state, questions, result, evaluation + 1)
            except (ProviderError, ValueError) as exc:
                # Transport retries remain bounded by the provider. Only a
                # received but invalid structured answer gets one extra query.
                result["error"] = getattr(exc, "code", type(exc).__name__)
                self.store.event(self.run_id, did, "inference_failed", {"error_code": result["error"], "fallback": "WAIT_OR_HOLD"})
                break
            validation_error = self._record_response(result, questions, response, evaluation + 1, started)
            # A deterministic local model would return the identical answer again.
            if result["valid"] or evaluation == 1 or getattr(self.provider, "deterministic", False):
                break
            if self._decision_block(result):
                break
            self.store.event(self.run_id, did, "decision_retry", {"reason": validation_error, "next_evaluation": 2,
                             "original_as_of_preserved": True})
        self.last_decision = {"id": did, "stage": stage, "symbol": state.get("market", {}).get("symbol"),
                              "valid": result["valid"], "error": result["error"]}
        return result

    def _answer(self, decision, key, default):
        if not decision["valid"]:
            return default, "inference_failed_or_invalid"
        block = self._decision_block(decision)
        if block:
            return default, block
        answer = decision["response"]["answers"][key]
        if answer["confidence"] < self.cfg.decision.min_action_confidence:
            return default, "low_confidence:" + key
        return answer["choice"], None

    def _disposition(self, decision, action, *, reasons=(), status="applied", related=None, answer_usage=None):
        self.store.event(self.run_id, decision["id"], "decision_disposition", {
            "applied_action": action, "status": status, "override_reasons": list(reasons),
            "related_decision_id": related, "answer_usage": answer_usage or {},
            "applied_action_is_not_necessarily_model_choice": True})

    def _system(self, stage, symbol=None, position=None, action="WAIT", reason="", *, source="safety"):
        state = self._state(symbol, self.quotes.get(symbol) if symbol else None, position)
        did = self.store.begin_decision(self.run_id, stage, state, {}, "deterministic", source=source,
                                        horizons=self.cfg.decision.outcome_horizons_seconds)
        decision = {"id": did, "as_of": state["as_of"], "state": state, "valid": False}
        self.store.event(self.run_id, did, "non_model_instruction", {"source": source, "action": action, "reason": reason})
        return decision

    def _sell(self, position_id, decision, *, fraction=1.0, reason="JEV", source="model"):
        if source == "model" and getattr(self.provider, "is_fixture", False): source = "fixture"
        pos = self.bot.simulator.positions.get(position_id)
        if not pos: return None
        symbol = pos["symbol"]
        market = self._quote(symbol, force=True)
        if self.cfg.trading.mode != "simulation" or not self._fresh(market):
            self._disposition(decision, "HOLD", status="blocked", reasons=["live_execution_disabled_or_no_fresh_bid"])
            return None
        if source in ("model", "fixture"):
            block = self._decision_block(decision)
            if block:
                self._disposition(decision, "HOLD", status="blocked", reasons=[block])
                return None
        self.store.link_position(self.run_id, [decision["id"]], pos)
        price = market["bid"] * (1 - self.cfg.decision.slippage_bps / 10000)
        self.store.event(self.run_id, decision["id"], "execution_intent", {
            "side": "SELL", "position_id": position_id, "fraction": fraction, "price": price,
            "quote": market, "source": source, "fill_source": "paper_quote_model"})
        trade = self.bot.simulator.sell(position_id, price, fraction=fraction, reason=reason)
        if trade:
            trade.update(decision_id=decision["id"], decision_source=source, run_id=self.run_id)
            self.store.event(self.run_id, decision["id"], "execution_result", {"status": "filled", "fill": trade, "fill_source": "paper_quote_model"})
            remaining = self.bot.simulator.positions.get(position_id, {}).get("quantity", 0.0)
            self.store.position_fill(self.run_id, position_id, trade, remaining)
            if remaining == 0:
                self.bot.risk_manager.record_trade_exit(symbol, is_loss=trade["net_pnl"] < 0)
            self.bot.log(f"{self.label} kayıtlı {source} satış [{symbol}]: {trade['net_pnl']:+.2f} TL | karar={decision['id']}")
        else:
            self.store.event(self.run_id, decision["id"], "execution_result", {"status": "rejected", "reason": "paper_engine_rejected"})
        return trade

    def _buy(self, symbol, budget, decision, *, parent_id=None, manual=False):
        market = self._quote(symbol, force=True)
        blocks = self._entry_blocks(symbol, market, ignore_policy=manual, manual=manual)
        if not manual:
            block = self._decision_block(decision)
            if block: blocks.append(block)
        reference = decision["state"].get("proposal", {}).get("reference_ask")
        if reference and self._fresh(market) and abs(market["ask"] / reference - 1) * 100 > self.cfg.decision.max_prebuy_price_move_pct:
            blocks.append("prebuy_price_move_limit")
        if not math.isfinite(budget) or budget < 10 or budget > self._budget() + 1e-8:
            blocks.append("invalid_or_excessive_budget")
        if blocks:
            self._disposition(decision, "WAIT", status="blocked", reasons=blocks)
            return None
        price = market["ask"] * (1 + self.cfg.decision.slippage_bps / 10000)
        self.store.event(self.run_id, decision["id"], "execution_intent", {"side": "BUY", "symbol": symbol,
                         "budget_try": budget, "price": price, "quote": market, "fill_source": "paper_quote_model"})
        pos = self.bot.simulator.buy(symbol, price, budget, reason=f"{'OPERATOR' if manual else self.label.upper()} decision={decision['id']}")
        if pos:
            self.bot.simulator.all_orders[-1].update(decision_id=decision["id"], run_id=self.run_id, decision_source="operator" if manual else ("fixture" if getattr(self.provider, "is_fixture", False) else "model"))
            pos.update(entry_decision_id=parent_id or decision["id"], entry_execution_decision_id=decision["id"], decision_run_id=self.run_id)
            self.bot.simulator.update_market_price(symbol, market["bid"] * (1 - self.cfg.decision.slippage_bps / 10000))
            self.bot.risk_manager.record_trade_entry(symbol)
            self.store.link_position(self.run_id, [decision["id"], parent_id], pos)
            self.store.event(self.run_id, decision["id"], "execution_result", {"status": "filled", "fill": copy.deepcopy(self.bot.simulator.all_orders[-1]),
                             "position_id": pos["position_id"], "fill_source": "paper_quote_model"})
            if parent_id:
                self.store.event(self.run_id, parent_id, "execution_result", {"status": "filled", "child_decision_id": decision["id"],
                                 "position_id": pos["position_id"], "fill": copy.deepcopy(self.bot.simulator.all_orders[-1]),
                                 "fill_source": "paper_quote_model", "attribution": "prebuy_child_execution"})
            self.bot.log(f"{self.label} kayıtlı alış [{symbol}]: {budget:.2f} TL | karar={decision['id']}")
        else:
            self.store.event(self.run_id, decision["id"], "execution_result", {"status": "rejected", "reason": "paper_engine_rejected"})
        return pos

    def _safety(self, *, force=False):
        """No inference latency before hard loss checks. Original cost is never reset by ROLLOVER."""
        for pos in list(self.bot.simulator.positions.values()):
            market = self._quote(pos["symbol"], force=force)
            if not self._fresh(market): continue
            mark = market["bid"] * (1 - self.cfg.decision.slippage_bps / 10000)
            self.bot.simulator.update_market_price(pos["symbol"], mark)
            if "risk_reference_price" in pos:
                pos["risk_highest_price"] = max(pos.get("risk_highest_price", pos["risk_reference_price"]), market["bid"])
            # Net liquidation loss, not a resettable trend-reference return.
            if pos["unrealized_pnl_pct"] <= -self.cfg.strategy.stop_loss_pct:
                dec = self._system("hard_position_stop", pos["symbol"], pos, "SELL", "absolute_net_loss_limit")
                self._sell(pos["position_id"], dec, reason="HARD_STOP_LOSS", source="safety")
        fresh = self._all_marks_fresh()
        summary = self._portfolio()
        if fresh:
            self.store.observe(self.run_id, equity=summary["total_equity"], max_lateness=self.cfg.decision.outcome_max_lateness_seconds)
        if fresh and summary["total_pnl_pct"] <= -self.cfg.strategy.portfolio_stop_loss_pct:
            self.store.event(self.run_id, None, "hard_portfolio_stop", {"portfolio": summary})
            self.portfolio_policy = "PAUSE_ENTRIES"
            for pos in list(self.bot.simulator.positions.values()):
                dec = self._system("hard_portfolio_stop", pos["symbol"], pos, "SELL", "portfolio_net_loss_limit")
                self._sell(pos["position_id"], dec, reason="HARD_PORTFOLIO_STOP", source="safety")
            self.bot.is_running = False
            self.bot.stop()
            return False
        return True

    def _scan(self):
        auto = self.cfg.trading.auto_select_coin or self.cfg.trading.symbol == "AUTO"
        if auto:
            self.bot.scanner.feature_only = True
            pairs = self.bot.scanner.scan_top_active_pairs(limit=0, only_uptrend=False, min_gain_pct=0)
            self.radar = {p["symbol"]: copy.deepcopy(p) for p in pairs}
        else:
            symbol = self.cfg.trading.symbol
            self.radar = {symbol: {"symbol": symbol, "source": "single_symbol_no_24h_radar", "updated_at": self.clock()}}
        return sorted(self.radar)

    def _prepare_symbol(self, symbol, *, prefetched=None):
        now = self.clock()
        self.last_evaluated[symbol] = now
        market = self._quote(symbol, prefetched=prefetched)
        # Single-symbol mode still records actual micro-ticks instead of all-zero momentum.
        if self._fresh(market) and self.radar.get(symbol, {}).get("source") == "single_symbol_no_24h_radar":
            self.bot.scanner.tracker.record_tick(symbol, market["bid"], timestamp=self.clock())
            self.radar[symbol].update(self.bot.scanner.tracker.get_micro_metrics(symbol))
        pos = next((p for p in self.bot.simulator.positions.values() if p["symbol"] == symbol), None)
        state = self._state(symbol, market, pos)
        self.bot.latest_snapshot = state["market"]
        questions = symbol_questions(pos is not None, self.cfg.decision.allocation_fractions, forecast=self.forecast)
        return {"symbol": symbol, "market": market, "pos": pos, "state": state, "questions": questions,
                "stage": "position" if pos else "candidate"}

    def _evaluate_symbol(self, symbol):
        prepared = self._prepare_symbol(symbol)
        dec = self._decide(prepared["stage"], prepared["state"], prepared["questions"])
        self._apply_symbol_decision(prepared, dec)

    def _uses_edge(self, position):
        d = self.cfg.decision
        return d.exit_policy == "expected_value" if position else d.entry_policy == "expected_value"

    def _edge(self, decision, *, side):
        """(edge_pct, failure_reason, detail). Entry: expected net result of buying at the ask and
        selling at the bid 5 minutes later. Hold: expected mid change of keeping a position."""
        if not decision["valid"]:
            return None, "inference_failed_or_invalid", {}
        block = self._decision_block(decision)
        if block:
            return None, block, {}
        answer = decision["response"]["answers"].get(FORECAST_ID) or {}
        expected = answer.get("expected_mid_return_pct")
        detail = {"side": side, "expected_mid_return_pct": expected, "forecast_probabilities": answer.get("probabilities"),
                  "forecast_trained_examples": answer.get("trained_examples")}
        if side == "hold":
            if not isinstance(expected, (int, float)) or isinstance(expected, bool) or not math.isfinite(expected):
                return None, "forecast_unavailable", detail
            return expected, None, {**detail, "edge_pct": expected}
        edge, costs = entry_edge_pct(expected, decision["state"].get("market"), self.cfg.trading.fee_rate_pct,
                                     self.cfg.decision.slippage_bps)
        detail.update(costs=costs, edge_pct=edge)
        if edge is None:
            return None, "forecast_or_quote_unavailable", detail
        return edge, None, detail

    def _policy_action(self, decision, position):
        """The executable action and, on failure, the fallback reason. Guardrails apply afterwards."""
        default = "HOLD" if position else "WAIT"
        if not self._uses_edge(position):
            action, override = self._answer(decision, "action", default)
            return action, override
        side = "hold" if position else "entry"
        edge, reason, detail = self._edge(decision, side=side)
        if reason:
            return default, reason
        if position:
            threshold = -self.cfg.decision.min_exit_edge_pct
            action = "SELL" if edge <= threshold else "HOLD"
        else:
            threshold = self.entry_edge_threshold
            action = "BUY" if edge >= threshold else "WAIT"
        self.store.event(self.run_id, decision["id"], "expected_value_assessment",
                         {**detail, "threshold_pct": threshold, "threshold_source": self.threshold_source if not position else "configuration",
                          "action": action, "model_action_answer_is_diagnostic": True})
        return action, None

    def _apply_symbol_decision(self, prepared, dec):
        symbol, pos, state, questions, market = (prepared[k] for k in ("symbol", "pos", "state", "questions", "market"))
        default = "HOLD" if pos else "WAIT"
        action, override = self._policy_action(dec, pos)
        usage = {name: "diagnostic_recorded_not_execution_gate" for name in questions}
        if self._uses_edge(pos):
            usage[FORECAST_ID] = "expected_value_gate_subject_to_guardrails"
        else:
            usage["action"] = "execution_action_subject_to_guardrails"
        if self.finished or not self._safety():
            self._disposition(dec, default, status="superseded", reasons=["hard_safety_override"], answer_usage=usage)
            return
        if pos and pos["position_id"] not in self.bot.simulator.positions:
            self._disposition(dec, "HOLD", status="superseded", reasons=["position_closed_by_safety"], answer_usage=usage)
            return
        if override:
            self._disposition(dec, default, status="fallback", reasons=[override], answer_usage=usage)
            return
        if action in ("SELL", "SELL_PARTIAL"):
            fraction = self.cfg.strategy.partial_tp_ratio if action == "SELL_PARTIAL" else 1.0
            filled = self._sell(pos["position_id"], dec, fraction=fraction, reason=f"{self.label.upper()}_{action}")
            self._disposition(dec, action if filled else "HOLD", status="filled" if filled else "blocked", answer_usage=usage)
        elif action == "ROLLOVER":
            fresh = self._quote(symbol, force=True)
            if self._fresh(fresh):
                self.bot.simulator.update_market_price(symbol, fresh["bid"] * (1 - self.cfg.decision.slippage_bps / 10000))
            block = self._decision_block(dec)
            if block:
                self._disposition(dec, "HOLD", status="blocked", reasons=[block], answer_usage=usage)
            elif self._fresh(fresh) and pos.get("unrealized_pnl_pct", 0) > 0:
                self.bot._rollover_reference(pos, fresh["bid"])
                self.store.event(self.run_id, dec["id"], "risk_reference_reset", {"risk_reference_price": fresh["bid"], "original_entry_price": pos["entry_price"]})
                self._disposition(dec, "ROLLOVER", answer_usage=usage)
            else:
                self._disposition(dec, "HOLD", status="blocked", reasons=["rollover_requires_fresh_quote_and_net_profit"], answer_usage=usage)
        elif action == "BUY":
            if self._uses_edge(None):
                # The imitation allocation answer is diagnostic here; the smallest size is used.
                allocation, error = min(self.cfg.decision.allocation_fractions, key=self.cfg.decision.allocation_fractions.get), None
                usage["allocation"] = "diagnostic_expected_value_policy_uses_smallest_allocation"
            else:
                allocation, error = self._answer(dec, "allocation", None)
                usage["allocation"] = "bounded_budget_multiplier"
            allocation_fallback = error == "low_confidence:allocation" and self.cfg.trading.mode == "simulation"
            if allocation_fallback:
                allocation = min(self.cfg.decision.allocation_fractions, key=self.cfg.decision.allocation_fractions.get)
                error = None
                usage["allocation"] = "low_confidence_smallest_allocation_capped_500_try"
            if error or allocation is None:
                self._disposition(dec, "WAIT", status="blocked", reasons=[error or "missing_allocation"], answer_usage=usage)
                return
            budget = state["guardrails"]["maximum_entry_budget_try"] * self.cfg.decision.allocation_fractions[allocation]
            if allocation_fallback:
                budget = min(500.0, budget, self._budget())
                self.store.event(self.run_id, dec["id"], "allocation_fallback", {
                    "reason": "low_confidence:allocation", "model_answer": dec["response"]["answers"]["allocation"],
                    "selected_allocation": allocation, "budget_try": budget,
                    "requires_fresh_prebuy_authorization": True})
                self.bot.log(f"{self.label} [{symbol}]: tutar güveni düşük; {budget:.2f} TL sanal alım teyidine gönderiliyor.")
            if self.cfg.trading.auto_select_coin or self.cfg.trading.symbol == "AUTO":
                refreshed_pairs = self.bot.scanner.scan_top_active_pairs(limit=0, force_refresh=True, only_uptrend=False, min_gain_pct=0)
                fresh_radar = {p["symbol"]: copy.deepcopy(p) for p in refreshed_pairs}
                self.radar.pop(symbol, None)
                if symbol in fresh_radar: self.radar[symbol] = fresh_radar[symbol]
            refreshed = self._quote(symbol, force=True)
            proposal = {"decision_id": dec["id"], "as_of": dec["as_of"], "action": "BUY", "allocation": allocation,
                        "budget_try": budget, "allocation_fallback": allocation_fallback,
                        "reference_ask": market.get("ask"), "prior_answers": dec["response"]["answers"]}
            prestate = self._state(symbol, refreshed, proposal=proposal)
            prebuy = self._decide("prebuy", prestate, prebuy_questions(forecast=self.forecast), parent_id=dec["id"])
            if self._uses_edge(None):
                # Fresh-data recheck of the same expected-value rule, not the imitation answer.
                edge_action, gate_error = self._policy_action(prebuy, None)
                authorization = "EXECUTE" if edge_action == "BUY" and gate_error is None else "WAIT"
            else:
                authorization, gate_error = self._answer(prebuy, "prebuy_authorization", "WAIT")
            self._disposition(dec, "BUY_PROPOSED", status="delegated_to_prebuy", related=prebuy["id"], answer_usage=usage)
            if authorization == "EXECUTE" and gate_error is None:
                if not self._safety() or self.finished:
                    self._disposition(prebuy, "WAIT", status="superseded", reasons=["hard_safety_override"])
                    return
                filled = self._buy(symbol, budget, prebuy, parent_id=dec["id"])
                self._disposition(prebuy, "BUY" if filled else "WAIT", status="filled" if filled else "blocked")
            else:
                status = "fallback" if gate_error else "applied"
                if gate_error in ("operator_or_session_stop", "session_ending", "decision_session_changed", "decision_expired"):
                    status = "blocked"
                self._disposition(prebuy, "WAIT" if authorization != "CANCEL" else "CANCEL", status=status,
                                  reasons=[gate_error] if gate_error else [])
        else:
            self._disposition(dec, action, answer_usage=usage)

    def _rate_limited(self, due, held):
        """Bound exchange requests per minute; open positions are never deferred."""
        cap = self.cfg.decision.max_symbol_evaluations_per_minute
        if not cap:
            return due, []
        now = self.clock()
        while self.evaluation_times and now - self.evaluation_times[0] > 60:
            self.evaluation_times.popleft()
        positions = [s for s in due if s in held]
        room = max(0, cap - len(self.evaluation_times) - len(positions))
        others = [s for s in due if s not in held]
        return positions + others[:room], others[room:]

    def _conviction(self, prepared, decision):
        """Ranking key for entries within one batch: expected edge, else P(BUY)."""
        if prepared["pos"] is not None or not decision["valid"]:
            return float("-inf")
        if self._uses_edge(None):
            edge, _, _ = self._edge(decision, side="entry")
            return edge if edge is not None else float("-inf")
        return decision["response"]["answers"]["action"]["probabilities"].get("BUY", 0.0)

    def _step_batched(self, held, selected, interval):
        """Everything a local model makes affordable: every due symbol in one forward pass,
        positions re-evaluated on their own short interval, entries executed best-first."""
        now = self.clock()
        position_interval = self.cfg.decision.position_decision_interval_seconds or interval
        due = [s for s in held if now - self.last_evaluated.get(s, float("-inf")) >= position_interval]
        due += [s for s in selected if now - self.last_evaluated.get(s, float("-inf")) >= interval]
        due, deferred = self._rate_limited(due, set(held))
        if deferred:
            self.store.event(self.run_id, None, "candidate_scheduling", {"method": "exchange_request_budget", "deferred_symbols": deferred,
                             "deferred_is_not_model_rejection": True})
        if not due:
            return
        fetched = self._prefetch(due)
        prepared = []
        for symbol in due:
            if self.finished or (self.expect_running and not self.bot.is_running):
                return
            prepared.append(self._prepare_symbol(symbol, prefetched=fetched[symbol]))
            self.evaluation_times.append(self.clock())
        if not self._safety():
            return
        decisions = self._decide_batch(prepared)
        # Exits first, then entries by descending conviction so scarce slots and cash go to the
        # strongest setups instead of whichever symbol came first alphabetically.
        scores = [self._conviction(p, d) for p, d in zip(prepared, decisions)]
        order = sorted(range(len(prepared)), key=lambda i: (prepared[i]["pos"] is None, -scores[i]))
        ranking = [{"symbol": prepared[i]["symbol"], "decision_id": decisions[i]["id"],
                    "score": scores[i] if math.isfinite(scores[i]) else None} for i in order if prepared[i]["pos"] is None]
        if ranking:
            self.store.event(self.run_id, None, "entry_ranking", {"key": "expected_edge_pct" if self._uses_edge(None) else "buy_probability",
                             "ranking": ranking})
        for i in order:
            if self.finished or (self.expect_running and not self.bot.is_running):
                self._disposition(decisions[i], "HOLD" if prepared[i]["pos"] else "WAIT", status="superseded", reasons=["session_ending"])
                continue
            self._apply_symbol_decision(prepared[i], decisions[i])

    def step(self):
        with self.lock:
            if self.faulted: return
            if self.cfg.trading.mode != "simulation":
                raise ValueError("JEV live execution disabled: verified exchange fills are not implemented")
            if self.expect_running and not self.bot.is_running: return
            self._ensure_run()
            if digest(public_config(self.bot.config)) != self.config_hash:
                self.store.event(self.run_id, None, "configuration_changed_mid_run", {"action": "halt"})
                self.bot.is_running = False
                self.bot.stop_completed = True
                self.faulted = True
                self.end_session(reason="configuration_changed", liquidate=False)
                raise ValueError("Configuration changed during a JEV run; stop and start a new versioned session")
            try:
                self.bot.step_count += 1
                if not self._safety(): return
                universe = self._scan()
                now = self.clock()
                interval = self.cfg.decision.decision_interval_seconds
                if now - self.last_portfolio >= interval:
                    pdec = self._decide("portfolio", self._state(), portfolio_questions())
                    if self.finished or not self._safety(): return
                    policy, error = self._answer(pdec, "portfolio_action", "PAUSE_ENTRIES")
                    if error and error.startswith("low_confidence") and self.cfg.decision.portfolio_low_confidence_policy == "CONTINUE":
                        # Explicit, journaled configuration: an unsure portfolio answer does not pause
                        # entries; every entry still needs its own gate and the hard portfolio stop.
                        policy = "CONTINUE"
                    self.portfolio_policy = policy
                    self.last_portfolio = self.clock()
                    self._disposition(pdec, policy, status="fallback" if error else "applied", reasons=[error] if error else [])
                    if policy == "FLATTEN":
                        for pos in list(self.bot.simulator.positions.values()):
                            self._sell(pos["position_id"], pdec, reason=f"{self.label.upper()}_PORTFOLIO_FLATTEN")
                # Positions get priority for monitoring, never a quant-score ranking of entry candidates.
                held = sorted({p["symbol"] for p in self.bot.simulator.positions.values()})
                candidates = [s for s in universe if s not in held]
                selected = []
                if candidates:
                    start = self.cursor % len(candidates)
                    rotated = candidates[start:] + candidates[:start]
                    selected = rotated[:self.cfg.decision.max_candidates_per_step]
                    self.cursor = (start + len(selected)) % len(candidates)
                if len(selected) < len(candidates):
                    self.store.event(self.run_id, None, "candidate_scheduling", {"method": "alphabetical_round_robin", "evaluated_batch": selected,
                                     "deferred_symbols": [s for s in candidates if s not in selected], "deferred_is_not_model_rejection": True})
                if self._batched():
                    self._step_batched(held, selected, interval)
                else:
                    for symbol in held + selected:
                        if self.finished or (self.expect_running and not self.bot.is_running): break
                        if not self._safety(): break
                        if self.clock() - self.last_evaluated.get(symbol, float("-inf")) < interval: continue
                        self._evaluate_symbol(symbol)
                # WAIT/SKIP outcomes must be observed even after a symbol leaves a ranked list.
                rows = self.store.db.execute("SELECT DISTINCT d.symbol FROM decisions d JOIN outcomes o USING(decision_id) "
                                             "WHERE d.run_id=? AND d.symbol IS NOT NULL AND o.status='pending' AND o.due_at<=?",
                                             (self.run_id, self.clock())).fetchall()
                if not self.finished:
                    for row in rows: self._quote(row["symbol"])
                    if self._all_marks_fresh():
                        self.store.observe(self.run_id, equity=self._portfolio()["total_equity"], max_lateness=self.cfg.decision.outcome_max_lateness_seconds)
                self.bot.current_status_text = f"{self.label} | {self.portfolio_policy} | {len(self.bot.simulator.positions)} pozisyon"
            except Exception as exc:
                self.faulted = True
                self.bot.is_running = False
                self.bot.stop_completed = True
                self.bot.current_status_text = f"{self.label} HALTED: {type(exc).__name__} — inspect journal"
                self.bot.log(self.bot.current_status_text)
                try: self.store.event(self.run_id, None, "controller_fault", {"error_type": type(exc).__name__, "automatic_execution_halted": True})
                except Exception: pass
                try: self.end_session(reason="controller_fault", liquidate=False)
                except Exception: pass  # A broken journal cannot authorize any further execution.
                raise

    def manual_buy(self, symbol, budget, reason):
        with self.lock:
            self._ensure_run()
            if not symbol or symbol == "AUTO":
                self.store.event(self.run_id, None, "operator_instruction_blocked", {"reason": "explicit_symbol_required_no_legacy_radar_selection"})
                return None
            market = self._quote(symbol, force=True)
            dec = self._system("operator_buy", symbol, action="BUY", reason=reason, source="operator")
            return self._buy(symbol, self._budget() if budget is None else budget, dec, manual=True)

    def manual_close(self, position_id, reason):
        with self.lock:
            self._ensure_run()
            pos = self.bot.simulator.positions.get(position_id)
            if not pos: return None
            self._quote(pos["symbol"], force=True)
            dec = self._system("operator_close", pos["symbol"], pos, "SELL", reason, source="operator")
            return self._sell(position_id, dec, reason=reason, source="operator")

    def manual_close_all(self, reason):
        with self.lock:
            return [trade for pos in list(self.bot.simulator.positions.values()) if (trade := self.manual_close(pos["position_id"], reason))]

    def end_session(self, *, reason="session_end", liquidate=True):
        self.inference.cancel()
        with self.lock:
            if self.run_id is None or self.finished or self.ending: return
            self.ending = True
            if liquidate:
                for pos in list(self.bot.simulator.positions.values()):
                    self._quote(pos["symbol"], force=True)
                    dec = self._system("session_close", pos["symbol"], pos, "SELL", reason, source="session")
                    self._sell(pos["position_id"], dec, reason=reason, source="session")
            self.store.end_run(self.run_id, reason, list(self.bot.simulator.positions.values()))
            self.finished = True
            self.ending = False

    def status(self):
        return {"engine": self.engine, "provider": self.provider.name, "model": self.provider.model, "run_id": self.run_id,
                "scheduling": "batched" if self._batched() else "sequential", "entry_policy": self.cfg.decision.entry_policy,
                "exit_policy": self.cfg.decision.exit_policy, "entry_min_expected_edge_pct": self.entry_edge_threshold,
                "portfolio_policy": self.portfolio_policy, "last_decision": self.last_decision,
                "database": self.cfg.decision.database_path, "faulted": self.faulted,
                "live_execution_enabled": False, "schema_version": SCHEMA_VERSION, "policy_version": self.policy_version}

    def close(self):
        self.inference.cancel()
        with self.lock:
            if self.closed: return
            self.end_session(reason="controller_closed", liquidate=False)
            self.inference.close()
            self.store.close()
            self.closed = True
