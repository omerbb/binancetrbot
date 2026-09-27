"""OpenRouter System One HTTP adapter, verified against official docs 2026-09-21.

This deliberately does NOT use chat/completions, messages, tools or JSON-mode prompts.
No key is persisted. API retries are for inference only, NEVER for exchange orders.
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from decision.contracts import dumps, validate_questions


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RequestBudgetSession:
    """Count actual HTTP POST attempts, including provider retries."""
    def __init__(self, session, maximum: int, provider):
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum HTTP calls must be a positive integer")
        self.session, self.maximum, self.provider, self.calls_made = session, maximum, provider, 0

    def post(self, *args, **kwargs):
        if self.calls_made >= self.maximum:
            self.provider.call_budget_reached = True
            raise ProviderError("http_call_budget_reached")
        self.calls_made += 1
        return self.session.post(*args, **kwargs)

    def close(self):
        self.session.close()


class OpenRouterJevProvider:
    name = "openrouter-systemone"

    def __init__(self, config, *, session=None, sleep=time.sleep, monotonic=time.monotonic):
        self.config = config
        self.model = config.model
        self.session = session or requests.Session()
        self.sleep = sleep
        self.monotonic = monotonic
        # Keys belong to a single request header, not a shared Binance HTTP session.

    def check_ready(self) -> None:
        if not self._api_key():
            raise ProviderError("missing_openrouter_api_key")
        if self.config.endpoint != "https://openrouter.ai/api/v1/systemone":
            raise ProviderError("unapproved_openrouter_endpoint")

    def evaluate(self, state: dict, questions: dict, *, on_attempt=None) -> dict:
        return self.evaluate_with_deadline(state, questions, on_attempt=on_attempt,
                                          deadline=self.monotonic() + self.config.max_decision_age_seconds)

    def evaluate_with_deadline(self, state: dict, questions: dict, *, deadline, cancelled=lambda: False,
                               on_attempt=None) -> dict:
        def remaining():
            if cancelled():
                raise ProviderError("inference_cancelled")
            left = deadline - self.monotonic()
            if left <= 0:
                raise ProviderError("decision_deadline_exceeded")
            return left

        self.check_ready()
        validate_questions(questions)
        payload = {"model": self.model, "state": state, "questions": questions}
        body = dumps(payload).encode("utf-8")
        if len(body) > self.config.max_request_bytes:
            raise ProviderError("request_too_large_no_truncation")
        key = self._api_key()
        retryable = {408, 429, 500, 502, 503, 504, 529}
        for attempt in range(1, self.config.max_attempts + 1):
            left = remaining()
            started = self.monotonic()
            retry_after = 0.0
            record = {"attempt": attempt}
            result = None
            try:
                response = self.session.post(
                    self.config.endpoint, data=body,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=(min(self.config.request_timeout_seconds, left),
                             min(self.config.request_timeout_seconds, left)),
                    allow_redirects=False,
                )
                record["http_status"] = response.status_code
                if response.status_code == 200:
                    if len(response.content) > 1024 * 1024:
                        raise ProviderError("oversized_response")
                    try:
                        result = response.json()
                    except ValueError:
                        raise ProviderError("invalid_response_json") from None
                    # Preserve the full response independently of shape validation.
                    # Remove the exact configured credential should a proxy echo it.
                    if isinstance(result, dict):
                        import json
                        result = json.loads(json.dumps(result).replace(key, "[REDACTED]"))
                    record["status"] = "received"
                else:
                    record["status"] = "http_error"
                    # Do not log server error bodies, exception reprs, headers or keys.
                    record["error_code"] = f"http_{response.status_code}"
                    if response.status_code not in retryable:
                        record["retryable"] = False
                    else:
                        record["retryable"] = True
                        retry = response.headers.get("Retry-After", "")
                        try:
                            retry_after = max(0.0, float(retry))
                        except (ValueError, TypeError):
                            try:
                                dt = parsedate_to_datetime(retry)
                                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                                retry_after = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError, OverflowError):
                                retry_after = 0.0
            except (requests.Timeout, requests.ConnectionError):
                record.update(status="network_error", error_code="network_or_timeout", retryable=True)
            except requests.RequestException:
                record.update(status="network_error", error_code="request_error", retryable=False)
            except ProviderError as exc:
                record.update(status="protocol_error", error_code=exc.code, retryable=False)
            finally:
                record["latency_ms"] = round((self.monotonic() - started) * 1000.0, 3)
                if on_attempt:
                    on_attempt(record)
            if record["status"] == "received":
                remaining()  # A successful HTTP response can still arrive too late.
                return result
            if not record.get("retryable") or attempt == self.config.max_attempts:
                raise ProviderError(record.get("error_code", "inference_failed"))
            # Never wait through a long Retry-After in a trading loop or violate it.
            if retry_after > self.config.request_timeout_seconds:
                raise ProviderError("retry_after_exceeds_decision_deadline")
            delay = max(retry_after, self.config.retry_backoff_seconds * 2 ** (attempt - 1))
            if delay >= remaining():
                raise ProviderError("decision_deadline_exceeded")
            self.sleep(delay)
        raise ProviderError("inference_failed")

    def close(self) -> None:
        self.session.close()

    def _api_key(self) -> str:
        """Prefer the optional local config value, then the configured environment."""
        return str(getattr(self.config, "api_key", "") or os.environ.get(self.config.api_key_env, "")).strip()


class BudgetedOpenRouterJevProvider(OpenRouterJevProvider):
    """OpenRouter provider used only by the bounded paid-demo runner."""
    def __init__(self, config, maximum_http_calls: int, *, session=None, sleep=time.sleep):
        self.call_budget_reached = False
        wrapped = RequestBudgetSession(session or requests.Session(), maximum_http_calls, self)
        super().__init__(config, session=wrapped, sleep=sleep)

    @property
    def calls_made(self):
        return self.session.calls_made
