#!/usr/bin/env python3
"""Evaluate a fine-tuned Laya checkpoint end to end and (optionally) calibrate its entry threshold.

Every request goes through the production path: LayaDecisionProvider -> JEV response
contract -> validate_response. Reported:

* teacher agreement per question vs Jev on held-out decisions (imitation, not correctness);
* forecast quality vs the training-marginal baseline (observed 5-minute markouts);
* the expected-value entry rule as a backtest on hypothetical markouts (ask entry, bid exit,
  configured fees; no depth or latency), next to simple baselines;
* the expected-value exit rule on derived position states.

--write-policy selects the entry threshold on the CALIBRATION split only and stores it in
the checkpoint; the test split is then reported as the out-of-sample check. If no threshold
has a positive realized mean on calibration, the stored threshold makes the bot not buy.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DecisionConfig  # noqa: E402
from decision.contracts import validate_response  # noqa: E402
from decision.expected_value import entry_edge_pct  # noqa: E402
from decision.laya_provider import LayaDecisionProvider, jev_confidence, resolve_checkpoint  # noqa: E402
from decision.questions import FORECAST_ID, portfolio_questions, prebuy_questions, symbol_questions  # noqa: E402

FRACTIONS = {"SMALL": 0.25, "HALF": 0.5, "FULL": 1.0}
POLICY_BLOCKS = {"jev_portfolio_pause"}  # model policy of the teacher run, not a deterministic guardrail
THRESHOLDS = [round(-0.3 + 0.05 * i, 2) for i in range(31)]


def questions_for(stage, forecast):
    if stage == "portfolio":
        return portfolio_questions()
    if stage == "prebuy":
        return prebuy_questions(forecast=forecast)
    return symbol_questions(stage == "position", FRACTIONS, forecast=forecast)


def run_model(checkpoint, rows, *, require_state_format=True, batch_size=8):
    cfg = DecisionConfig(engine="laya", laya_checkpoint=str(checkpoint), laya_batch_size=batch_size,
                         max_decision_age_seconds=3600.0)
    provider = LayaDecisionProvider(cfg, require_state_format=require_state_format)
    provider.check_ready()
    forecast = bool(provider.meta.get("bot_forecast"))
    by_stage = collections.defaultdict(list)
    for i, row in enumerate(rows):
        by_stage[row["stage"]].append(i)
    answers, invalid, latency = [None] * len(rows), 0, []
    for stage, indices in by_stage.items():
        questions = questions_for(stage, forecast)
        for start in range(0, len(indices), 32):
            chunk = indices[start:start + 32]
            t0 = time.perf_counter()
            out = provider.evaluate_batch([(rows[i]["state"], questions) for i in chunk], deadline=time.monotonic() + 3600)
            latency.append((time.perf_counter() - t0) * 1000 / len(chunk))
            for i, response in zip(chunk, out):
                try:
                    answers[i] = validate_response(response, questions)["answers"]
                except (ValueError, TypeError, KeyError):
                    invalid += 1
    return provider, answers, invalid, latency


def teacher_agreement(rows, answers, majority):
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    for row, ans in zip(rows, answers):
        if ans is None or row["variant"] != "recorded":
            continue
        for qid, teacher in (row.get("teacher_answers") or {}).items():
            mine = ans.get(qid)
            if not mine or teacher.get("type") != mine.get("type") or teacher["type"] == "noul":
                continue
            keys = list(teacher["probabilities"])
            t = [float(teacher["probabilities"][k]) for k in keys]
            m = [float(mine["probabilities"].get(k, 0.0)) for k in keys]
            s = per[qid]
            s["agree"].append(float(max(keys, key=lambda k: mine["probabilities"].get(k, 0)) == max(keys, key=lambda k: teacher["probabilities"][k])))
            s["soft"].append(sum(a * b for a, b in zip(t, m)))
            s["tv"].append(0.5 * sum(abs(a - b) for a, b in zip(t, m)))
            s["majority"].append(float(max(keys, key=lambda k: teacher["probabilities"][k]) == majority.get(qid)))
            s["gate_laya_0.3"].append(float(jev_confidence(m) >= 0.3))
            s["gate_jev_0.3"].append(float(jev_confidence(t) >= 0.3))
            if teacher["type"] == "score":
                s["score_mae"].append(abs(float(mine["score"]) - float(teacher["score"])))
    return {qid: {k: round(statistics.fmean(v), 4) for k, v in s.items()} | {"n": len(s["agree"])} for qid, s in per.items()}


def forecast_quality(rows, answers, marginal):
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for row, ans in zip(rows, answers):
        if ans is None or not row.get("outcome") or FORECAST_ID not in ans:
            continue
        probs = ans[FORECAST_ID]["probabilities"]
        level = row["outcome"]["forecast_level"]
        p = [float(probs[str(i)]) for i in range(len(probs))]
        g = groups[row["variant"]]
        g["log_loss"].append(-math.log(max(1e-6, p[level])))
        g["baseline_log_loss"].append(-math.log(max(1e-6, marginal[level])))
        g["level_accuracy"].append(float(max(range(len(p)), key=p.__getitem__) == level))
        cdf_p = cdf_t = rps = 0.0
        for i in range(len(p)):
            cdf_p += p[i]; cdf_t += float(i == level)
            rps += (cdf_p - cdf_t) ** 2
        g["rps"].append(rps / (len(p) - 1))
        g["expected"].append(ans[FORECAST_ID].get("expected_mid_return_pct"))
        g["realized"].append(row["outcome"]["mid_return_pct"])
    out = {}
    for variant, g in groups.items():
        metrics = {k: round(statistics.fmean(v), 4) for k, v in g.items() if k not in ("expected", "realized")}
        pairs = [(e, r) for e, r in zip(g["expected"], g["realized"]) if isinstance(e, (int, float))]
        if len(pairs) > 3:
            metrics["spearman_expected_vs_realized"] = round(spearman(*zip(*pairs)), 4)
        metrics["n"] = len(g["log_loss"])
        out[variant] = metrics
    return out


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=v.__getitem__)
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = ranks(list(xs)), ranks(list(ys))
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def entry_candidates(rows, answers):
    """Recorded candidate states with an observed markout and no deterministic entry block."""
    out, blocked = [], 0
    for row, ans in zip(rows, answers):
        if ans is None or row["variant"] != "recorded" or row["stage"] != "candidate" or not row.get("outcome"):
            continue
        realized = row["outcome"].get("hypothetical_buy_hold_net_return_pct")
        if not isinstance(realized, (int, float)):
            continue
        state = row["state"]
        if set(state.get("guardrails", {}).get("entry_block_reasons") or []) - POLICY_BLOCKS:
            blocked += 1
            continue
        costs = state.get("costs") or {}
        edge, _ = entry_edge_pct((ans.get(FORECAST_ID) or {}).get("expected_mid_return_pct"), state.get("market"),
                                 float(costs.get("fee_rate_pct", 0.1)), float(costs.get("slippage_bps", 0.0)))
        teacher_action = (row.get("teacher_answers") or {}).get("action") or {}
        out.append({"edge": edge, "realized": float(realized), "symbol": row["symbol"],
                    "jev_buy": teacher_action.get("choice") == "BUY",
                    "legacy_buy": (state.get("legacy_advisory") or {}).get("signal") == "BUY",
                    "laya_action_buy": (ans.get("action") or {}).get("choice") == "BUY"})
    return out, blocked


def trades_summary(selected):
    if not selected:
        return {"trades": 0}
    r = [t["realized"] for t in selected]
    return {"trades": len(r), "mean_net_pct": round(statistics.fmean(r), 4), "sum_net_pct": round(sum(r), 4),
            "hit_rate": round(sum(x > 0 for x in r) / len(r), 4), "symbols": len({t["symbol"] for t in selected})}


def entry_backtest(cands):
    grid = {str(t): trades_summary([c for c in cands if c["edge"] is not None and c["edge"] >= t]) for t in THRESHOLDS}
    baselines = {"buy_every_unblocked": trades_summary(cands),
                 "jev_action_buy": trades_summary([c for c in cands if c["jev_buy"]]),
                 "legacy_strategy_buy": trades_summary([c for c in cands if c["legacy_buy"]]),
                 "laya_imitation_action_buy": trades_summary([c for c in cands if c["laya_action_buy"]])}
    return {"eligible": len(cands), "by_threshold": grid, "baselines": baselines}


def choose_threshold(calib_cands, min_trades=5):
    """Maximize total realized net on calibration among thresholds with a positive mean."""
    best = None
    for t in THRESHOLDS:
        s = trades_summary([c for c in calib_cands if c["edge"] is not None and c["edge"] >= t])
        if s["trades"] >= min_trades and s["mean_net_pct"] > 0 and (best is None or s["sum_net_pct"] > best[1]["sum_net_pct"]):
            best = (t, s)
    if best:
        return best[0], True, best[1]
    edges = [c["edge"] for c in calib_cands if c["edge"] is not None]
    # No edge found: a threshold above every calibration edge, i.e. the bot does not buy.
    return round(max(edges, default=0.0) + 0.25, 2), False, None


def exit_backtest(rows, answers, min_exit_edge):
    sold, held = [], []
    for row, ans in zip(rows, answers):
        if ans is None or row["variant"] != "synthetic_position" or not row.get("outcome"):
            continue
        expected = (ans.get(FORECAST_ID) or {}).get("expected_mid_return_pct")
        if not isinstance(expected, (int, float)):
            continue
        (sold if expected <= -min_exit_edge else held).append(row["outcome"]["mid_return_pct"])
    return {"min_exit_edge_pct": min_exit_edge,
            "sell_signals": len(sold), "realized_mid_change_after_sell_signal_pct": round(statistics.fmean(sold), 4) if sold else None,
            "hold_signals": len(held), "realized_mid_change_after_hold_signal_pct": round(statistics.fmean(held), 4) if held else None,
            "note": "a sell is useful when the price then falls (negative number after sell signals)"}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", default="models/laya-bsjev")
    p.add_argument("--dataset", default="data/laya_dataset")
    p.add_argument("--compare-base", help="zero-shot base checkpoint directory for the teacher-agreement comparison")
    p.add_argument("--write-policy", action="store_true", help="store the calibration-selected entry threshold in the checkpoint")
    p.add_argument("--min-exit-edge", type=float, default=0.05)
    p.add_argument("--report", default=None)
    p.add_argument("--sample-per-split", type=int, default=0, help="evaluate at most N random requests per split (0 = all)")
    args = p.parse_args()

    dataset = Path(args.dataset)
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (dataset / "eval_requests.jsonl").open(encoding="utf-8")]
    if args.sample_per_split:
        import random
        rng, kept = random.Random(20260927), []
        for split in ("calib", "test"):
            part = [r for r in rows if r["split"] == split]
            kept += rng.sample(part, min(len(part), args.sample_per_split))
        rows = kept
    majority = {}
    counts = collections.defaultdict(collections.Counter)
    for line in (dataset / "dataset.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        if r["split"] == "train" and r["supervision"] == "teacher" and r.get("teacher_choice"):
            counts[r["question_id"]][r["teacher_choice"]] += 1
    majority = {q: c.most_common(1)[0][0] for q, c in counts.items()}
    level_counts = manifest["forecast"]["train_level_counts"]
    total = sum(level_counts.values())
    marginal = [level_counts[str(i)] / total for i in range(len(level_counts))]

    provider, answers, invalid, latency = run_model(args.checkpoint, rows)
    report = {"checkpoint": str(resolve_checkpoint(args.checkpoint)), "weights_sha256": provider.digest,
              "dataset_sha256": manifest["dataset_sha256"], "contract_invalid_responses": invalid,
              "latency_ms_per_state": {"p50": round(statistics.median(latency), 1), "max": round(max(latency), 1),
                                       "device": str(provider.agent.device)}, "splits": {}}
    cands_by_split = {}
    for split in ("calib", "test"):
        idx = [i for i, r in enumerate(rows) if r["split"] == split]
        srows, sans = [rows[i] for i in idx], [answers[i] for i in idx]
        cands, blocked = entry_candidates(srows, sans)
        cands_by_split[split] = cands
        report["splits"][split] = {"teacher_agreement": teacher_agreement(srows, sans, majority),
                                   "forecast": forecast_quality(srows, sans, marginal),
                                   "entry_backtest": {**entry_backtest(cands), "blocked_by_guardrails": blocked},
                                   "exit_backtest": exit_backtest(srows, sans, args.min_exit_edge)}
    threshold, found, calib_summary = choose_threshold(cands_by_split["calib"])
    test_at = trades_summary([c for c in cands_by_split["test"] if c["edge"] is not None and c["edge"] >= threshold])
    report["policy"] = {"entry_min_expected_edge_pct": threshold, "edge_found_on_calibration": found,
                        "selected_on": "calib", "calib_at_threshold": calib_summary, "test_at_threshold": test_at,
                        "horizon_seconds": manifest["forecast"]["horizon_seconds"]}
    if args.compare_base:
        _, base_answers, base_invalid, base_latency = run_model(args.compare_base, rows, require_state_format=False)
        idx = [i for i, r in enumerate(rows) if r["split"] == "test"]
        report["base_zero_shot_test_teacher_agreement"] = teacher_agreement([rows[i] for i in idx], [base_answers[i] for i in idx], majority)
        report["base_contract_invalid_responses"] = base_invalid
    if args.write_policy:
        cfg_path = resolve_checkpoint(args.checkpoint) / "rl_agent_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["bot_policy"] = {**report["policy"], "written_at": time.time()}
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    out = Path(args.report or ROOT / "reports" / f"laya_eval_{time.strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"report": str(out), "policy": report["policy"], "invalid": invalid,
                      "latency_ms_per_state": report["latency_ms_per_state"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
