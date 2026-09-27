#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decision.csv_replay import convert_ohlcv_csv

def main():
    p = argparse.ArgumentParser(description="1m CSV -> synthetic bar-open quotes. No current-bar lookahead.")
    p.add_argument("--input", required=True); p.add_argument("--output", required=True)
    p.add_argument("--symbol", required=True); p.add_argument("--spread-bps", required=True, type=float)
    a = p.parse_args()
    try:
        print(json.dumps(convert_ohlcv_csv(a.input, a.output, symbol=a.symbol, spread_bps=a.spread_bps))); return 0
    except (ValueError, OSError) as e: p.error(str(e))

if __name__ == "__main__": raise SystemExit(main())
