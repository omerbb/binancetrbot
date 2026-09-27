#!/usr/bin/env python3
"""Offline readiness check for the Laya engine: loads the checkpoint, answers one recorded
held-out state through the production provider, and prints answers and latency.
No network, no exchange access, no trades."""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import load_config  # noqa: E402
from decision.contracts import validate_response  # noqa: E402
from decision.laya_provider import LayaDecisionProvider  # noqa: E402
from decision.laya_state import render_state  # noqa: E402
from decision.questions import symbol_questions  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.laya.example.yaml")
    p.add_argument("--dataset", default="data/laya_dataset")
    p.add_argument("--repeat", type=int, default=5)
    args = p.parse_args()
    cfg = load_config(args.config)
    cfg.decision.engine = "laya"
    t0 = time.perf_counter()
    provider = LayaDecisionProvider(cfg.decision)
    provider.check_ready()
    load_s = time.perf_counter() - t0
    state = None
    path = Path(args.dataset) / "eval_requests.jsonl"
    if path.is_file():
        for line in path.open(encoding="utf-8"):
            row = json.loads(line)
            if row["stage"] == "candidate":
                state = row["state"]
                break
    if state is None:
        raise SystemExit("no recorded candidate state found; build the dataset first")
    questions = symbol_questions(False, cfg.decision.allocation_fractions, forecast=True)
    latencies = []
    for _ in range(max(1, args.repeat)):
        t = time.perf_counter()
        response = validate_response(provider.evaluate(state, questions), questions)
        latencies.append((time.perf_counter() - t) * 1000)
    print(render_state(state), "\n")
    for qid, answer in response["answers"].items():
        value = answer.get("choice", answer.get("score"))
        extra = f" expected_mid_return {answer['expected_mid_return_pct']:+.3f}%" if "expected_mid_return_pct" in answer else ""
        print(f"{qid:22s} {value!s:14s} jev-style confidence {answer['confidence']:.2f}{extra}")
    print(f"\nmodel {provider.model} on {provider.agent.device} | load {load_s:.1f}s | "
          f"decision p50 {statistics.median(latencies):.0f} ms | cost 0 | network none")
    print(json.dumps(provider.run_metadata(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
