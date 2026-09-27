#!/usr/bin/env python3
"""Run a deliberately bounded, billable JEV demo on synthetic replay data only."""
from __future__ import annotations
import argparse
import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from decision.contracts import validate_config
from decision.openrouter import BudgetedOpenRouterJevProvider
from decision.replay import run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.jev.demo.yaml")
    parser.add_argument("--data", default="data/demo_frames.jsonl")
    parser.add_argument("--database", default="data/jev_demo.sqlite3")
    parser.add_argument("--summary", default="data/jev_demo_summary.json")
    parser.add_argument("--max-frames", type=int, default=30)
    parser.add_argument("--max-http-calls", type=int, default=30)
    args = parser.parse_args()
    if args.max_frames < 3 or args.max_http_calls < 1:
        parser.error("--max-frames must be at least 3 and --max-http-calls at least 1")
    cfg = load_config(args.config)
    cfg.decision.database_path = args.database
    validate_config(cfg)
    if not (cfg.decision.api_key or os.environ.get(cfg.decision.api_key_env, "").strip()):
        print(f"Missing API key: set decision.api_key or environment variable {cfg.decision.api_key_env}", file=sys.stderr)
        return 2
    data = Path(args.data)
    if not data.is_file():
        parser.error(f"Replay input does not exist: {data}")
    with data.open("r", encoding="utf-8") as stream, tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", encoding="utf-8", delete=False
    ) as limited:
        for line in itertools.islice(stream, args.max_frames):
            limited.write(line)
        limited_path = Path(limited.name)
    try:
        provider = BudgetedOpenRouterJevProvider(cfg.decision, args.max_http_calls)
        result = run_replay(cfg, limited_path, provider=provider)
        result["http_calls_made"] = provider.calls_made
        result["http_call_limit"] = args.max_http_calls
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        destination = Path(args.summary)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        print(text)
        return 0
    except Exception as exc:
        print(f"Bounded JEV demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        limited_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
