#!/usr/bin/env python3
"""An explicitly requested, BILLABLE OpenRouter protocol probe. No market access/trades."""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import load_config
from decision.contracts import validate_config, validate_response
from decision.openrouter import OpenRouterJevProvider

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.jev.example.yaml")
    a = p.parse_args()
    cfg = load_config(a.config)
    validate_config(cfg)
    provider = OpenRouterJevProvider(cfg.decision)
    questions = {"ready": {"type": "choice", "instructions": "Choose the label matching state.status.",
                            "criteria": {"READY": "state.status is ready", "NOT_READY": "state.status is not ready"}}}
    try:
        response = provider.evaluate({"status": "ready", "purpose": "non-trading connectivity test"}, questions)
        print(json.dumps(validate_response(response, questions), indent=2, ensure_ascii=False)); return 0
    except Exception as e:
        print(f"Probe failed: {type(e).__name__}: {e}", file=sys.stderr); return 1
    finally: provider.close()

if __name__ == "__main__": raise SystemExit(main())
