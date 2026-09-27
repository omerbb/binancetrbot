#!/usr/bin/env python3
"""Build the Laya training set from one or more Jev decision journals (read-only).

Example:
  python tools/laya_build_dataset.py --database ../binancetrbot_jev_openrouter/binancetrbot-jev/data/jev_demo.sqlite3 \
      --output data/laya_dataset
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decision.laya_dataset import TEACHER_PROVIDERS, build_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", action="append", required=True, help="Jev journal SQLite; repeatable")
    parser.add_argument("--output", default="data/laya_dataset")
    parser.add_argument("--teacher-provider", action="append", default=None,
                        help=f"Run provider names accepted as teacher (default {TEACHER_PROVIDERS}); Laya's own runs are excluded")
    parser.add_argument("--outcome-provider", action="append", default=[],
                        help="Run providers used for outcome labels only, e.g. laya-local (own paper sessions, incl. real positions)")
    parser.add_argument("--test-runs", type=int, default=1, help="Most recent N runs held out as the test set")
    parser.add_argument("--test-hours", type=float, default=0.0,
                        help="Hold out the last H hours in time instead of whole runs (use for one long overnight session)")
    parser.add_argument("--calib-fraction", type=float, default=0.15, help="Grouped (run, symbol) share for calibration")
    parser.add_argument("--no-synthetic", action="store_true", help="Do not derive position/prebuy states")
    args = parser.parse_args()
    manifest = build_dataset(args.database, args.output, teacher_providers=tuple(args.teacher_provider or TEACHER_PROVIDERS),
                             outcome_providers=tuple(args.outcome_provider),
                             test_runs=args.test_runs, calib_fraction=args.calib_fraction, synthesize=not args.no_synthetic,
                             test_hours=args.test_hours)
    summary = {k: manifest[k] for k in ("rows", "eval_requests", "dataset_sha256")}
    summary["decisions_per_split"] = manifest["counts"]["decisions"]
    summary["forecast_level_values_pct"] = [round(v, 3) for v in manifest["forecast"]["level_values_pct"]]
    summary["forecast_train_level_counts"] = manifest["forecast"]["train_level_counts"]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
