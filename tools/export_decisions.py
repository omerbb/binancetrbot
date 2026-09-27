#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decision.export import export_dataset

def main():
    p = argparse.ArgumentParser(description="Export recorded requests or teacher targets. Outcomes stay OUTSIDE model inputs.")
    p.add_argument("--database", required=True); p.add_argument("--output", required=True)
    p.add_argument("--layout", choices=["requests", "questions", "sft"], default="requests")
    p.add_argument("--run-id"); p.add_argument("--include-fixtures", action="store_true")
    p.add_argument("--completed-only", action="store_true", help="Require every linked outcome to be observed; may cause selection bias")
    a = p.parse_args()
    try:
        result = export_dataset(a.database, a.output, layout=a.layout, run_id=a.run_id,
                                include_fixtures=a.include_fixtures, completed_only=a.completed_only)
        print(json.dumps(result)); return 0
    except Exception as e: p.error(str(e))

if __name__ == "__main__": raise SystemExit(main())
