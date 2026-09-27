"""Concurrency and deadline guarantees, with no real network or exchange calls."""
import sqlite3
import json
import threading
import time

import pytest

from config import BotConfig
from decision.fixtures import FixtureDecisionProvider
from decision.inference import InferenceRunner
from decision.journal import DecisionJournal
from decision.openrouter import OpenRouterJevProvider, ProviderError
from decision.questions import portfolio_questions
from jev_helpers import make_bot, advance, records


def background_step(bot):
    errors = []
    def run():
        try:
            bot.step()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def test_hard_stop_runs_while_inference_is_still_blocked(tmp_path, monkeypatch):
    b, c, feed, provider = make_bot(tmp_path)
    entered, release, sold = threading.Event(), threading.Event(), threading.Event()
    worker = None
    try:
        b.step()
        provider.choices["action"] = "HOLD"
        def block(state, questions, response):
            if state.get("position"):
                entered.set()
                assert release.wait(5)
        provider.hook = block
        original_sell = b.simulator.sell
        def sell(*args, **kwargs):
            result = original_sell(*args, **kwargs)
            sold.set()
            return result
        monkeypatch.setattr(b.simulator, "sell", sell)
        worker, errors = background_step(b)
        assert entered.wait(2)
        advance(feed, seconds=1, price=80)
        assert sold.wait(2), "Hard stop waited for the model response"
        assert not release.is_set()
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert not b.simulator.positions and records(c, "hard_position_stop")
        assert c.inference.busy
        calls = len(provider.calls)
        b.step()
        assert len(provider.calls) == calls, "A second worker was allowed while the first was stuck"
    finally:
        release.set()
        if worker: worker.join(3)
        if c.inference.task: c.inference.task.done.wait(3)
        c.close()


def test_operator_stop_and_close_do_not_wait_for_late_model(tmp_path, monkeypatch):
    b, c, feed, provider = make_bot(tmp_path)
    entered, release = threading.Event(), threading.Event()
    worker = None
    try:
        b.step()
        provider.choices["action"] = "SELL_PARTIAL"
        def block(state, questions, response):
            if state.get("position"):
                entered.set()
                assert release.wait(5)
        provider.hook = block
        monkeypatch.setattr(b, "_generate_final_report", lambda: {})
        worker, errors = background_step(b)
        assert entered.wait(2)
        started = time.monotonic()
        b.stop()
        assert time.monotonic() - started < 2
        assert not release.is_set() and not b.simulator.positions
        assert len(b.simulator.closed_trades) == 1
        assert b.simulator.closed_trades[0]["decision_source"] == "session"
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert c.finished and not c.faulted
        task = c.inference.task
        c.close()  # SQLite closes while the old provider is deliberately still waiting.
        with sqlite3.connect(c.cfg.decision.database_path) as db:
            before = db.execute("SELECT count(*) FROM events").fetchone()[0]
        release.set()
        assert task.done.wait(2)
        with sqlite3.connect(c.cfg.decision.database_path) as db:
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == before
        assert len(b.simulator.closed_trades) == 1
    finally:
        release.set()
        if worker: worker.join(3)
        if c.inference.task: c.inference.task.done.wait(3)
        c.close()


def test_cancelled_completed_response_cannot_authorize_a_trade(tmp_path):
    b, c, feed, provider = make_bot(tmp_path)
    entered, release = threading.Event(), threading.Event()
    worker = None
    try:
        b.step()
        provider.choices["action"] = "SELL"
        def block(state, questions, response):
            if state.get("position"):
                entered.set()
                release.wait(5)
        provider.hook = block
        worker, errors = background_step(b)
        assert entered.wait(2)
        c.inference.cancel()
        release.set()  # Completion races cancellation, without changing bot.is_running.
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert b.simulator.positions and not b.simulator.closed_trades
    finally:
        release.set()
        if worker: worker.join(3)
        if c.inference.task: c.inference.task.done.wait(3)
        c.close()


def test_provider_close_is_deferred_and_called_once():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Provider(FixtureDecisionProvider):
        closes = 0
        def evaluate(self, state, questions, *, on_attempt=None):
            entered.set()
            release.wait(5)
            return super().evaluate(state, questions, on_attempt=on_attempt)
        def close(self):
            self.closes += 1
            closed.set()
    provider = Provider()
    runner = InferenceRunner(provider)
    task = runner.submit({}, portfolio_questions(), time.monotonic() + 8)
    try:
        assert entered.wait(2)
        runner.close()
        assert not closed.is_set() and task.cancelled.is_set()
        release.set()
        assert closed.wait(2)
        runner.close()
        assert provider.closes == 1
    finally:
        release.set()
        task.done.wait(3)
        runner.close()


def test_early_budget_stop_closes_replay_input(tmp_path, monkeypatch):
    import decision.replay as replay
    from jev_helpers import START, candles_before, ScriptProvider
    cfg = BotConfig()
    cfg.decision.engine = "jev"
    cfg.decision.database_path = str(tmp_path / "early-stop.sqlite3")
    cfg.auth.enabled = False
    frames = [{"as_of": START + i * 60, "markets": {"SOL_TRY": {
        "bid": 100, "ask": 100.1,
        "closed_candles": candles_before(START) if i == 0 else []}}} for i in range(3)]
    path = tmp_path / "input.jsonl"
    path.write_text("\n".join(json.dumps(frame) for frame in frames), encoding="utf-8")
    retained = []
    original = replay.iter_frames
    def track(path):
        generator = original(path)
        retained.append(generator)  # Prevent refcount/GC from accidentally masking the missing close.
        return generator
    monkeypatch.setattr(replay, "iter_frames", track)
    provider = ScriptProvider({"action": "WAIT"})
    provider.call_budget_reached = True
    summary = replay.run_replay(cfg, path, provider=provider)
    assert summary["frames_processed"] == 1
    assert retained[0].gi_frame is None
    path.unlink()  # Also verifies Windows handle release.


def test_wall_deadline_expires_even_when_replay_clock_is_frozen(tmp_path):
    def configure(cfg):
        cfg.decision.max_decision_age_seconds = .15
    b, c, feed, provider = make_bot(tmp_path, configure=configure)
    entered, release = threading.Event(), threading.Event()
    def block(state, questions, response):
        entered.set()
        assert release.wait(5)
    provider.hook = block
    try:
        started = time.monotonic()
        b.step()
        assert time.monotonic() - started < 2 and entered.is_set()
        assert c.inference.busy and not b.simulator.positions
        assert not c.faulted
        events = [e for r in records(c) for e in r["events"]]
        assert any(e["kind"] == "inference_abandoned" for e in events)
        assert any(e["data"].get("error_code") == "decision_expired" for e in events)
    finally:
        release.set()
        c.inference.task.done.wait(3)
        c.close()


def test_schema_retry_shares_original_wall_deadline(tmp_path):
    def configure(cfg):
        cfg.decision.max_decision_age_seconds = .3
    b, c, feed, provider = make_bot(tmp_path, configure=configure)
    release = threading.Event()
    calls = []
    def block(state, questions, response):
        if "action" not in questions:
            return
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(.15)
            response["answers"] = {}
        else:
            release.wait(5)
    provider.hook = block
    try:
        b.step()
        assert len(calls) == 2
        assert time.monotonic() - calls[0] < .6
        assert not b.simulator.positions
        row = records(c, "candidate")[0]
        assert any(e["kind"] == "decision_retry" for e in row["events"])
        assert any(e["kind"] == "inference_abandoned" for e in row["events"])
    finally:
        release.set()
        if c.inference.task: c.inference.task.done.wait(3)
        c.close()


@pytest.mark.parametrize("mode", ["expired", "cancelled"])
def test_transport_never_retries_after_late_response_or_cancel(mode):
    cfg = BotConfig().decision
    cfg.api_key = "audit-test-key"
    elapsed = [0.0]
    cancelled = threading.Event()
    requests = []
    class Response:
        status_code = 200
        content = b"{}"
        def json(self):
            return {}
    class Session:
        def post(self, *args, **kwargs):
            requests.append(kwargs)
            if mode == "expired": elapsed[0] = 9
            else: cancelled.set()
            return Response()
    p = OpenRouterJevProvider(cfg, session=Session(), monotonic=lambda: elapsed[0])
    with pytest.raises(ProviderError, match="deadline|cancelled"):
        p.evaluate_with_deadline({}, portfolio_questions(), deadline=8, cancelled=cancelled.is_set)
    assert len(requests) == 1


def test_flatten_rechecks_expiry_for_each_position(tmp_path, monkeypatch):
    b, c, feed, provider = make_bot(tmp_path, symbols=("AAA_TRY", "BBB_TRY"))
    try:
        b.step()
        assert len(b.simulator.positions) == 2
        provider.choices["portfolio_action"] = "FLATTEN"
        armed = False
        def arm(state, questions, response):
            nonlocal armed
            if "portfolio_action" in questions: armed = True
            if "action" in questions:
                choice = "HOLD" if state.get("position") else "WAIT"
                response["answers"]["action"].update(
                    choice=choice, probabilities={k: float(k == choice) for k in questions["action"]["criteria"]})
        provider.hook = arm
        original = b.simulator.sell
        def sell(*args, **kwargs):
            nonlocal armed
            result = original(*args, **kwargs)
            if armed:
                armed = False
                advance(feed, seconds=9, price=101)
            return result
        monkeypatch.setattr(b.simulator, "sell", sell)
        b.step()
        assert len(b.simulator.positions) == 1 and len(b.simulator.closed_trades) == 1
        row = records(c, "portfolio")[-1]
        assert any("decision_expired" in e.get("override_reasons", []) for e in row["execution"])
        # Expired MODEL instructions do not disable emergency/session liquidation.
        c.end_session()
        assert not b.simulator.positions
    finally:
        c.close()


@pytest.mark.parametrize("fresh", [None, False, True])
@pytest.mark.parametrize("equity", [1000, float("nan"), float("inf"), True, 0])
def test_portfolio_labels_require_explicit_fresh_finite_reference(tmp_path, fresh, equity):
    store = DecisionJournal(str(tmp_path / "labels.sqlite3"), clock=lambda: 100)
    try:
        run = store.create_run({})
        state = {"as_of": 100, "portfolio": {"total_equity": equity}}
        if fresh is not None: state["portfolio_marks_fresh"] = fresh
        store.begin_decision(run, "portfolio", state, {}, "test", horizons=[60])
        status = store.db.execute("SELECT status FROM outcomes").fetchone()[0]
        assert status == ("pending" if fresh is True and equity == 1000 else "no_reference")
    finally:
        store.close()
