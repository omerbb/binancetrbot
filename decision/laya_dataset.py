"""Training data for Laya from Jev decision journals.

Two kinds of supervision, never mixed up:

* teacher  - Jev's full answer distributions for the exact questions Jev was asked
             (distillation; Jev is not ground truth);
* outcome  - the observed 5-minute mid-price markout for the forecast question
             (what the market actually did; no teacher involved).

The recorded Jev sessions contain no open positions, so position (and prebuy) states are
also synthesized from each recorded candidate state: the position is entered at a past
closed candle of that same state's history, marked with the code the bot uses live
(simulator arithmetic, RiskManager.evaluate_exit). Only information available at `as_of`
is used; the label is the outcome recorded after `as_of`. Splits are chronological for
the test run(s) and grouped by (run, symbol) for calibration, so near-duplicate
consecutive evaluations of one symbol never straddle a split.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import statistics
import time
from pathlib import Path

from core.risk_manager import RiskManager
from decision.contracts import dumps
from decision.laya_state import STATE_FORMAT, question_key, render_state
from decision.questions import (FORECAST_HORIZON_SECONDS, FORECAST_ID, FORECAST_LEVELS, forecast_level,
                                forecast_question)

DATASET_VERSION = "laya-bsjev-dataset-v1"
TEACHER_PROVIDERS = ("openrouter-systemone",)
SYNTHETIC_ENTRY_MINUTES = (1, 2, 3, 5, 8, 12)
RISK_FIELDS = ("take_profit_pct", "partial_tp_pct", "partial_tp_ratio", "enable_partial_tp", "stop_loss_pct",
               "trailing_stop_pct", "trailing_activation_pct", "breakeven_trigger_pct", "max_holding_seconds")


def _stable_fraction(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16) / float(16 ** 12)


def teacher_target(question: dict, answer: dict):
    """Teacher distribution in the model's option order, renormalized; None if unusable."""
    t = question["type"]
    probs = answer.get("probabilities") or {}
    if t == "choice":
        target = [float(probs.get(k, 0.0)) for k in question["criteria"]]
    elif t == "score":
        target = [float(probs.get(str(i), 0.0)) for i in range(len(question["criteria"]))]
    else:
        p = float(answer.get("noul", 0.5))
        target = [1 - p, p]
    total = sum(target)
    if not all(math.isfinite(v) and v >= 0 for v in target) or total <= 0:
        return None
    return [v / total for v in target]


def _read_journal(path: Path, teacher_providers, outcome_providers=()):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    runs = {}
    for row in db.execute("SELECT run_id, started_at, metadata_json FROM runs"):
        meta = json.loads(row["metadata_json"])
        provider = meta.get("provider")
        if meta.get("fixture") or provider not in tuple(teacher_providers) + tuple(outcome_providers):
            continue
        # Outcome-only runs (e.g. Laya's own sessions) contribute market markouts, never answers.
        runs[row["run_id"]] = {"run_id": row["run_id"], "started_at": row["started_at"], "source": str(path),
                               "provider": provider, "teacher": provider in teacher_providers,
                               "thresholds": (meta.get("configuration") or {}).get("strategy") or {}}
    decisions = []
    for row in db.execute("SELECT decision_id, run_id, stage, symbol, as_of, source, request_json FROM decisions "
                          "WHERE source='model' ORDER BY as_of"):
        if row["run_id"] not in runs:
            continue
        response = None
        for (data,) in db.execute("SELECT data_json FROM events WHERE decision_id=? AND kind='model_response' "
                                  "ORDER BY sequence", (row["decision_id"],)):
            event = json.loads(data)
            if event.get("valid"):
                response = event["raw"]
        outcome = None
        for (data,) in db.execute("SELECT data_json FROM outcomes WHERE decision_id=? AND name='market_markout' "
                                  "AND horizon_seconds=? AND status='observed'", (row["decision_id"], FORECAST_HORIZON_SECONDS)):
            outcome = json.loads(data)
        request = json.loads(row["request_json"])
        if not runs[row["run_id"]]["teacher"]:
            response = None
        decisions.append({"decision_id": row["decision_id"], "run_id": row["run_id"], "stage": row["stage"],
                          "symbol": row["symbol"], "as_of": row["as_of"], "state": request["state"],
                          "questions": request["questions"], "teacher": response, "outcome": outcome})
    db.close()
    return runs, decisions


def synthesize_position(state: dict, thresholds: dict, minutes: int):
    """A position entered `minutes` closed candles ago at the ask implied by today's spread.
    Returns None when the history is too short or the bot would already have hard-stopped."""
    s = copy.deepcopy(state)
    candles = (s.get("history") or {}).get("closed_candles") or []
    market = s.get("market") or {}
    bid, ask = market.get("bid"), market.get("ask")
    if len(candles) <= minutes or not all(isinstance(v, (int, float)) and v > 0 for v in (bid, ask)) or ask < bid:
        return None
    entry_candle = candles[-minutes - 1]
    entry_price = float(entry_candle["close"]) * (ask / bid) ** 0.5
    entry_time = float(entry_candle["close_time"])
    fee_rate = float((s.get("costs") or {}).get("fee_rate_pct", 0.1)) / 100
    budget = 0.25 * float((s.get("guardrails") or {}).get("maximum_entry_budget_try") or 2000.0)
    quantity = budget * (1 - fee_rate) / entry_price
    highest = max([entry_price, bid] + [float(c["close"]) * (bid / ask) ** 0.5 for c in candles[-minutes:]])
    net = quantity * bid * (1 - fee_rate)
    pnl_pct = (net - budget) / budget * 100
    stop = float(thresholds.get("stop_loss_pct", 0.85))
    if pnl_pct <= -stop:
        return None
    position = {"position_id": "synthetic", "symbol": market.get("symbol"), "entry_price": entry_price,
                "current_price": bid, "highest_price": highest, "quantity": quantity, "invested_cost": budget,
                "entry_fee": budget * fee_rate, "entry_time": entry_time, "entry_reason": "synthetic_training_position",
                "unrealized_pnl": net - budget, "unrealized_pnl_pct": pnl_pct}
    as_of = float(s["as_of"])
    risk = RiskManager(clock=lambda: as_of, fee_rate_pct=fee_rate * 100,
                       **{k: thresholds[k] for k in RISK_FIELDS if k in thresholds})
    close, why, gross = risk.evaluate_exit(copy.deepcopy(position), bid)
    s["position"] = position
    s["risk_reference"] = {**(s.get("risk_reference") or {}), "holding_seconds": as_of - entry_time,
                           "absolute_entry_price_change_pct": (bid / entry_price - 1) * 100,
                           "net_liquidation_pnl_pct": pnl_pct, "absolute_loss_limit_pct": stop}
    legacy = s.setdefault("legacy_advisory", {})
    legacy["exit"] = {"would_exit": close, "reason": why, "reference_gross_pnl_pct": gross}
    guard = s.setdefault("guardrails", {})
    guard["entry_block_reasons"] = sorted(set(guard.get("entry_block_reasons") or []) | {"duplicate_symbol_exposure"})
    portfolio = s.setdefault("portfolio", {})
    portfolio["open_positions_count"] = int(portfolio.get("open_positions_count") or 0) + 1
    if isinstance(portfolio.get("cash"), (int, float)):
        portfolio["cash"] = portfolio["cash"] - budget
    return s


def synthesize_prebuy(state: dict, teacher_answers: dict | None):
    s = copy.deepcopy(state)
    market = s.get("market") or {}
    s["proposal"] = {"decision_id": "synthetic", "as_of": float(s["as_of"]) - 1.5, "action": "BUY", "allocation": "SMALL",
                     "budget_try": 500.0, "allocation_fallback": False, "reference_ask": market.get("ask"),
                     "prior_answers": teacher_answers or {}}
    return s


def assign_split(decision, test_runs, calib_fraction, seed, test_cut=None, gap=0.0):
    if test_cut is not None:
        # Time holdout: the last hours are test; decisions whose labels would reach into the
        # test window are dropped ("gap"), so no outcome straddles the boundary.
        if decision["as_of"] >= test_cut:
            return "test"
        if decision["as_of"] >= test_cut - gap:
            return "gap"
    elif decision["run_id"] in test_runs:
        return "test"
    group = decision["symbol"] or f"portfolio@{int(decision['as_of'] // 300)}"
    return "calib" if _stable_fraction(seed, decision["run_id"], group) < calib_fraction else "train"


def build_dataset(databases, output_dir, *, teacher_providers=TEACHER_PROVIDERS, outcome_providers=(), test_runs=1,
                  calib_fraction=0.15, seed="20260927", synthesize=True, test_hours=0.0):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    runs, decisions, sources = {}, [], []
    for db in databases:
        path = Path(db)
        r, d = _read_journal(path, teacher_providers, outcome_providers)
        runs.update(r)
        decisions.extend(d)
        sources.append({"path": str(path.resolve()), "runs": len(r), "decisions": len(d),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    seen, unique = set(), []
    for d in sorted(decisions, key=lambda x: x["as_of"]):
        if d["decision_id"] not in seen:
            seen.add(d["decision_id"])
            unique.append(d)
    ordered_runs = sorted(runs.values(), key=lambda r: r["started_at"])
    test_ids = {r["run_id"] for r in ordered_runs[-test_runs:]} if test_runs and not test_hours else set()
    # One long session (e.g. overnight) is split in time rather than sent whole to the test set.
    gap = float(max(FORECAST_HORIZON_SECONDS, 900))
    test_cut = max(d["as_of"] for d in unique) - test_hours * 3600 if test_hours and unique else None
    forecast_q = forecast_question()
    rows, eval_rows, level_values = [], [], {}
    counts = {"teacher": {}, "outcome": {}, "decisions": {}}

    def add(d, split, variant, question_id, question, state, target, supervision, extra=None):
        rows.append({"decision_id": d["decision_id"], "run_id": d["run_id"], "split": split, "stage": d["stage"],
                     "variant": variant, "symbol": d["symbol"], "as_of": d["as_of"], "question_id": question_id,
                     "question_key": question_key(question), "question": question, "state_text": render_state(state),
                     "target": target, "supervision": supervision, **(extra or {})})
        key = f"{split}:{supervision}:{question_id}:{variant}"
        counts[supervision][key] = counts[supervision].get(key, 0) + 1

    for d in unique:
        split = assign_split(d, test_ids, calib_fraction, seed, test_cut, gap)
        counts["decisions"][split] = counts["decisions"].get(split, 0) + 1
        if split == "gap":
            continue
        teacher_answers = (d["teacher"] or {}).get("answers") or {}
        for qid, question in d["questions"].items():
            answer = teacher_answers.get(qid)
            target = teacher_target(question, answer) if answer else None
            if target:
                add(d, split, "recorded", qid, question, d["state"], target, "teacher",
                    {"teacher_choice": answer.get("choice"), "teacher_confidence": answer.get("confidence")})
        outcome = d["outcome"]
        if not outcome or d["symbol"] is None or not isinstance(outcome.get("mid_return_pct"), (int, float)):
            continue
        mid = float(outcome["mid_return_pct"])
        level = forecast_level(mid)
        if split == "train":
            level_values.setdefault(level, []).append(mid)
        target = [1.0 if i == level else 0.0 for i in range(len(FORECAST_LEVELS))]
        label = {"mid_return_pct": mid, "forecast_level": level,
                 "hypothetical_buy_hold_net_return_pct": outcome.get("hypothetical_buy_hold_net_return_pct"),
                 "actual_horizon_seconds": outcome.get("actual_horizon_seconds")}
        variants = [("recorded", d["state"])]
        if synthesize and d["stage"] == "candidate":
            thresholds = runs[d["run_id"]]["thresholds"]
            minutes = SYNTHETIC_ENTRY_MINUTES[int(_stable_fraction(seed, d["decision_id"]) * len(SYNTHETIC_ENTRY_MINUTES))]
            position_state = synthesize_position(d["state"], thresholds, minutes)
            if position_state is not None:
                variants.append(("synthetic_position", position_state))
            variants.append(("synthetic_prebuy", synthesize_prebuy(d["state"], teacher_answers)))
        for variant, state in variants:
            add(d, split, variant, FORECAST_ID, forecast_q, state, target, "outcome", {"outcome": label})
            if split != "train":
                stage = {"synthetic_position": "position", "synthetic_prebuy": "prebuy"}.get(variant, d["stage"])
                eval_rows.append({"decision_id": d["decision_id"], "run_id": d["run_id"], "split": split, "stage": stage,
                                  "variant": variant, "symbol": d["symbol"], "state": state, "outcome": label,
                                  "teacher_answers": teacher_answers if variant == "recorded" else {}})
        # Teacher-only decisions of held-out splits are evaluated end to end as well.
    for d in unique:
        split = assign_split(d, test_ids, calib_fraction, seed, test_cut, gap)
        if split in ("test", "calib") and not d["outcome"] and (d["teacher"] or {}).get("answers"):
            eval_rows.append({"decision_id": d["decision_id"], "run_id": d["run_id"], "split": split, "stage": d["stage"],
                              "variant": "recorded", "symbol": d["symbol"], "state": d["state"], "outcome": None,
                              "teacher_answers": d["teacher"]["answers"]})

    means = [statistics.fmean(level_values[i]) if level_values.get(i) else (lo + hi) / 2 if math.isfinite(lo + hi) else (lo if math.isfinite(lo) else hi)
             for i, (_, lo, hi) in enumerate(FORECAST_LEVELS)]
    rows_path, eval_path = output / "dataset.jsonl", output / "eval_requests.jsonl"
    with rows_path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(dumps(row) + "\n")
    with eval_path.open("w", encoding="utf-8") as stream:
        for row in eval_rows:
            stream.write(dumps(row) + "\n")
    manifest = {"dataset_version": DATASET_VERSION, "state_format": STATE_FORMAT, "created_at": time.time(),
                "teacher_providers": list(teacher_providers), "outcome_only_providers": list(outcome_providers), "sources": sources,
                "runs": [{**r, "split": "test" if r["run_id"] in test_ids else "train+calib"} for r in ordered_runs],
                "test_split": {"mode": "time", "test_hours": test_hours, "cut_as_of": test_cut, "gap_seconds": gap}
                if test_cut is not None else {"mode": "runs", "test_runs": test_runs},
                "calib_fraction": calib_fraction, "seed": seed, "rows": len(rows), "eval_requests": len(eval_rows),
                "counts": counts, "forecast": {"question_id": FORECAST_ID, "question_key": question_key(forecast_q),
                                               "horizon_seconds": FORECAST_HORIZON_SECONDS,
                                               "levels": [{"text": t, "low_pct": lo if math.isfinite(lo) else None,
                                                           "high_pct": hi if math.isfinite(hi) else None} for t, lo, hi in FORECAST_LEVELS],
                                               "level_values_pct": means,
                                               "train_level_counts": {str(i): len(level_values.get(i, [])) for i in range(len(FORECAST_LEVELS))}},
                "dataset_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
                "notes": ["teacher targets imitate Jev and are not ground truth",
                          "forecast targets are observed markouts; hypothetical net returns assume ask entry, bid exit, "
                          "configured fees, no depth or latency",
                          "synthetic position/prebuy states reuse the recorded market state and its recorded outcome"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
