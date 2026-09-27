#!/usr/bin/env python3
"""Run forward-only historical Jev paper trading. OpenRouter is the default provider."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import load_config
from decision.replay import run_replay
from decision.fixtures import FixtureDecisionProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.jev.example.yaml")
    parser.add_argument("--data", required=True, help="Chronological frame JSONL; see JEV_INTEGRATION.md")
    parser.add_argument("--provider", choices=["openrouter", "fixture"], default="openrouter")
    parser.add_argument("--database", help="Override the SQLite output path")
    parser.add_argument("--summary", help="Also write the summary to a JSON file")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.database: cfg.decision.database_path = args.database
        result = run_replay(cfg, args.data, provider=FixtureDecisionProvider() if args.provider == "fixture" else None)
        text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
        if args.summary:
            path = Path(args.summary); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding="utf-8")
        print(text)
        return 0
    except Exception as exc:
        # Errors from the provider are sanitized codes. No request headers or environment is printed.
        print(f"Replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__": raise SystemExit(main())
