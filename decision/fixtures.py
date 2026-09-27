"""OFFLINE TEST FIXTURE. This is NOT Jev inference and NOT Jev training data."""
from __future__ import annotations


class FixtureDecisionProvider:
    name = "fixture-not-jev"
    model = "fixture/deterministic-test-v1"
    is_fixture = True

    def check_ready(self):
        pass

    def evaluate(self, state, questions, *, on_attempt=None):
        answers = {}
        ready = state.get("market", {}).get("features_ready", False)
        pos = state.get("position")
        for name, question in questions.items():
            if question["type"] == "choice":
                choices = question["criteria"]
                chosen = next(iter(choices))
                if name == "portfolio_action": chosen = "CONTINUE"
                elif name == "action":
                    if pos:
                        chosen = "SELL" if pos.get("unrealized_pnl_pct", 0) > 0.25 else "HOLD"
                    else:
                        chosen = "BUY" if ready else "OBSERVE"
                elif name == "prebuy_authorization": chosen = "EXECUTE"
                elif name == "allocation": chosen = "HALF" if "HALF" in choices else next(iter(choices))
                elif name == "reason_code": chosen = "TREND_CONTINUATION" if ready else "INSUFFICIENT_EVIDENCE"
                elif name == "regime": chosen = "UPTREND" if ready else "UNCLEAR"
                answers[name] = {"type": "choice", "choice": chosen, "confidence": 1.0,
                                 "probabilities": {key: float(key == chosen) for key in choices}}
            elif question["type"] == "score":
                score = len(question["criteria"]) - 1 if ready else 0
                answers[name] = {"type": "score", "score": score, "confidence": 1.0,
                                 "probabilities": {str(i): float(i == score) for i in range(len(question["criteria"]))},
                                 "legend": {str(i): text for i, text in enumerate(question["criteria"])}}
            else:
                answers[name] = {"type": "noul", "noul": 1.0 if ready else 0.0}
        if on_attempt:
            on_attempt({"attempt": 1, "status": "fixture", "latency_ms": 0, "network_used": False})
        return {"model": self.model, "provider": self.name, "answers": answers, "usage": {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}}
