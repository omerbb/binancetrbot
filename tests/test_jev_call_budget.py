from types import SimpleNamespace

import pytest

from decision.openrouter import ProviderError, RequestBudgetSession


class _Session:
    def __init__(self):
        self.posts = 0
    def post(self, *args, **kwargs):
        self.posts += 1
        return object()
    def close(self):
        pass


def test_http_budget_counts_attempts_and_blocks_before_extra_request():
    provider = SimpleNamespace(call_budget_reached=False)
    upstream = _Session()
    session = RequestBudgetSession(upstream, 1, provider)
    session.post("https://openrouter.ai/api/v1/systemone")
    with pytest.raises(ProviderError, match="http_call_budget_reached"):
        session.post("https://openrouter.ai/api/v1/systemone")
    assert upstream.posts == 1
    assert provider.call_budget_reached
