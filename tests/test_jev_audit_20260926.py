"""Regression coverage for the four defects found in the 2026-09-26 audit.

Run through tools/run_offline_tests.py with networking and exchange orders disabled.
"""
import importlib

import pytest

from jev_helpers import make_bot, advance, records
from decision.openrouter import ProviderError


@pytest.mark.parametrize("failure", ["low_confidence", "timeout", "invalid", "expired"])
def test_loss_during_failed_position_decision_is_closed_in_same_step(tmp_path, failure):
    b, c, feed, provider = make_bot(tmp_path)
    try:
        b.step()
        assert len(b.simulator.positions) == 1
        provider.choices["action"] = "HOLD"

        def crash_during_inference(state, questions, response):
            if state.get("position") is None:
                return
            advance(feed, seconds=9 if failure == "expired" else 1, price=80)
            if failure == "low_confidence":
                response["answers"]["action"]["confidence"] = .1
            elif failure == "timeout":
                raise ProviderError("network_or_timeout")
            elif failure == "invalid":
                response["answers"] = {}

        provider.hook = crash_during_inference
        b.step()
        assert not b.simulator.positions, "Fresh 20% loss remains open after the failed model decision"
        assert records(c, "hard_position_stop")
    finally:
        c.close()


@pytest.mark.parametrize("action", ["SELL", "SELL_PARTIAL", "ROLLOVER"])
def test_expired_position_action_cannot_execute_after_slow_quote(tmp_path, monkeypatch, action):
    b, c, feed, provider = make_bot(tmp_path)
    try:
        b.step()
        advance(feed, price=102)
        provider.choices["action"] = action
        armed = False

        def arm(state, questions, response):
            nonlocal armed
            if state.get("position"):
                armed = True

        original_quote = b.client.get_best_prices

        def slow_quote(symbol):
            nonlocal armed
            if armed:
                armed = False
                advance(feed, seconds=9, price=102)
            return original_quote(symbol)

        provider.hook = arm
        monkeypatch.setattr(b.client, "get_best_prices", slow_quote)
        b.step()
        row = records(c, "position")[-1]
        assert feed.clock() - row["as_of"] > c.cfg.decision.max_decision_age_seconds
        fills = [e for e in row["events"] if e["kind"] in ("execution_result", "risk_reference_reset")]
        assert not fills, f"Expired {action} was still applied"
    finally:
        c.close()


def test_stale_portfolio_cannot_seed_equity_outcome_reference(tmp_path):
    b, c, feed, provider = make_bot(tmp_path)
    try:
        b.step()
        feed.clock.advance_to(feed.clock() + 6)
        provider.choices["action"] = "HOLD"
        b.step()
        row = records(c, "portfolio")[-1]
        assert row["input"]["state"]["portfolio_marks_fresh"] is False
        labels = [o for o in row["outcomes"] if o["name"] == "portfolio_equity"]
        assert labels and all(o["status"] == "no_reference" for o in labels)
    finally:
        c.close()


def test_valid_hold_cannot_override_loss_during_inference(tmp_path):
    b, c, feed, provider = make_bot(tmp_path)
    try:
        b.step()
        provider.choices["action"] = "HOLD"
        def crash(state, questions, response):
            if state.get("position"):
                advance(feed, seconds=1, price=80)
        provider.hook = crash
        b.step()
        assert not b.simulator.positions
        assert records(c, "hard_position_stop")
    finally:
        c.close()


def test_buy_still_rejects_decision_expired_during_quote_refresh(tmp_path, monkeypatch):
    b, c, feed, provider = make_bot(tmp_path)
    try:
        armed = False
        def arm(state, questions, response):
            nonlocal armed
            if "prebuy_authorization" in questions:
                armed = True
        original_quote = b.client.get_best_prices
        def slow_quote(symbol):
            nonlocal armed
            if armed:
                armed = False
                advance(feed, seconds=9, price=100)
            return original_quote(symbol)
        provider.hook = arm
        monkeypatch.setattr(b.client, "get_best_prices", slow_quote)
        b.step()
        assert not b.simulator.positions
        assert any("decision_expired" in e.get("override_reasons", [])
                   for r in records(c, "prebuy") for e in r["execution"])
    finally:
        c.close()


def test_provider_does_not_start_retry_after_total_decision_deadline(monkeypatch):
    import requests
    from config import BotConfig
    from decision.openrouter import OpenRouterJevProvider
    from decision.questions import portfolio_questions
    cfg = BotConfig().decision
    cfg.api_key = "audit-fake-credential"
    cfg.max_attempts = 3
    elapsed = [0.0]
    starts = []
    class Timeouts:
        def post(self, *args, **kwargs):
            starts.append(elapsed[0])
            elapsed[0] += cfg.request_timeout_seconds
            raise requests.Timeout()
    def sleep(seconds):
        elapsed[0] += seconds
    provider = OpenRouterJevProvider(cfg, session=Timeouts(), sleep=sleep, monotonic=lambda: elapsed[0])
    with pytest.raises(ProviderError):
        provider.evaluate({"as_of": 1770000000.0}, portfolio_questions())
    assert all(t < cfg.max_decision_age_seconds for t in starts), starts


def test_authenticated_web_lifecycle_with_paper_fills(tmp_path, monkeypatch):
    """Exercise real handlers; drive ticks explicitly for deterministic timing."""
    from fastapi.testclient import TestClient
    module = importlib.import_module("web.app")
    b, c, feed, provider = make_bot(tmp_path)
    b.is_running = False
    b.config.auth.enabled = True
    b.config.auth.username = "audit"
    b.config.auth.password = "local-test-only"
    monkeypatch.setattr(module, "bot_instance", b)
    monkeypatch.setattr(module, "active_sessions", set())
    # Thread startup alone is stubbed; start/stop/step/SQLite/report handlers are real.
    from types import SimpleNamespace
    class ManualThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr("bot.threading", SimpleNamespace(Thread=ManualThread))
    client = TestClient(module.app)
    try:
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/login", json={"username": "audit", "password": "local-test-only"}).status_code == 200
        assert client.get("/").status_code == 200
        assert client.post("/api/start", json={"duration_minutes": 15}).json()["status"] == "started"
        c = b._jev()
        b.step()
        assert len(b.simulator.positions) == 1
        state = client.get("/api/state").json()
        assert state["decision_engine"] == "jev"
        assert client.post("/api/config", json={"budget_per_trade": 100}).status_code == 409
        provider.choices["action"] = "SELL_PARTIAL"
        advance(feed, price=102)
        b.step()
        assert len(b.simulator.closed_trades) == 1 and b.simulator.positions
        result = client.post("/api/stop").json()
        assert result["status"] == "stopped" and "report" in result
        assert not b.simulator.positions and c.finished
        assert len(b.simulator.closed_trades) == 2
        assert client.get("/api/decisions/recent").status_code == 200
        assert client.post("/api/stop").json()["status"] == "already_stopped"
    finally:
        c.close()
