"""Read-only audit metrics; never print credentials or whole model requests."""
import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path


def summarize(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        decisions = list(db.execute("SELECT decision_id,stage,as_of FROM decisions"))
        counts = Counter()
        errors = Counter()
        dispositions = Counter()
        overrides = Counter()
        models = Counter()
        latency = []
        costs = []
        intent = Counter()
        result = Counter()
        choices = Counter()
        latest = {}
        attempts = Counter()
        for row in db.execute("SELECT decision_id,kind,data_json FROM events ORDER BY sequence"):
            data = json.loads(row["data_json"])
            kind = row["kind"]
            counts[kind] += 1
            if kind == "model_response":
                counts["valid_model_response"] += bool(data.get("valid"))
                if data.get("validation_error"):
                    errors[data["validation_error"]] += 1
                raw = data.get("raw") or {}
                models[raw.get("model", "missing")] += 1
                if "cost" in raw.get("usage", {}):
                    costs.append(raw["usage"]["cost"])
                if data.get("latency_ms") is not None:
                    latency.append(data["latency_ms"])
                for key in ("action", "portfolio_action", "prebuy_authorization"):
                    answer = raw.get("answers", {}).get(key)
                    if answer:
                        choices[f"{key}:{answer.get('choice')}"] += 1
            elif kind == "decision_disposition":
                dispositions[f"{data.get('applied_action')}:{data.get('status')}"] += 1
                overrides.update(data.get("override_reasons", []))
            elif kind == "inference_failed":
                errors[data.get("error_code", "unknown")] += 1
            elif kind == "inference_attempt":
                attempts[str(data.get("http_status", data.get("status")))] += 1
            elif kind == "execution_intent":
                intent[row["decision_id"]] += 1
            elif kind == "execution_result" and data.get("attribution") != "prebuy_child_execution":
                result[row["decision_id"]] += 1
                if data.get("status") == "filled":
                    latest[f"fills_{data['fill']['side']}"] = latest.get(f"fills_{data['fill']['side']}", 0) + 1
        runs = []
        for row in db.execute("SELECT * FROM runs"):
            meta = json.loads(row["metadata_json"])
            config = meta.get("configuration", {}).get("decision", {})
            runs.append({"run_id": row["run_id"], "started_at": row["started_at"],
                         "ended_at": row["ended_at"], "end_reason": row["end_reason"],
                         "provider": meta.get("provider"), "environment": meta.get("environment"),
                         "policy_version": meta.get("policy_version"), "fixture": meta.get("fixture"),
                         "confidence_threshold": config.get("min_action_confidence"),
                         "code_sha256": meta.get("code_sha256")})
        return {"database": str(path), "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_key_violations": len(db.execute("PRAGMA foreign_key_check").fetchall()),
                "runs": runs, "decisions": len(decisions),
                "stages": dict(Counter(r["stage"] for r in decisions)), "events": dict(counts),
                "errors": dict(errors), "dispositions": dict(dispositions), "overrides": dict(overrides),
                "raw_choices_including_invalid": dict(choices), "models": dict(models),
                "http_attempt_statuses": dict(attempts),
                "reported_cost_usd": sum(costs),
                "response_latency_ms": {"n": len(latency), "mean": statistics.mean(latency),
                                        "p95": sorted(latency)[math.ceil(.95 * len(latency)) - 1],
                                        "max": max(latency)} if latency else {},
                "unresolved_intents": sum(max(0, n-result[did]) for did, n in intent.items()),
                "fills": latest,
                "outcomes": dict(db.execute("SELECT status,count(*) FROM outcomes GROUP BY status").fetchall()),
                "ledger": [dict(r) for r in db.execute("SELECT status,count(*) AS positions,sum(realized_pnl) AS pnl_try,sum(total_fees) AS fees_try FROM position_ledger GROUP BY status")]}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    metrics = [summarize(path) for path in args.databases]
    Path(args.output).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in metrics:
        print(json.dumps({k: v for k, v in item.items() if k not in ("runs", "events")}, ensure_ascii=False))
