"""Provider selection by decision.engine. Execution, journaling and guardrails are shared."""
from __future__ import annotations


def make_provider(decision_config):
    if decision_config.engine == "laya":
        from decision.laya_provider import LayaDecisionProvider
        return LayaDecisionProvider(decision_config)
    from decision.openrouter import OpenRouterJevProvider
    return OpenRouterJevProvider(decision_config)


def preflight(decision_config) -> None:
    """Cheap readiness check: no model load, no paid API call. Raises ProviderError."""
    if decision_config.engine == "laya":
        from decision.laya_provider import LayaDecisionProvider
        LayaDecisionProvider.verify_checkpoint(decision_config)
        return
    from decision.openrouter import OpenRouterJevProvider
    probe = OpenRouterJevProvider(decision_config)
    try:
        probe.check_ready()
    finally:
        probe.close()
