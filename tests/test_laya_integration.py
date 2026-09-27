"""Laya engine: rendering, provider contract, batched scheduling and the expected-value policy.
No checkpoint is loaded; the model is replaced by deterministic fakes."""
import copy
import json

import pytest

from config import BotConfig
from decision.contracts import validate_config, validate_response
from decision.laya_provider import LayaDecisionProvider, jev_confidence
from decision.laya_state import STATE_FORMAT, laya_questions, question_key, render_state, stage_of
from decision.openrouter import ProviderError
from decision.questions import (COMMON, FORECAST_ID, FORECAST_LEVELS, forecast_level, portfolio_questions,
                                prebuy_questions, symbol_questions)
import jev_helpers as helpers
from jev_helpers import ScriptProvider, advance, make_bot

FRACTIONS = {"SMALL": 0.25, "HALF": 0.5, "FULL": 1.0}


def candidate_state(**market):
    candles = [{"open": 100 + i * .01, "high": 100.1 + i * .01, "low": 99.9 + i * .01, "close": 100 + i * .01,
                "volume": 5.0, "open_time": 1000 + 60 * i, "close_time": 1059.999 + 60 * i} for i in range(40)]
    return {"as_of": 4000.0, "market": {"symbol": "SOL_TRY", "bid": 100.3, "ask": 100.4, "rsi": 55.0, "features_ready": True,
                                        "quote_valid": True, **market},
            "history": {"closed_candles": candles, "micro_ticks": [[3990.0, 100.2, 1e6], [3999.0, 100.3, 1e6]]},
            "guardrails": {"entry_block_reasons": [], "maximum_entry_budget_try": 2000.0, "max_open_positions": 5},
            "portfolio": {"total_equity": 10000.0, "cash": 10000.0, "open_positions_count": 0},
            "legacy_advisory": {"signal": "HOLD", "reason": "⚠️ Aşırı Alım (RSI: 70 > 68)"},
            "costs": {"fee_rate_pct": 0.1, "slippage_bps": 0.0}, "session": {"remaining_seconds": 600}}


# ---- rendering ----------------------------------------------------------------------------
def test_render_is_deterministic_compact_and_timestamp_free():
    state = candidate_state()
    text = render_state(state)
    assert text == render_state(copy.deepcopy(state))
    assert text.startswith("stage: candidate") and "SOL_TRY" in text
    assert "4000" not in text and "1059" not in text, "absolute timestamps are noise and must not leak"
    assert "Aşırı Alım" in text and "⚠" not in text
    assert len(text) < 2500


def test_render_every_stage_and_missing_data():
    assert stage_of({"as_of": 1}) == "portfolio" and render_state({"as_of": 1}).startswith("stage: portfolio")
    pos = candidate_state(); pos["position"] = {"entry_price": 100.0, "unrealized_pnl_pct": 0.1, "invested_cost": 500}
    assert stage_of(pos) == "position" and "position: net pnl +0.10%" in render_state(pos)
    pre = candidate_state(); pre["proposal"] = {"as_of": 3998.5, "reference_ask": 100.3, "allocation": "SMALL", "budget_try": 500}
    assert stage_of(pre) == "prebuy" and "proposal: BUY SMALL" in render_state(pre)
    bare = {"as_of": 1.0, "market": {"symbol": "X_TRY", "bid": None, "ask": float("nan")}}
    assert "price na" in render_state(bare)


def test_question_adaptation_and_coverage_keys():
    qs = symbol_questions(False, FRACTIONS, forecast=True)
    adapted = laya_questions(qs)
    assert all(not q["instructions"].startswith(COMMON) for q in adapted.values())
    assert adapted["action"]["criteria"] == qs["action"]["criteria"]
    held = symbol_questions(True, FRACTIONS)
    assert question_key(qs["action"]) != question_key(held["action"])
    assert question_key(qs[FORECAST_ID]) == question_key(prebuy_questions(forecast=True)[FORECAST_ID])
    assert FORECAST_ID not in symbol_questions(False, FRACTIONS) and FORECAST_ID not in prebuy_questions()


def test_forecast_levels_cover_the_real_line():
    assert [forecast_level(v) for v in (-3, -0.3, -0.1, 0.0, 0.1, 0.3, 3)] == list(range(len(FORECAST_LEVELS)))


# ---- provider contract ----------------------------------------------------------------------
class FakeAgent:
    device = "cpu"

    def __init__(self):
        self.calls = []

    def predict_batch(self, texts, questions, **kwargs):
        self.calls.append((list(texts), kwargs))
        out = []
        for _ in texts:
            answers = {}
            for qid, q in questions.items():
                if q["type"] == "choice":
                    keys = list(q["criteria"])
                    probs = {k: (0.7 if i == 0 else 0.3 / (len(keys) - 1)) for i, k in enumerate(keys)}
                    answers[qid] = {"type": "choice", "choice": keys[0], "probabilities": probs, "confidence": 0.4, "answer_confidence": 0.7}
                elif q["type"] == "score":
                    n = len(q["criteria"])
                    probs = {str(i): (0.5 if i == n - 1 else 0.5 / (n - 1)) for i in range(n)}
                    answers[qid] = {"type": "score", "score": sum(i * p for i, p in enumerate(probs.values())), "probabilities": probs,
                                    "legend": {str(i): c for i, c in enumerate(q["criteria"])}, "confidence": 0.3, "answer_confidence": 0.5}
                else:
                    answers[qid] = {"type": "noul", "noul": 0.8, "confidence": 0.8, "answer_confidence": 0.8}
            out.append({"model": "laya-rl-agent", "answers": answers, "usage": {"input_tokens": 600, "output_tokens": 0}})
        return out


def make_checkpoint(tmp_path, coverage_questions=(), **extra):
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "model.safetensors").write_bytes(b"not-real-weights")
    meta = {"model_name": "laya-test", "bot_state_format": STATE_FORMAT,
            "bot_question_coverage": {question_key(q): {"examples": 10} for q in coverage_questions},
            "bot_forecast": {"level_values_pct": [-1.0, -0.3, -0.1, 0.0, 0.1, 0.3, 1.0]}, **extra}
    (ckpt / "rl_agent_config.json").write_text(json.dumps(meta), encoding="utf-8")
    cfg = BotConfig().decision
    cfg.engine, cfg.laya_checkpoint = "laya", str(ckpt)
    return cfg


def test_provider_output_passes_jev_contract_with_jev_confidence(tmp_path):
    qs = symbol_questions(False, FRACTIONS, forecast=True)
    cfg = make_checkpoint(tmp_path, qs.values())
    provider = LayaDecisionProvider(cfg, agent=FakeAgent())
    attempts = []
    response = provider.evaluate(candidate_state(), qs, on_attempt=attempts.append)
    validated = validate_response(response, qs)
    action = validated["answers"]["action"]
    assert action["confidence"] == jev_confidence(action["probabilities"].values()) == pytest.approx((4 * .7 - 1) / 3, abs=1e-4)
    assert action["trained_examples"] == 10 and response["usage"]["cost"] == 0.0
    forecast = validated["answers"][FORECAST_ID]
    expected = sum(float(forecast["probabilities"][str(i)]) * v for i, v in enumerate([-1.0, -0.3, -0.1, 0.0, 0.1, 0.3, 1.0]))
    assert forecast["expected_mid_return_pct"] == pytest.approx(expected, abs=1e-4)
    assert attempts and attempts[0]["network_used"] is False and attempts[0]["status"] == "received"
    assert provider.model == "laya-local/laya-test"


def test_batch_shares_forward_passes_and_keeps_order(tmp_path):
    cand, held = symbol_questions(False, FRACTIONS, forecast=True), symbol_questions(True, FRACTIONS, forecast=True)
    agent = FakeAgent()
    provider = LayaDecisionProvider(make_checkpoint(tmp_path, [*cand.values(), *held.values()]), agent=agent)
    states = [candidate_state(symbol=s) for s in ("A_TRY", "B_TRY", "C_TRY")]
    out = provider.evaluate_batch([(states[0], cand), (states[1], held), (states[2], cand)])
    assert len(agent.calls) == 2 and len(agent.calls[0][0]) == 2  # two question sets -> two forward groups
    assert set(out[1]["answers"]["action"]["probabilities"]) == set(held["action"]["criteria"])
    assert "A_TRY" in agent.calls[0][0][0] and "C_TRY" in agent.calls[0][0][1]


def test_untrained_questions_abstain_and_deadline(tmp_path):
    cand, held = symbol_questions(False, FRACTIONS), symbol_questions(True, FRACTIONS)
    cfg = make_checkpoint(tmp_path, cand.values())
    cfg.laya_untrained_questions = "abstain"
    provider = LayaDecisionProvider(cfg, agent=FakeAgent())
    out = provider.evaluate_batch([(candidate_state(), cand), (candidate_state(), held)])
    assert isinstance(out[1], ProviderError) and out[1].code.startswith("untrained_question:")
    validate_response(out[0], cand)
    with pytest.raises(ProviderError, match="decision_deadline_exceeded"):
        provider.evaluate_batch([(candidate_state(), cand)], deadline=0)


def test_checkpoint_preflight(tmp_path):
    cfg = BotConfig().decision
    cfg.engine, cfg.laya_checkpoint = "laya", str(tmp_path / "missing")
    with pytest.raises(ProviderError, match="laya_checkpoint_missing"):
        LayaDecisionProvider.verify_checkpoint(cfg)
    cfg = make_checkpoint(tmp_path, bot_state_format="old-format")
    with pytest.raises(ProviderError, match="state_format_mismatch"):
        LayaDecisionProvider.verify_checkpoint(cfg)


@pytest.mark.parametrize("field,value", [("laya_device", "gpu0"), ("laya_untrained_questions", "guess"), ("laya_batch_size", 0),
                                         ("entry_policy", "yolo"), ("market_fetch_workers", 0), ("min_expected_edge_pct", float("nan"))])
def test_laya_config_validation(field, value):
    cfg = BotConfig(); cfg.decision.engine = "laya"
    validate_config(cfg)
    setattr(cfg.decision, field, value)
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_expected_value_policy_requires_laya():
    cfg = BotConfig(); cfg.decision.engine = "jev"; cfg.decision.entry_policy = "expected_value"
    with pytest.raises(ValueError, match="laya"):
        validate_config(cfg)


# ---- controller: batched evaluation and the expected-value policy ----------------------------
class LayaScript(ScriptProvider):
    """Fixture answers plus a controllable 5-minute forecast; supports batched evaluation."""
    name = "laya-script"
    deterministic = True

    def __init__(self, expected):
        super().__init__()
        self.expected = expected
        self.batch_sizes = []

    def evaluate(self, state, questions, *, on_attempt=None):
        response = super().evaluate(state, questions, on_attempt=on_attempt)
        if FORECAST_ID in questions:
            n = len(questions[FORECAST_ID]["criteria"])
            response["answers"][FORECAST_ID] = {
                "type": "score", "score": 3.0, "confidence": 1.0, "probabilities": {str(i): float(i == 3) for i in range(n)},
                "legend": {str(i): c for i, c in enumerate(questions[FORECAST_ID]["criteria"])},
                "expected_mid_return_pct": self.expected.get(state["market"]["symbol"], 0.0)}
        return response

    def evaluate_batch(self, requests, *, deadline=None, cancelled=lambda: False, on_attempt=None):
        self.batch_sizes.append(len(requests))
        return [self.evaluate(s, q, on_attempt=on_attempt) for s, q in requests]


def laya_bot(tmp_path, expected, symbols):
    def configure(cfg):
        cfg.decision.engine = "laya"
        cfg.decision.batch_inference = True
        cfg.decision.entry_policy = cfg.decision.exit_policy = "expected_value"
        cfg.decision.min_expected_edge_pct = 0.1
        cfg.decision.max_candidates_per_step = 10
        cfg.trading.max_open_positions = cfg.trading.target_coins_count = 5
    original = helpers.ScriptProvider
    helpers.ScriptProvider = lambda choices=None: LayaScript(expected)
    try:
        return make_bot(tmp_path, configure=configure, symbols=symbols)
    finally:
        helpers.ScriptProvider = original


def test_batched_ev_entries_only_above_costs_and_best_first(tmp_path):
    # spread 0.1% + 2 x 0.1% fee = 0.3% cost; threshold 0.1% net edge
    expected = {"SOL_TRY": 0.2, "ETH_TRY": 0.6, "AVAX_TRY": 0.9}
    bot, ctl, feed, provider = laya_bot(tmp_path, expected, ("SOL_TRY", "ETH_TRY", "AVAX_TRY"))
    ctl.step()
    assert provider.batch_sizes == [3], "all due symbols in one provider call"
    held = [p["symbol"] for p in bot.simulator.positions.values()]
    assert sorted(held) == ["AVAX_TRY", "ETH_TRY"], "SOL clears no costs: no buying just to buy"
    buys = [o["symbol"] for o in bot.simulator.all_orders if o["side"] == "BUY"]
    assert buys == ["AVAX_TRY", "ETH_TRY"], "highest expected edge executes first"
    kinds = [r[0] for r in ctl.store.db.execute("SELECT kind FROM events WHERE run_id=?", (ctl.run_id,))]
    assert "entry_ranking" in kinds and kinds.count("expected_value_assessment") >= 3
    run = json.loads(ctl.store.db.execute("SELECT metadata_json FROM runs").fetchone()[0])
    assert run["engine"] == "laya" and run["scheduling"] == "batched" and run["policy_version"] == "laya-spot-policy-v1"


def test_no_edge_no_trade(tmp_path):
    bot, ctl, feed, provider = laya_bot(tmp_path, {"SOL_TRY": 0.1, "ETH_TRY": -0.2}, ("SOL_TRY", "ETH_TRY"))
    ctl.step()
    assert not bot.simulator.positions and not bot.simulator.all_orders


def test_ev_exit_sells_when_expected_hold_return_is_negative(tmp_path):
    expected = {"SOL_TRY": 0.9, "ETH_TRY": 0.9}
    bot, ctl, feed, provider = laya_bot(tmp_path, expected, ("SOL_TRY", "ETH_TRY"))
    ctl.step()
    assert len(bot.simulator.positions) == 2
    expected.update(SOL_TRY=-0.4, ETH_TRY=0.2)
    advance(feed, seconds=20, price=100.0)
    ctl.step()
    held = sorted(p["symbol"] for p in bot.simulator.positions.values())
    assert held == ["ETH_TRY"]
    sells = [o for o in bot.simulator.all_orders if o["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["symbol"] == "SOL_TRY"


def test_portfolio_low_confidence_policy_is_explicit(tmp_path):
    def configure(cfg):
        cfg.decision.portfolio_low_confidence_policy = "CONTINUE"
        cfg.decision.min_action_confidence = 0.99
    bot, ctl, feed, provider = make_bot(tmp_path, choices={"portfolio_action": "PAUSE_ENTRIES"}, configure=configure)
    provider.confidence = 0.5  # an unsure PAUSE answer
    ctl.step()
    assert ctl.portfolio_policy == "CONTINUE"


# ---- dataset builder ----------------------------------------------------------------------
class TeacherScript(ScriptProvider):
    """Stands in for recorded Jev runs: a non-fixture teacher provider name."""
    name = "openrouter-systemone"
    model = "typesafe/jev-test"
    is_fixture = False


def journal(tmp_path, provider_cls, minutes=7):
    original = helpers.ScriptProvider
    helpers.ScriptProvider = lambda choices=None: provider_cls(choices)
    try:
        bot, ctl, feed, provider = make_bot(tmp_path, symbols=("SOL_TRY", "ETH_TRY"))
    finally:
        helpers.ScriptProvider = original
    ctl.step()
    for _ in range(minutes):
        advance(feed, seconds=60, price=100.2)
        ctl.step()
    ctl.end_session(reason="test_end")
    path = ctl.store.path
    ctl.close()
    return path


def test_dataset_builder_teacher_outcome_and_synthetic_states(tmp_path):
    from decision.laya_dataset import build_dataset
    db = journal(tmp_path / "teacher", TeacherScript)
    manifest = build_dataset([db], tmp_path / "ds", test_runs=0, calib_fraction=0.0)
    rows = [json.loads(line) for line in (tmp_path / "ds" / "dataset.jsonl").open(encoding="utf-8")]
    teacher = [r for r in rows if r["supervision"] == "teacher"]
    outcome = [r for r in rows if r["supervision"] == "outcome"]
    assert teacher and outcome and {r["split"] for r in rows} == {"train"}
    assert {r["question_id"] for r in outcome} == {FORECAST_ID}
    assert {"recorded", "synthetic_prebuy"} <= {r["variant"] for r in outcome}
    assert all(abs(sum(r["target"]) - 1) < 1e-9 for r in rows)
    assert all(r["state_text"].startswith("stage: ") for r in rows)
    assert {"candidate", "prebuy", "position"} & {r["stage"] for r in teacher}
    assert len(manifest["forecast"]["level_values_pct"]) == len(FORECAST_LEVELS)
    # No future information in any model input.
    for r in outcome:
        assert str(r["outcome"]["mid_return_pct"]) not in r["state_text"] or r["outcome"]["mid_return_pct"] == 0


def test_dataset_builder_excludes_fixture_runs(tmp_path):
    from decision.laya_dataset import build_dataset
    db = journal(tmp_path / "fixture", ScriptProvider, minutes=1)
    manifest = build_dataset([db], tmp_path / "ds")
    assert manifest["rows"] == 0 and manifest["sources"][0]["runs"] == 0


def test_own_laya_runs_give_outcome_labels_but_never_teacher_targets(tmp_path):
    from decision.laya_dataset import build_dataset

    class OwnRun(TeacherScript):
        name = "laya-local"
    db = journal(tmp_path / "own", OwnRun)
    assert build_dataset([db], tmp_path / "a")["rows"] == 0
    build_dataset([db], tmp_path / "b", outcome_providers=("laya-local",), test_runs=0, calib_fraction=0.0)
    rows = [json.loads(line) for line in (tmp_path / "b" / "dataset.jsonl").open(encoding="utf-8")]
    assert rows and {r["supervision"] for r in rows} == {"outcome"}


def test_operator_opened_position_is_managed_by_the_ev_exit(tmp_path):
    expected = {"SOL_TRY": -0.5}  # the model would never buy this, and wants out of it
    bot, ctl, feed, provider = laya_bot(tmp_path, expected, ("SOL_TRY",))
    pos = ctl.manual_buy("SOL_TRY", 500.0, "operator test")
    assert pos is not None and bot.simulator.positions
    advance(feed, seconds=10, price=100.0)
    ctl.step()
    assert not bot.simulator.positions
    sells = [o for o in bot.simulator.all_orders if o["side"] == "SELL"]
    assert len(sells) == 1
    sources = {r[0] for r in ctl.store.db.execute("SELECT source FROM decisions")}
    assert {"operator", "fixture"} <= sources  # operator entry, model (test fixture) exit


def test_time_split_holds_out_the_end_of_one_long_session_with_a_gap(tmp_path):
    from decision.laya_dataset import build_dataset

    class OwnRun(TeacherScript):
        name = "laya-local"
    db = journal(tmp_path / "night", OwnRun, minutes=30)
    manifest = build_dataset([db], tmp_path / "ds", outcome_providers=("laya-local",), test_hours=0.1, calib_fraction=0.0)
    splits = manifest["counts"]["decisions"]
    assert splits.get("test") and splits.get("train") and splits.get("gap"), splits
    rows = [json.loads(line) for line in (tmp_path / "ds" / "dataset.jsonl").open(encoding="utf-8")]
    cut, gap = manifest["test_split"]["cut_as_of"], manifest["test_split"]["gap_seconds"]
    assert all(r["as_of"] < cut - gap for r in rows if r["split"] == "train")
    assert all(r["as_of"] >= cut for r in rows if r["split"] == "test")
