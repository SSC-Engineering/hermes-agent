"""HEL-6108: free NVIDIA NIM 429 retries with backoff before paid fallback.

Acceptance:
  * simulated 429 then 200 completes without touching the fallback provider
  * sustained 429 exhausts the retry budget and surfaces a real error
    (or only then activates fallback — never on the first 429)
  * retry attempts are visible in logs with model id and attempt number
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent

NIM_BASE = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
PAID_FALLBACK = {
    "provider": "nous",
    "model": "x-ai/grok-4.5",
    "base_url": "https://inference.nousresearch.com/v1",
}


def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_nim_agent(*, max_retries: int = 3):
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="nvidia-key-abcdef12",
            base_url=NIM_BASE,
            provider="nvidia",
            model=NIM_MODEL,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=PAID_FALLBACK,
        )
        agent.client = MagicMock()
        agent._api_max_retries = max_retries
        return agent


def _mock_response(content: str, model: str = NIM_MODEL):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model=model, usage=None)


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, message: str = "Error code: 429 - rate limit exceeded"):
        super().__init__(message)
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}}


def test_nim_429_then_200_never_touches_paid_fallback(caplog):
    """AC1: intermittent free-tier 429 recovers on primary; no paid rail."""
    agent = _make_nim_agent(max_retries=3)
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if len(calls) == 1:
            raise RateLimitError()
        return _mock_response("ok from free NIM")

    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "nous-key"
    mock_fb_client.base_url = PAID_FALLBACK["base_url"]
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.conversation_loop.time.sleep"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, PAID_FALLBACK["model"]),
        ) as mock_resolve,
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        caplog.at_level(logging.INFO),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["final_response"] == "ok from free NIM"
    assert result.get("failed") is not True
    # Stayed on free NIM for both the 429 and the recovery 200.
    assert calls == [
        ("nvidia", NIM_MODEL),
        ("nvidia", NIM_MODEL),
    ]
    mock_resolve.assert_not_called()
    assert agent._fallback_activated is False
    assert agent.provider == "nvidia"
    assert agent.model == NIM_MODEL

    # AC3: retry pressure visible with model id + attempt.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "NVIDIA NIM free-tier 429" in joined or "nvidia_nim_free_tier" in joined or NIM_MODEL in joined
    assert "attempt" in joined.lower() or any(
        "retrying before paid fallback" in r.getMessage().lower() for r in caplog.records
    )


def test_nim_sustained_429_exhausts_retries_then_may_fallback(caplog):
    """AC2: sustained 429 burns the budget; first 429 never activates fallback.

    After retries are exhausted the normal max-retries path may still try
    fallback (that is intentional — paid rail is last resort, not first).
    """
    agent = _make_nim_agent(max_retries=2)
    calls = []
    fallback_activations = []

    real_try = agent._try_activate_fallback

    def tracking_try_activate(*args, **kwargs):
        fallback_activations.append((agent.provider, agent.model, len(calls)))
        return real_try(*args, **kwargs)

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        # Always 429 on primary; if we ever land on fallback, succeed so the
        # test can observe that fallback only happened after budget burn.
        if agent.provider == "nvidia":
            raise RateLimitError()
        return _mock_response("paid fallback answer", model=PAID_FALLBACK["model"])

    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "nous-key"
    mock_fb_client.base_url = PAID_FALLBACK["base_url"]
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_try_activate_fallback", side_effect=tracking_try_activate),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.conversation_loop.time.sleep"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, PAID_FALLBACK["model"]),
        ) as mock_resolve,
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        caplog.at_level(logging.INFO),
    ):
        result = agent.run_conversation("hello")

    # First call must be free NIM; first 429 must NOT have activated fallback.
    assert calls[0] == ("nvidia", NIM_MODEL)
    # No fallback activation on the first failure (calls count would be 1).
    assert not any(n_calls == 1 for _, _, n_calls in fallback_activations), (
        f"paid fallback must not engage on the first 429; got {fallback_activations}"
    )

    # Either: (a) fallback eventually engaged after budget, completed via paid,
    # or (b) no fallback activated and a real failed/error result surfaces.
    if agent._fallback_activated or mock_resolve.called:
        assert any(p == "nous" for p, _ in calls) or result.get("completed") is True
        # At least max_retries primary attempts before fallback.
        primary_attempts = sum(1 for p, m in calls if p == "nvidia")
        assert primary_attempts >= 2, (
            f"expected >=2 free-NIM attempts before paid fallback, got {calls}"
        )
    else:
        assert result.get("failed") is True or result.get("completed") is False
        assert result.get("error") or result.get("final_response")
        # No silent success.
        assert result.get("completed") is not True or result.get("failed") is True

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert NIM_MODEL in joined or "NVIDIA NIM" in joined


def test_non_nim_429_still_eager_fallbacks():
    """Regression: non-NIM providers keep eager 429 → fallback behaviour."""
    # Use a zai fallback entry so resolve_provider_client does not require a
    # live Nous token (nous_token_missing would skip the chain and mask the
    # eager-fallback behaviour under test).
    zai_fallback = {
        "provider": "zai",
        "model": "glm-4.7",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="or-key-abcdef12",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="meta-llama/llama-3.3-70b-instruct",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=zai_fallback,
        )
        agent.client = MagicMock()
        agent._api_max_retries = 3

    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.provider == "openrouter":
            raise RateLimitError()
        return _mock_response("via fallback", model=zai_fallback["model"])

    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "zai-key-abcdef12"
    mock_fb_client.base_url = zai_fallback["base_url"]
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.conversation_loop.time.sleep"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, zai_fallback["model"]),
        ) as mock_resolve,
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    mock_resolve.assert_called()
    assert agent._fallback_activated is True
    # First call primary, second on fallback (eager) — not 3 primary retries.
    assert calls[0][0] == "openrouter"
    assert any(p == "zai" for p, _ in calls)
    assert len([p for p, _ in calls if p == "openrouter"]) == 1
