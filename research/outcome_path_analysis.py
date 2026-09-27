"""Path-dependent hindsight analysis of the overnight Laya journal (read-only).

For every unblocked candidate, rebuild the following 1-minute candles of the same symbol
from LATER recorded states (each state carries its last 50 closed candles), then measure:
  * max favorable / adverse excursion within 15 minutes (hindsight opportunity),
  * take-profit / stop-loss / timeout exits (what a rule-based exit would have realized).
Costs: entry at the ask, exit at the bid (current spread), 0.1% fee per side.
Intrabar ambiguity: if TP and SL fall in the same candle, the stop is assumed first.
"""
import collections
import json
import sqlite3
import statistics as st

import argparse

_p = argparse.ArgumentParser(description="Path-dependent hindsight analysis of a Laya journal")
_p.add_argument("--database", default="data/laya_night.sqlite3")
DB = __import__("pathlib").Path(_p.parse_args().database).resolve().as_uri() + "?mode=ro"
FEE = 0.001
HORIZON_MIN = 15

db = sqlite3.connect(DB, uri=True)
candles = collections.defaultdict(dict)   # symbol -> open_time -> (o, h, l, c)
cands = []
edges = {}
for did, dj in db.execute("SELECT decision_id, data_json FROM events WHERE kind='expected_value_assessment'"):
    edges[did] = json.loads(dj).get("edge_pct")
for did, as_of, rj in db.execute("SELECT decision_id, as_of, request_json FROM decisions WHERE stage='candidate'"):
    s = json.loads(rj)["state"]
    m = s["market"]
    sym = m.get("symbol")
    rows = (s.get("history") or {}).get("closed_candles") or []
    for r in rows:
        candles[sym][r["open_time"]] = (r["open"], r["high"], r["low"], r["close"])
    blocks = set((s.get("guardrails") or {}).get("entry_block_reasons") or []) - {"jev_portfolio_pause"}
    if blocks or not rows or not m.get("bid") or not m.get("ask"):
        continue
    cands.append({"id": did, "sym": sym, "as_of": as_of, "c0": rows[-1]["close"], "last_open": rows[-1]["open_time"],
                  "spread": m["ask"] / m["bid"] - 1, "legacy": (s.get("legacy_advisory") or {}).get("signal"),
                  "edge": edges.get(did)})

def path(c):
    """Forward candles strictly after the decision's last closed candle, contiguous minutes."""
    out, t = [], c["last_open"] + 60
    book = candles[c["sym"]]
    for _ in range(HORIZON_MIN):
        if t not in book:
            break
        out.append(book[t])
        t += 60
    return out

def net(exit_ratio, spread):
    # exit_ratio = exit trade price / entry-time last price; buy at ask, sell at bid
    return ((exit_ratio * (1 - spread / 2)) / (1 + spread / 2) * (1 - FEE) ** 2 - 1) * 100

usable = []
for c in cands:
    p = path(c)
    if len(p) >= HORIZON_MIN and c["c0"] > 0:
        c["p"] = p
        usable.append(c)
print(f"unblocked candidates {len(cands)}, with a full {HORIZON_MIN}-min forward path {len(usable)}")

cost = [((1 + c['spread'] / 2) / (1 - c['spread'] / 2) / (1 - FEE) ** 2 - 1) * 100 for c in usable]
mfe = [(max(h for _, h, _, _ in c["p"]) / c["c0"] - 1) * 100 for c in usable]
mae = [(min(l for _, _, l, _ in c["p"]) / c["c0"] - 1) * 100 for c in usable]
print(f"median round-trip cost {st.median(cost):.3f}%")
print(f"hindsight: peak within 15 min clears costs in {sum(m > k for m, k in zip(mfe, cost)) / len(usable):.1%} of candidates; "
      f"clears costs +0.3% in {sum(m > k + .3 for m, k in zip(mfe, cost)) / len(usable):.1%}")
print(f"mean max favorable {st.fmean(mfe):+.3f}%  mean max adverse {st.fmean(mae):+.3f}%  "
      f"hold 15 min net {st.fmean(net(c['p'][-1][3] / c['c0'], c['spread']) for c in usable):+.3f}%")

def simulate(sel, tp, sl):
    res = []
    for c in sel:
        up, dn = c["c0"] * (1 + tp / 100), c["c0"] * (1 - sl / 100)
        exit_ratio = None
        for o, h, l, cl in c["p"]:
            if l <= dn:
                exit_ratio = min(o, dn) / c["c0"]  # gap below the stop fills at the open
                break
            if h >= up:
                exit_ratio = max(o, up) / c["c0"]
                break
        if exit_ratio is None:
            exit_ratio = c["p"][-1][3] / c["c0"]
        res.append(net(exit_ratio, c["spread"]))
    return res

subsets = {"all unblocked": usable,
           "legacy BUY signal": [c for c in usable if c["legacy"] == "BUY"],
           "Laya top 10% edge": sorted([c for c in usable if c["edge"] is not None], key=lambda c: -c["edge"])[:len(usable) // 10],
           "spread <= 0.05%": [c for c in usable if c["spread"] <= 0.0005]}
print("\nTP/SL exit grid (mean net % | hit rate | n), 15-min timeout:")
for name, sel in subsets.items():
    best = None
    lines = []
    for tp in (0.3, 0.5, 0.8, 1.2, 2.0):
        for sl in (0.3, 0.5, 0.85, 1.5):
            r = simulate(sel, tp, sl)
            m = st.fmean(r)
            lines.append((m, tp, sl, sum(x > 0 for x in r) / len(r)))
            if best is None or m > best[0]:
                best = (m, tp, sl, sum(x > 0 for x in r) / len(r))
    worst = min(lines)
    print(f"  {name:20s} n={len(sel):5d} best TP{best[1]}/SL{best[2]}: {best[0]:+.3f}% hit {best[3]:.1%} | worst {worst[0]:+.3f}%")
