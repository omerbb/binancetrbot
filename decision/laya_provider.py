"""Local Laya inference behind the unchanged DecisionProvider contract.

No network, no API key and no per-call cost: every decision is one forward pass on this
machine, and many states can share one pass (`evaluate_batch`). Answers are converted to
exactly the schema `validate_response` accepts for Jev, so journaling, execution, exports
and the confidence gate are shared. `confidence` is recomputed with Jev's definition,
(n * p_max - 1) / (n - 1), so `min_action_confidence` keeps its meaning; Laya's own
entropy confidence and max-probability `answer_confidence` are kept alongside.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from decision.contracts import validate_questions
from decision.laya_state import STATE_FORMAT, laya_questions, question_key, render_state
from decision.openrouter import ProviderError
from decision.questions import FORECAST_ID, portfolio_questions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = ("rl_agent_config.json", "model.safetensors")

# One resident model per process: a restart of a bot session must not pay the load again.
_AGENTS: dict = {}
_DIGESTS: dict = {}
_LOCK = threading.Lock()


def resolve_checkpoint(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def read_checkpoint_config(path: Path) -> dict:
    if not path.is_dir():
        raise ProviderError("laya_checkpoint_missing")
    for name in REQUIRED_FILES:
        if not (path / name).is_file():
            raise ProviderError("laya_checkpoint_incomplete")
    try:
        return json.loads((path / "rl_agent_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProviderError("laya_checkpoint_config_unreadable") from None


def weights_digest(path: Path) -> str:
    weights = path / "model.safetensors"
    stamp = (str(weights), weights.stat().st_mtime, weights.stat().st_size)
    with _LOCK:
        if stamp not in _DIGESTS:
            h = hashlib.sha256()
            with weights.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 22), b""):
                    h.update(block)
            _DIGESTS[stamp] = h.hexdigest()
        return _DIGESTS[stamp]


def load_agent(path: Path, device: str = "auto", threads: int = 0):
    weights = path / "model.safetensors"
    stamp = (str(path), device, weights.stat().st_mtime)
    with _LOCK:
        agent = _AGENTS.get(stamp)
        if agent is not None:
            return agent
        try:
            import torch
            from laya.agent import Agent
        except ImportError:
            raise ProviderError("laya_runtime_not_installed") from None
        if threads:
            torch.set_num_threads(threads)
        agent = Agent(str(path), device=None if device == "auto" else device)
        _AGENTS.clear()  # keep a single resident checkpoint
        _AGENTS[stamp] = agent
        return agent


def unload_agents() -> None:
    with _LOCK:
        _AGENTS.clear()


def jev_confidence(probabilities) -> float:
    probabilities = list(probabilities)
    n = len(probabilities)
    if n < 2:
        return 1.0
    return round(min(1.0, max(0.0, (n * max(probabilities) - 1) / (n - 1))), 4)


class LayaDecisionProvider:
    name = "laya-local"
    deterministic = True  # the same input yields the same answer; retrying a schema failure is pointless
    is_fixture = False

    def __init__(self, config, *, agent=None, require_state_format=True, monotonic=time.monotonic):
        self.config = config
        self.path = resolve_checkpoint(config.laya_checkpoint)
        self.require_state_format = require_state_format
        self.monotonic = monotonic
        self.agent = agent
        try:
            self.meta = read_checkpoint_config(self.path)
        except ProviderError:
            self.meta = {}
        self.model = "laya-local/" + str(self.meta.get("model_name") or self.path.name)
        self.digest = None

    # ---- readiness -------------------------------------------------------------------------
    @staticmethod
    def verify_checkpoint(config, *, require_state_format=True) -> dict:
        """Cheap preflight: files and input format only; loads no weights."""
        meta = read_checkpoint_config(resolve_checkpoint(config.laya_checkpoint))
        if require_state_format and meta.get("bot_state_format") != STATE_FORMAT:
            raise ProviderError("laya_checkpoint_state_format_mismatch")
        return meta

    def check_ready(self) -> None:
        self.meta = self.verify_checkpoint(self.config, require_state_format=self.require_state_format)
        self.model = "laya-local/" + str(self.meta.get("model_name") or self.path.name)
        if self.agent is None:
            self.agent = load_agent(self.path, self.config.laya_device, self.config.laya_threads)
            # First CUDA call compiles kernels; pay it now, not inside a decision deadline.
            self.agent.predict_batch(["stage: portfolio"], laya_questions(portfolio_questions()), **self._len_kwargs())
        if self.digest is None and (self.path / "model.safetensors").is_file():
            self.digest = weights_digest(self.path)

    def run_metadata(self) -> dict:
        forecast = self.meta.get("bot_forecast") or {}
        return {"laya": {"checkpoint": str(self.path), "weights_sha256": self.digest,
                         "state_format": STATE_FORMAT, "base_model": self.meta.get("base_model"),
                         "dataset_sha256": (self.meta.get("bot_dataset") or {}).get("sha256"),
                         "device": str(getattr(self.agent, "device", "not_loaded")),
                         "forecast_level_values_pct": forecast.get("level_values_pct"),
                         "calibrated_entry_min_expected_edge_pct": (self.meta.get("bot_policy") or {}).get("entry_min_expected_edge_pct"),
                         "network_used_for_decisions": False}}

    def policy_defaults(self) -> dict:
        return dict(self.meta.get("bot_policy") or {})

    # ---- inference -------------------------------------------------------------------------
    def _len_kwargs(self) -> dict:
        return {"max_len": self.config.laya_max_len} if self.config.laya_max_len else {}

    def coverage(self, question: dict) -> int:
        entry = (self.meta.get("bot_question_coverage") or {}).get(question_key(question)) or {}
        return int(entry.get("examples", 0))

    def evaluate(self, state: dict, questions: dict, *, on_attempt=None) -> dict:
        return self.evaluate_with_deadline(state, questions, on_attempt=on_attempt,
                                          deadline=self.monotonic() + self.config.max_decision_age_seconds)

    def evaluate_with_deadline(self, state, questions, *, deadline, cancelled=lambda: False, on_attempt=None) -> dict:
        result = self.evaluate_batch([(state, questions)], deadline=deadline, cancelled=cancelled, on_attempt=on_attempt)[0]
        if isinstance(result, Exception):
            raise result
        return result

    def evaluate_batch(self, requests, *, deadline=None, cancelled=lambda: False, on_attempt=None) -> list:
        """Answer many (state, questions) pairs; requests sharing a question set share forward
        passes. Returns one response or ProviderError per request, in input order."""
        if deadline is None:
            deadline = self.monotonic() + self.config.max_decision_age_seconds

        def remaining():
            if cancelled():
                raise ProviderError("inference_cancelled")
            if deadline - self.monotonic() <= 0:
                raise ProviderError("decision_deadline_exceeded")

        remaining()
        if self.agent is None:
            self.check_ready()
        out: list = [None] * len(requests)
        groups: dict = {}
        for i, (state, questions) in enumerate(requests):
            validate_questions(questions)
            untrained = [qid for qid, q in questions.items() if self.coverage(q) == 0]
            if untrained and self.config.laya_untrained_questions == "abstain":
                out[i] = ProviderError("untrained_question:" + untrained[0])
                continue
            key = json.dumps(questions, sort_keys=True, ensure_ascii=False)
            groups.setdefault(key, (questions, []))[1].append(i)
        record = {"attempt": 1, "network_used": False, "batch_size": len(requests), "forward_groups": len(groups),
                  "device": str(getattr(self.agent, "device", "unknown"))}
        started = self.monotonic()
        try:
            for questions, indices in groups.values():
                remaining()
                results = self.agent.predict_batch([render_state(requests[i][0]) for i in indices], laya_questions(questions),
                                                   batch_size=self.config.laya_batch_size, sort_by_length=True,
                                                   **self._len_kwargs())
                for i, result in zip(indices, results):
                    out[i] = self._to_contract(result, questions)
            record["status"] = "received"
        except ProviderError as exc:
            record.update(status="abandoned", error_code=exc.code)
            raise
        except Exception as exc:  # OOM, tokenizer or shape errors: a sanitized code, never a trace
            record.update(status="inference_error", error_code="laya_" + type(exc).__name__)
            # A CUDA failure can leave the runtime half-moved between devices; reload it cleanly
            # on the next call rather than answering from a possibly inconsistent model.
            self.agent = None
            unload_agents()
            raise ProviderError(record["error_code"]) from None
        finally:
            record["latency_ms"] = round((self.monotonic() - started) * 1000.0, 3)
            if on_attempt:
                on_attempt(record)
        remaining()  # a late answer is never applied
        return out

    def _to_contract(self, result: dict, questions: dict) -> dict:
        answers = {}
        forecast = self.meta.get("bot_forecast") or {}
        for qid, question in questions.items():
            raw = result["answers"][qid]
            base = {"trained_examples": self.coverage(question), "answer_confidence": raw.get("answer_confidence"),
                    "laya_entropy_confidence": raw.get("confidence")}
            if question["type"] == "choice":
                probs = {k: float(raw["probabilities"][k]) for k in question["criteria"]}
                answers[qid] = {"type": "choice", "choice": raw["choice"], "probabilities": probs,
                                "confidence": jev_confidence(probs.values()), **base}
            elif question["type"] == "score":
                probs = {str(i): float(raw["probabilities"][str(i)]) for i in range(len(question["criteria"]))}
                answer = {"type": "score", "score": raw["score"], "probabilities": probs, "legend": raw["legend"],
                          "confidence": jev_confidence(probs.values()), **base}
                values = forecast.get("level_values_pct")
                if qid == FORECAST_ID and isinstance(values, list) and len(values) == len(probs):
                    # Fixed per-level means measured on the training split; the model supplies only the distribution.
                    answer["expected_mid_return_pct"] = round(sum(probs[str(i)] * v for i, v in enumerate(values)) / max(1e-9, sum(probs.values())), 5)
                answers[qid] = answer
            else:
                answers[qid] = {"type": "noul", "noul": float(raw["noul"]), **base}
        return {"model": self.model, "provider": self.name, "answers": answers,
                "usage": {"input_tokens": int(result["usage"]["input_tokens"]), "output_tokens": 0, "cost": 0.0},
                "laya": {"state_format": STATE_FORMAT, "weights_sha256": self.digest}}

    def close(self) -> None:
        # The resident model stays loaded for the next session; unload_agents() frees it.
        pass
