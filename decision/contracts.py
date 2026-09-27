"""Transport-neutral contract. All trading arithmetic stays in deterministic code."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol

SCHEMA_VERSION = "jev-trading-dataset-v1"
POLICY_VERSION = "jev-spot-policy-v3"
SENSITIVE_KEYS = {"api_key", "secret_key", "password", "authorization", "access_token", "cookie", "headers"}


def clean(value: Any) -> Any:
    """Strict JSON + defense-in-depth secret removal; never serialize object reprs."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return re.sub(r"sk-or-[A-Za-z0-9_-]+", "[REDACTED]", value)
    raise TypeError(f"Non-JSON value of type {type(value).__name__}")


def dumps(value: Any) -> str:
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def code_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    paths = [root / "bot.py", root / "config.py"]
    for directory in ("core", "decision", "strategies"):
        paths.extend(sorted((root / directory).glob("*.py")))
    for path in sorted(paths):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def public_config(config) -> dict:
    """Only decision-relevant configuration, not API/auth/server settings."""
    return {name: asdict(getattr(config, name)) for name in ("trading", "strategy", "test", "decision")}


def finite_number(value: Any, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected finite number, not a string/bool/null")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError("Number outside permitted range")
    return float(value)


def validate_config(config) -> None:
    d, t, s = config.decision, config.trading, config.strategy
    if d.engine not in ("legacy", "jev"):
        raise ValueError("decision.engine must be legacy or jev")
    if d.endpoint != "https://openrouter.ai/api/v1/systemone":
        raise ValueError("Only the documented OpenRouter System One HTTPS endpoint is permitted")
    if not isinstance(d.database_path, str) or not d.database_path.strip():
        raise ValueError("decision.database_path must be a nonempty SQLite path")
    if not isinstance(d.model, str) or not d.model.strip():
        raise ValueError("decision.model is required")
    if not isinstance(d.api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", d.api_key_env):
        raise ValueError("api_key_env must name an environment variable, not contain a key")
    for v in (d.min_action_confidence,):
        finite_number(v, 0, 1)
    for v in (d.request_timeout_seconds, d.max_decision_age_seconds):
        finite_number(v, 0.05, 120)
    for v in (d.decision_interval_seconds, d.retry_backoff_seconds, d.outcome_max_lateness_seconds,
              d.entry_blackout_seconds, d.slippage_bps, d.max_prebuy_price_move_pct):
        finite_number(v, 0, 100000)
    if d.slippage_bps >= 10000:
        raise ValueError("slippage_bps must be smaller than 10000")
    for name, low, high in (("max_attempts", 1, 3), ("max_candidates_per_step", 1, 1000),
                            ("max_request_bytes", 1000, 125000), ("history_candles", 0, 500), ("history_ticks", 0, 300)):
        v = getattr(d, name)
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError(f"decision.{name} must be an integer in [{low}, {high}]")
    if not d.outcome_horizons_seconds or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in d.outcome_horizons_seconds):
        raise ValueError("Outcome horizons must be positive integer seconds")
    if len(set(d.outcome_horizons_seconds)) != len(d.outcome_horizons_seconds):
        raise ValueError("Duplicate outcome horizons")
    if not isinstance(d.allocation_fractions, dict) or not d.allocation_fractions or len(d.allocation_fractions) > 255:
        raise ValueError("allocation_fractions must contain 1..255 named sizes")
    for name, value in d.allocation_fractions.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,39}", name):
            raise ValueError("Allocation labels must be uppercase identifiers")
        finite_number(value, 0.00001, 1)
    for v in (t.budget_per_trade, t.initial_virtual_balance, t.max_market_data_age_seconds, t.max_candle_age_seconds,
              t.max_radar_age_seconds, s.stop_loss_pct, s.portfolio_stop_loss_pct):
        finite_number(v, 0.00001)
    finite_number(t.fee_rate_pct, 0, 99.999)
    finite_number(t.max_allowed_spread_pct, 0)
    finite_number(s.partial_tp_ratio, 0.00001, 0.99999)
    for v in (t.target_coins_count, t.max_open_positions):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError("Position limits must be positive integers")
    for v in (s.cooldown_seconds, s.symbol_cooldown_seconds, s.loss_cooldown_seconds):
        finite_number(v, 0)


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a nonempty map")
    for key, q in questions.items():
        if not isinstance(key, str) or not key or key.lower() in SENSITIVE_KEYS or not isinstance(q, dict) or not q.get("instructions"):
            raise ValueError("Invalid question")
        if q.get("type") == "choice":
            if not isinstance(q.get("criteria"), dict) or not 1 <= len(q["criteria"]) <= 255:
                raise ValueError("Choice requires a map of 1..255 criteria")
        elif q.get("type") == "score":
            if not isinstance(q.get("criteria"), list) or not 2 <= len(q["criteria"]) <= 10:
                raise ValueError("Score requires 2..10 ordered criteria")
        elif q.get("type") != "noul":
            raise ValueError("Unknown primitive")
    dumps(questions)


def validate_response(response: Any, questions: dict) -> dict:
    """Reject missing/extra answers, invalid labels, NaN and inconsistent distributions."""
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not response["model"]:
        raise ValueError("Response has no serving model")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Response question set differs from request")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Response usage is missing")
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid token usage")
    if "cost" in usage:
        finite_number(usage["cost"], 0)
    for key, question in questions.items():
        a = answers[key]
        if not isinstance(a, dict) or a.get("type") != question["type"]:
            raise ValueError(f"Wrong answer type: {key}")
        if a["type"] == "noul":
            finite_number(a.get("noul"), 0, 1)
            continue
        finite_number(a.get("confidence"), 0, 1)
        probs = a.get("probabilities")
        expected = set(question["criteria"]) if a["type"] == "choice" else {str(i) for i in range(len(question["criteria"]))}
        if not isinstance(probs, dict) or set(probs) != expected:
            raise ValueError(f"Wrong probability labels: {key}")
        for prob in probs.values():
            finite_number(prob, 0, 1)
        # The live provider rounds probabilities to two decimal places. Validate
        # that a normalized distribution exists inside those rounding intervals;
        # keep the original numbers in the journal, never renormalize evidence.
        rounded = all(abs(p * 100 - round(p * 100)) < 1e-9 for p in probs.values())
        epsilon = 0.005 if rounded else 0.002 / len(probs)
        bounds = {k: (max(0, p - epsilon), min(1, p + epsilon)) for k, p in probs.items()}
        if sum(lo for lo, hi in bounds.values()) > 1 + 1e-9 or sum(hi for lo, hi in bounds.values()) < 1 - 1e-9:
            raise ValueError(f"Probabilities do not sum to one: {key}")
        if a["type"] == "choice":
            if a.get("choice") not in expected or probs[a["choice"]] + 0.002 < max(probs.values()):
                raise ValueError(f"Choice is not a highest-probability allowed option: {key}")
        else:
            finite_number(a.get("score"), 0, len(expected) - 1)
            if not isinstance(a.get("legend"), dict) or set(a["legend"]) != expected:
                raise ValueError(f"Missing score legend: {key}")
            # Bound the weighted mean with the same unit-mass constraint. This
            # admits rounding only, not arbitrary score/probability mismatches.
            def extreme(reverse=False):
                mass = 1 - sum(lo for lo, hi in bounds.values())
                value = sum(int(k) * lo for k, (lo, hi) in bounds.items())
                for k in sorted(bounds, key=int, reverse=reverse):
                    lo, hi = bounds[k]
                    added = min(max(0, mass), hi - lo)
                    value += int(k) * added
                    mass -= added
                return value
            score_epsilon = 0.005 if rounded else 0.02
            if not extreme() - score_epsilon - 1e-9 <= a["score"] <= extreme(True) + score_epsilon + 1e-9:
                raise ValueError(f"Score differs from weighted probabilities: {key}")
    return clean(response)


class DecisionProvider(Protocol):
    """A later local classifier can implement this without changing execution/datasets."""
    name: str
    model: str
    def check_ready(self) -> None: ...
    def evaluate(self, state: dict, questions: dict, *, on_attempt: Callable[[dict], None] | None = None) -> dict: ...
