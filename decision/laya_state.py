"""Compact, deterministic text rendering of controller states for Laya.

Jev receives the full JSON state (~8k tokens with 50 candles and 60 ticks). Laya's encoder
reads at most `max_len` tokens and silently truncates the rest, so every raw series is
reduced here to derived features. The SAME function renders training states (read back
from the Jev journal) and live states, so there is no train/serve skew. Any change to the
output requires a new STATE_FORMAT and a rebuilt dataset; checkpoints record the format.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics

from decision.questions import COMMON

STATE_FORMAT = "laya-compact-v1"


def _num(value, digits=2, *, signed=False, suffix=""):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "na"
    text = f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
    return text + suffix


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "na"
    return f"{value:.6g}"


def _yn(value):
    return "na" if value is None else ("yes" if value else "no")


def _pct_change(new, old):
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(new) and math.isfinite(old)) or old <= 0:
        return None
    return (new / old - 1) * 100


def _clean_text(text, limit=120):
    text = re.sub(r"[^\w\s.,:;%()<>+\-=/!?]", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit] or "none"


def stage_of(state: dict) -> str:
    if "proposal" in state:
        return "prebuy"
    if not state.get("market"):
        return "portfolio"
    return "position" if state.get("position") else "candidate"


def _candles(history: dict) -> list[str]:
    rows = [r for r in (history or {}).get("closed_candles") or [] if isinstance(r, dict)]
    closes = [float(r["close"]) for r in rows if isinstance(r.get("close"), (int, float)) and r["close"] > 0]
    if len(closes) < 2:
        return [f"candles 1m: {len(closes)} closed, not enough history"]
    last = closes[-1]
    rets = " ".join(f"{k}m {_num(_pct_change(last, closes[-1 - k]), signed=True, suffix='%')}"
                    for k in (1, 5, 15, 30) if len(closes) > k)
    steps = [(b / a - 1) * 100 for a, b in zip(closes[-31:], closes[-30:])]
    hi, lo = max(closes), min(closes)
    peak, drawdown = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        drawdown = min(drawdown, (c / peak - 1) * 100)
    green = sum(1 for r in rows if r.get("close", 0) > r.get("open", 0))
    flat = sum(1 for r in rows if r.get("close") == r.get("open"))
    zero_vol = sum(1 for r in rows if not r.get("volume"))
    vols = [float(r.get("volume") or 0) for r in rows]
    base = statistics.fmean(vols[-30:-5]) if len(vols) > 10 else 0
    vol_ratio = statistics.fmean(vols[-5:]) / base if base > 0 else None
    spark = " ".join(_num(_pct_change(c, last)) for c in closes[-10:])
    return [
        f"candles 1m: {len(closes)} closed, return {rets}, window {_num(_pct_change(last, closes[0]), signed=True, suffix='%')}",
        f"candle stats: volatility {_num(statistics.pstdev(steps), 3, suffix='%')} per min, range position "
        f"{_num((last - lo) / (hi - lo)) if hi > lo else 'flat'}, below window high {_num(_pct_change(last, hi), suffix='%')}, "
        f"max drawdown {_num(drawdown, suffix='%')}, green {green} flat {flat} of {len(rows)}, zero-volume {zero_vol}, "
        f"volume last5/prior {_num(vol_ratio)}",
        f"last 10 closes vs now %: {spark}",
    ]


def _ticks(history: dict, as_of) -> str:
    ticks = [t for t in (history or {}).get("micro_ticks") or []
             if isinstance(t, (list, tuple)) and len(t) >= 2 and isinstance(t[1], (int, float)) and t[1] > 0]
    if len(ticks) < 2:
        return f"micro ticks: {len(ticks)}"
    prices = [float(t[1]) for t in ticks]
    ups = sum(1 for a, b in zip(prices, prices[1:]) if b > a)
    downs = sum(1 for a, b in zip(prices, prices[1:]) if b < a)
    span = float(ticks[-1][0]) - float(ticks[0][0])
    age = (float(as_of) - float(ticks[-1][0])) if isinstance(as_of, (int, float)) else None
    return (f"micro ticks: {len(ticks)} over {_num(span, 0)}s, return {_num(_pct_change(prices[-1], prices[0]), 3, signed=True, suffix='%')}, "
            f"up {ups} down {downs}, last tick age {_num(age, 0)}s")


def _btc(btc: dict) -> str:
    btc = btc or {}
    return (f"btc: velocity 1m {_num(btc.get('velocity_1m_pct'), signed=True, suffix='%')}, up/down ticks "
            f"{btc.get('up_ticks', 'na')}/{btc.get('down_ticks', 'na')}, dumping {_yn(btc.get('is_dumping'))}, "
            f"drop from high {_num(btc.get('drop_from_high'), suffix='%')}")


def _portfolio_line(state: dict) -> str:
    p = state.get("portfolio") or {}
    g = state.get("guardrails") or {}
    equity, cash = p.get("total_equity"), p.get("cash")
    cash_share = cash / equity * 100 if isinstance(cash, (int, float)) and isinstance(equity, (int, float)) and equity > 0 else None
    return (f"portfolio: equity {_num(equity, 0)} TRY, cash {_num(cash_share, 0, suffix='%')}, pnl {_num(p.get('total_pnl_pct'), signed=True, suffix='%')}, "
            f"open {p.get('open_positions_count', 'na')}/{g.get('max_open_positions', 'na')}, closed trades {p.get('total_closed_trades', 'na')}, "
            f"marks fresh {_yn(state.get('portfolio_marks_fresh'))}")


def _session_costs(state: dict) -> str:
    c = state.get("costs") or {}
    remaining = (state.get("session") or {}).get("remaining_seconds")
    return (f"session remaining {_num(remaining, 0)}s; fee {_num(c.get('fee_rate_pct'), suffix='%')} per side, "
            f"slippage {_num(c.get('slippage_bps'), 1)} bps")


def _symbol_lines(state: dict, stage: str) -> list[str]:
    m = state.get("market") or {}
    r = state.get("radar") or {}
    g = state.get("guardrails") or {}
    facts = state.get("computed_facts") or {}
    price = m.get("bid") if isinstance(m.get("bid"), (int, float)) else m.get("price")
    lines = [f"symbol: {m.get('symbol', 'na')} price {_price(price)} TRY"]
    proposal = state.get("proposal")
    if proposal:
        prior = proposal.get("prior_answers") or {}
        act = (prior.get("action") or {}).get("probabilities") or {}
        lines.append(f"proposal: BUY {proposal.get('allocation', 'na')} budget {_num(proposal.get('budget_try'), 0)} TRY, "
                     f"allocation fallback {_yn(proposal.get('allocation_fallback'))}, ask moved "
                     f"{_num(_pct_change(m.get('ask'), proposal.get('reference_ask')), 3, signed=True, suffix='%')}, "
                     f"age {_num((state.get('as_of') or 0) - (proposal.get('as_of') or 0), 1)}s")
        lines.append("prior answers: " + ", ".join(
            f"{k} {(prior.get(k) or {}).get('choice', 'na')}" for k in ("allocation", "regime", "reason_code"))
            + f", buy prob {_num(act.get('BUY'))}, setup {_num((prior.get('setup_quality') or {}).get('score'))}/3")
    pos = state.get("position")
    if pos:
        risk = state.get("risk_reference") or {}
        peak = pos.get("risk_highest_price") or pos.get("highest_price")
        lines.append(f"position: net pnl {_num(pos.get('unrealized_pnl_pct'), signed=True, suffix='%')} after fees, bid vs entry "
                     f"{_num(risk.get('absolute_entry_price_change_pct'), signed=True, suffix='%')}, held {_num(risk.get('holding_seconds'), 0)}s, "
                     f"cost {_num(pos.get('invested_cost'), 0)} TRY, peak vs entry {_num(_pct_change(peak, pos.get('entry_price')), signed=True, suffix='%')}, "
                     f"bid vs peak {_num(_pct_change(m.get('bid'), peak), signed=True, suffix='%')}, loss limit -{_num(risk.get('absolute_loss_limit_pct'))}%, "
                     f"trend reference reset {_yn('risk_reference_price' in pos)}")
        exit_ = (state.get("legacy_advisory") or {}).get("exit") or {}
        lines.append(f"legacy exit rule: would exit {_yn(exit_.get('would_exit'))} - {_clean_text(exit_.get('reason'))}")
    blocks = g.get("entry_block_reasons") or []
    lines.append(f"entry blocks: {', '.join(_clean_text(b, 60) for b in blocks) if blocks else 'none'}; "
                 f"max entry budget {_num(g.get('maximum_entry_budget_try'), 0)} TRY, portfolio policy {g.get('portfolio_policy', 'na')}")
    lines.append(f"quote: spread {_num(m.get('spread_pct'), 3, suffix='%')} (cap {_num(g.get('max_spread_pct'), suffix='%')}), valid {_yn(m.get('quote_valid'))}, "
                 f"age {_num(m.get('quote_age_seconds'), 1)}s; features ready {_yn(m.get('features_ready'))}, candle age {_num(m.get('candle_age_seconds'), 0)}s, "
                 f"issues {', '.join(m.get('data_quality_reasons') or []) or 'none'}")
    atr_pct = m["atr"] / price * 100 if isinstance(m.get("atr"), (int, float)) and isinstance(price, (int, float)) and price > 0 else None
    lo, hi = m.get("bb_lower"), m.get("bb_upper")
    pct_b = (price - lo) / (hi - lo) if all(isinstance(v, (int, float)) for v in (price, lo, hi)) and hi > lo else None
    lines.append(f"indicators: rsi {_num(m.get('rsi'), 1)} ({facts.get('rsi_zone', 'na')}), ema fast above slow {_yn(facts.get('ema_fast_above_slow'))} "
                 f"trend {_num(m.get('ema_trend_pct'), signed=True, suffix='%')}, adx {_num(m.get('adx'), 1)} (+di {_num(m.get('plus_di'), 1)} -di {_num(m.get('minus_di'), 1)}), "
                 f"atr {_num(atr_pct, 3, suffix='%')}, bollinger %b {_num(pct_b)} width {_num(m.get('bb_width_pct'), suffix='%')}")
    lines.append(f"momentum: 24h {_num(m.get('change_24h_pct'), signed=True, suffix='%')}, velocity 1m {_num(m.get('velocity_1m_pct'), signed=True, suffix='%')}, "
                 f"volume surge {_num(m.get('volume_surge_ratio'))} (proxy {_yn(facts.get('volume_surge_is_proxy'))}), burst ratio {_num(m.get('burst_ratio'))}, "
                 f"squeeze breakout {_yn(m.get('is_squeeze_breakout'))}, quant score {_num(m.get('quant_score'))}")
    lines += _candles(state.get("history"))
    lines.append(_ticks(state.get("history"), state.get("as_of")))
    if r:
        rng = _num((price - r["low"]) / (r["high"] - r["low"])) if all(isinstance(r.get(k), (int, float)) for k in ("low", "high")) \
            and isinstance(price, (int, float)) and r["high"] > r["low"] else "na"
        mm = r.get("micro_metrics") or {}
        obs = r.get("observation") or {}
        lines.append(f"radar: 24h volume {_num((r.get('volume_try') or 0) / 1e6, 1)}M TRY, 24h range position {rng}, dumping {_yn(r.get('is_dumping'))}, "
                     f"stagnant {_yn(r.get('is_stagnant'))}, qualified 1m {_yn(r.get('is_qualified_1m'))}, micro up/down {mm.get('up_ticks', 'na')}/{mm.get('down_ticks', 'na')}, "
                     f"drop from high {_num(mm.get('drop_from_high'), suffix='%')}")
        if obs:
            lines.append(f"observation: {obs.get('status', 'na')}, bursts {obs.get('burst_count', 'na')}/{obs.get('min_burst_count', 'na')}, "
                         f"change {_num(obs.get('change_pct'), signed=True, suffix='%')} vs target {_num(obs.get('target_gain_pct'), suffix='%')}, ready {_yn(obs.get('is_ready'))}")
    legacy = state.get("legacy_advisory") or {}
    lines.append(f"legacy strategy: {legacy.get('signal', 'na')} - {_clean_text(legacy.get('reason'))}")
    lines += [_btc(state.get("btc_market")), _portfolio_line(state), _session_costs(state)]
    return lines


def _portfolio_stage_lines(state: dict) -> list[str]:
    o = state.get("market_overview") or {}
    g = state.get("guardrails") or {}
    size, rising = o.get("universe_size"), o.get("rising_count")
    share = rising / size * 100 if isinstance(size, int) and size > 0 and isinstance(rising, int) else None
    p = state.get("portfolio") or {}
    positions = sorted((x for x in p.get("open_positions") or [] if isinstance(x, dict)),
                       key=lambda x: x.get("unrealized_pnl_pct") or 0)
    held = ", ".join(f"{x.get('symbol')} {_num(x.get('unrealized_pnl_pct'), signed=True, suffix='%')}" for x in positions[:8]) or "none"
    return [f"market breadth: {size if size is not None else 'na'} symbols, rising {rising if rising is not None else 'na'} ({_num(share, 0, suffix='%')}), "
            f"falling {o.get('falling_count', 'na')}, dump flags {o.get('dump_flag_count', 'na')}",
            _portfolio_line(state), f"positions: {held}",
            f"portfolio loss limit -{_num(g.get('portfolio_loss_limit_pct'))}%, realized {_num(p.get('realized_pnl'))} TRY, unrealized {_num(p.get('unrealized_pnl'))} TRY",
            _btc(state.get("btc_market")), _session_costs(state)]


def render_state(state: dict) -> str:
    stage = stage_of(state)
    lines = [f"stage: {stage}"] + (_portfolio_stage_lines(state) if stage == "portfolio" else _symbol_lines(state, stage))
    return "\n".join(lines)


def laya_question(question: dict) -> dict:
    """Drop the shared LLM-oriented preamble: it is identical for every question and would
    otherwise consume the option budget, truncating the part that identifies the question."""
    q = dict(question)
    ins = q.get("instructions")
    if isinstance(ins, str) and ins.startswith(COMMON):
        q["instructions"] = ins[len(COMMON):]
    return q


def laya_questions(questions: dict) -> dict:
    return {qid: laya_question(q) for qid, q in questions.items()}


def question_key(question: dict) -> str:
    """Identity of a question as the model reads it; used for training coverage."""
    q = laya_question(question)
    body = {"type": q.get("type"), "instructions": q.get("instructions"), "criteria": q.get("criteria")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
